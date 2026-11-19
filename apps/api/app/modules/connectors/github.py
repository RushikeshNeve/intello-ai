"""GitHub connector (M06.01.05; contract m06 section 2): REST v3 over an injected ``httpx.Client``.

* first listing (``cursor`` None) = the git tree of ``ref`` HEAD, recursive; the cursor is the HEAD
  commit sha;
* later syncs = ``compare cursor...HEAD`` (added/modified/removed/renamed files). A cursor GitHub no
  longer knows (force push), a diverged history or a truncated compare falls back to the full
  listing, flagged ``full_listing`` so the sync can reconcile deletions;
* filters: size <= ``max_file_bytes``, text/code/markdown/pdf/html/csv/xlsx extensions, ``include``
  globs, vendor/build directories skipped;
* rate limits: a 403/429 with remaining 0 (or ``Retry-After``) raises
  ``ConnectorError('rate_limited', retry_after_seconds=...)`` and the sync is rescheduled; the
  connector never retries in a loop.

The token (if any) is only ever in the client's ``Authorization`` header, built by the caller.
"""

from __future__ import annotations

import base64
import binascii
import re
import time
from collections.abc import Mapping
from fnmatch import fnmatchcase
from typing import Any, Final

import httpx

from app.modules.connectors.port import (
    ConnectorError,
    DeltaPage,
    FetchedItem,
    ItemSkipped,
    RemoteItem,
)

REPO_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9._-]{1,100}$")
REF_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
SKIP_DIRS: Final = frozenset(
    {
        ".git",
        "node_modules",
        "vendor",
        "third_party",
        "dist",
        "build",
        "target",
        "__pycache__",
        ".venv",
        "venv",
        ".next",
    }
)
TEXT_EXTENSIONS: Final = (  # noqa: SIM905 - a readable word list
    "txt rst adoc py js jsx ts tsx java go rs c h cc cpp hpp cs rb php kt swift sh "
    "sql yaml yml toml json ini cfg tf scala lua r css scss"
).split()
CONTENT_TYPES: Final[Mapping[str, str]] = {
    "md": "text/markdown",
    "markdown": "text/markdown",
    "html": "text/html",
    "htm": "text/html",
    "pdf": "application/pdf",
    "csv": "text/csv",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    **dict.fromkeys(TEXT_EXTENSIONS, "text/plain"),
}
NAMED_TEXT_FILES: Final = frozenset({"readme", "license", "dockerfile", "makefile", "changelog"})
COMPARE_FILE_LIMIT: Final = 300
MAX_RETRY_AFTER: Final = 3600


def content_type_for(path: str) -> str | None:
    name = path.rsplit("/", 1)[-1]
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext in CONTENT_TYPES:
        return CONTENT_TYPES[ext]
    return "text/plain" if name.lower() in NAMED_TEXT_FILES else None


class GitHubConnector:
    def __init__(
        self,
        client: httpx.Client,
        config: Mapping[str, Any],
        *,
        max_file_bytes: int,
        max_files: int,
    ) -> None:
        self._client = client
        self._repo = str(config["repo"])
        self._ref = str(config.get("ref") or "")
        self._include = [str(g) for g in config.get("include") or []]
        self._max_bytes = max_file_bytes
        self._max_files = max_files
        self.head_sha: str | None = None

    # -- config ---------------------------------------------------------------------------------

    @staticmethod
    def validate_config(config: Mapping[str, Any]) -> dict[str, Any]:
        repo = str(config.get("repo") or "").strip()
        if not REPO_PATTERN.fullmatch(repo):
            raise ValueError("repo must look like owner/name")
        ref = str(config.get("ref") or "").strip()
        if ref and (not REF_PATTERN.fullmatch(ref) or ".." in ref):
            raise ValueError("ref is not a valid branch, tag or sha")
        include = config.get("include") or []
        if not isinstance(include, list) or len(include) > 20:
            raise ValueError("include must be a list of at most 20 globs")
        globs = [str(g).strip() for g in include]
        if any(not g or len(g) > 200 or g.startswith("/") or ".." in g for g in globs):
            raise ValueError("include globs must be relative and at most 200 characters")
        out: dict[str, Any] = {"provider": "github", "repo": repo, "include": globs}
        if ref:
            out["ref"] = ref
        return out

    def validate(self, config: Mapping[str, Any]) -> dict[str, Any]:
        return self.validate_config(config)

    # -- http -----------------------------------------------------------------------------------

    def _get(self, path: str, params: Mapping[str, Any] | None = None) -> httpx.Response:
        try:
            response = self._client.get(path, params=params)
        except httpx.HTTPError:
            raise ConnectorError("upstream_unreachable") from None
        status = response.status_code
        remaining = response.headers.get("x-ratelimit-remaining")
        if status in (403, 429) and (remaining == "0" or "retry-after" in response.headers):
            raise ConnectorError("rate_limited", retry_after_seconds=self._retry_after(response))
        if status == 401:
            raise ConnectorError("auth_failed")
        if status in (403, 404):
            raise ConnectorError("repo_not_found")
        if status >= 500:
            raise ConnectorError("upstream_error")
        return response

    @staticmethod
    def _retry_after(response: httpx.Response) -> int:
        try:
            if "retry-after" in response.headers:
                wait = int(response.headers["retry-after"])
            else:
                wait = int(response.headers.get("x-ratelimit-reset", "0")) - int(time.time())
        except ValueError:
            wait = 60
        return max(60, min(wait, MAX_RETRY_AFTER))

    def _head(self) -> str:
        ref = self._ref or "HEAD"
        response = self._get(f"/repos/{self._repo}/commits/{ref}")
        if response.status_code != 200:
            raise ConnectorError("ref_not_found")
        sha = str(response.json().get("sha") or "")
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ConnectorError("upstream_error")
        return sha

    # -- filtering ------------------------------------------------------------------------------

    def _wanted(self, path: str) -> str | None:
        parts = path.split("/")
        if any(p in SKIP_DIRS for p in parts[:-1]):
            return None
        if self._include and not any(fnmatchcase(path, g) for g in self._include):
            return None
        return content_type_for(path)

    # -- discover / fetch -------------------------------------------------------------------------

    def discover(self, cursor: str | None) -> DeltaPage:
        head = self._head()
        self.head_sha = head
        if cursor is None:
            return self._full_listing(head)
        if cursor == head:
            return DeltaPage([], head, False)
        page = self._compare(cursor, head)
        return page if page is not None else self._full_listing(head)

    def _full_listing(self, head: str) -> DeltaPage:
        response = self._get(f"/repos/{self._repo}/git/trees/{head}", {"recursive": "1"})
        if response.status_code != 200:
            raise ConnectorError("upstream_error")
        body = response.json()
        if body.get("truncated"):
            raise ConnectorError("repo_too_large")
        items: list[RemoteItem] = []
        for entry in body.get("tree", []):
            if entry.get("type") != "blob":
                continue
            path = str(entry["path"])
            ctype = self._wanted(path)
            size = int(entry.get("size") or 0)
            if ctype is None or size > self._max_bytes:
                continue
            items.append(RemoteItem(path, path, str(entry["sha"]), size, ctype))
        if len(items) > self._max_files:
            raise ConnectorError("too_many_files")
        return DeltaPage(items, head, False, full_listing=True)

    def _compare(self, base: str, head: str) -> DeltaPage | None:
        files: list[dict[str, Any]] = []
        page_no = 1
        while True:
            response = self._get(
                f"/repos/{self._repo}/compare/{base}...{head}",
                {"per_page": 100, "page": page_no},
            )
            if response.status_code in (404, 422):
                return None  # the cursor commit is gone (force push): list everything again
            if response.status_code != 200:
                raise ConnectorError("upstream_error")
            body = response.json()
            if body.get("status") not in ("ahead", "identical"):
                return None
            batch = body.get("files") or []
            files.extend(batch)
            if len(batch) < 100 or len(files) >= COMPARE_FILE_LIMIT:
                break
            page_no += 1
        if len(files) >= COMPARE_FILE_LIMIT:
            return None
        items: list[RemoteItem] = []
        for entry in files:
            path = str(entry["filename"])
            status = str(entry.get("status"))
            previous = entry.get("previous_filename")
            if status == "removed" or previous:
                gone = str(previous or path)
                items.append(RemoteItem(gone, gone, "", 0, None, deleted=True))
                if status == "removed":
                    continue
            ctype = self._wanted(path)
            if ctype is None:
                continue
            items.append(RemoteItem(path, path, str(entry.get("sha") or ""), 0, ctype))
        if len(items) > self._max_files:
            return None
        return DeltaPage(items, head, False)

    def fetch(self, item: RemoteItem) -> FetchedItem:
        response = self._get(f"/repos/{self._repo}/git/blobs/{item.version_token}")
        if response.status_code != 200:
            raise ConnectorError("upstream_error")
        body = response.json()
        if int(body.get("size") or 0) > self._max_bytes:
            raise ItemSkipped("too_large")
        try:
            data = base64.b64decode(str(body.get("content") or ""))
        except (binascii.Error, ValueError):
            raise ConnectorError("upstream_error") from None
        return FetchedItem(
            data=data,
            content_type=item.content_type or "text/plain",
            metadata={"repo": self._repo, "path": item.path, "commit_sha": self.head_sha},
        )
