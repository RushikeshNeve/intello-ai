"""Safe URL capture (M06.01.02, m06-contracts.md section 3).

``POST /v1/orgs/{o}/workspaces/{w}/documents/from-url {url, title?}`` (``document.write``): the
server fetches ONE public web page / text / PDF and stores it exactly like an upload (document +
immutable version + ingestion job).

SSRF defence (every rule is enforced on the first request AND on every redirect hop):

* scheme http/https only, no userinfo, port in ``URL_CAPTURE_ALLOWED_PORTS`` (default 80, 443);
* the host must be a domain name or a canonical IP literal (decimal/hex/octal IPv4 spellings such
  as ``2130706433`` or ``0x7f.1`` are refused), it is resolved by US and EVERY answer must be a
  global unicast address (loopback, RFC1918, link-local incl. 169.254.169.254, CGNAT, ULA incl.
  fd00::/8, multicast, reserved, NAT64/6to4 and IPv4-mapped IPv6 of any of those are blocked);
* the connection goes to the validated IP (DNS-rebinding pin) with ``Host``/SNI of the original
  name; redirects (max 3) are followed manually, https -> http downgrades are refused;
* the response is streamed and aborted at ``URL_CAPTURE_MAX_BYTES`` (decoded bytes, so a gzip bomb
  is cut too); total wall clock ``URL_CAPTURE_TIMEOUT_SECONDS``; content type allow-list;
* no cookies, no credentials, ``User-Agent: IntelloBot``.

Errors are generic (``url_not_allowed``, ``fetch_failed``, ``too_large``, ``unsupported_type``) and
never echo an address. An unauthorized caller is rejected BEFORE any network activity (no oracle).
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Annotated, Final, Protocol
from urllib.parse import urljoin, urlsplit

import httpx
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import Tenant, TenantRequest, api_error
from app.core.config import get_settings
from app.modules.ai import egress
from app.modules.connectors.settings import UrlCaptureSettings, get_url_capture_settings
from app.modules.documents import errors, upload_service
from app.modules.documents.remote_ingest import QuotaExceeded, ingest_bytes
from app.modules.documents.schemas import sanitize_filename
from app.modules.storage.deps import ObjectStoreDep
from app.modules.storage.ports import ObjectStore, StorageError

logger = logging.getLogger("ukos.connectors.url_capture")

USER_AGENT: Final = "IntelloBot"
URL_NOT_ALLOWED: Final = "url_not_allowed"
FETCH_FAILED: Final = "fetch_failed"
TOO_LARGE: Final = "too_large"
UNSUPPORTED_TYPE: Final = "unsupported_type"
_HTTP_STATUS: Final = {
    URL_NOT_ALLOWED: 422,
    FETCH_FAILED: 502,
    TOO_LARGE: 413,
    UNSUPPORTED_TYPE: 415,
}
ALLOWED_CONTENT_TYPES: Final = frozenset(
    {"text/html", "application/xhtml+xml", "text/plain", "text/markdown", "application/pdf"}
)
_REDIRECT_CODES: Final = frozenset({301, 302, 303, 307, 308})
_CHUNK: Final = 64 * 1024
_MAX_URL_CHARS: Final = 2048
_NAT64: Final = ipaddress.ip_network("64:ff9b::/96")
_SIX_TO_FOUR: Final = ipaddress.ip_network("2002::/16")


class UrlCaptureError(Exception):
    """A closed-set failure; ``code`` is the only thing the client ever learns."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# --- address policy ----------------------------------------------------------------------------


def is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True unless ``ip`` is a public (global unicast) address."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return is_blocked_ip(ip.ipv4_mapped)
        if ip in _NAT64 or ip in _SIX_TO_FOUR:
            return True
    return not ip.is_global or ip.is_multicast


Resolver = Callable[[str, int], list[str]]


def system_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


@dataclass(frozen=True, slots=True)
class PinnedRequest:
    """One hop: connect to ``ip`` (already validated), speak for ``host``."""

    scheme: str
    host: str
    port: int
    ip: str
    path_query: str
    headers: Mapping[str, str]
    timeout: float


class FetchResponse(Protocol):
    @property
    def status_code(self) -> int: ...

    @property
    def headers(self) -> Mapping[str, str]: ...

    def iter_bytes(self) -> Iterator[bytes]: ...

    def close(self) -> None: ...


Transport = Callable[[PinnedRequest], FetchResponse]


class _HttpxResponse:
    def __init__(self, client: httpx.Client, response: httpx.Response) -> None:
        self._client, self._response = client, response
        self.status_code = response.status_code
        self.headers = {k.lower(): v for k, v in response.headers.items()}

    def iter_bytes(self) -> Iterator[bytes]:
        return self._response.iter_bytes(_CHUNK)

    def close(self) -> None:
        self._response.close()
        self._client.close()


def httpx_transport(req: PinnedRequest) -> FetchResponse:
    """Real transport: TCP to the pinned IP; TLS SNI + certificate check use the original name."""
    egress.check(req.host, "url_capture", addresses=[req.ip])
    literal = f"[{req.ip}]" if ":" in req.ip else req.ip
    client = httpx.Client(
        timeout=req.timeout, follow_redirects=False, trust_env=False, cookies=None
    )
    try:
        request = client.build_request(
            "GET",
            f"{req.scheme}://{literal}:{req.port}{req.path_query}",
            headers=dict(req.headers),
            extensions={"sni_hostname": req.host},
        )
        return _HttpxResponse(client, client.send(request, stream=True))
    except BaseException:
        client.close()
        raise


# --- URL checks --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Target:
    scheme: str
    host: str
    port: int
    path_query: str
    literal: ipaddress.IPv4Address | ipaddress.IPv6Address | None


def _parse(url: str, settings: UrlCaptureSettings) -> _Target:
    if len(url) > _MAX_URL_CHARS or any(ord(c) < 33 or ord(c) == 127 for c in url):
        raise UrlCaptureError(URL_NOT_ALLOWED)
    try:
        parts = urlsplit(url)
        port = parts.port
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        raise UrlCaptureError(URL_NOT_ALLOWED) from None
    if parts.scheme not in ("http", "https") or not host or "@" in parts.netloc:
        raise UrlCaptureError(URL_NOT_ALLOWED)
    port = port or (443 if parts.scheme == "https" else 80)
    if port not in settings.allowed_ports or "%" in host:
        raise UrlCaptureError(URL_NOT_ALLOWED)
    literal: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        # Not a canonical IP literal: it must be a real domain name. A numeric or 0x-prefixed last
        # label is an inet_aton spelling of an IPv4 address (2130706433, 0x7f.1, 017700000001).
        last = host.rsplit(".", 1)[-1]
        if last.isdigit() or last.startswith("0x") or "." not in host or ":" in host:
            raise UrlCaptureError(URL_NOT_ALLOWED) from None
    path_query = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return _Target(parts.scheme, host, port, path_query, literal)


def _pin(target: _Target, resolver: Resolver) -> str:
    """The IP to connect to; every resolved address must be public."""
    if target.literal is not None:
        addresses = [str(target.literal)]
    else:
        try:
            addresses = resolver(target.host, target.port)
        except OSError:
            raise UrlCaptureError(FETCH_FAILED) from None
    if not addresses:
        raise UrlCaptureError(FETCH_FAILED)
    parsed = []
    for raw in addresses:
        try:
            parsed.append(ipaddress.ip_address(raw.partition("%")[0]))
        except ValueError:
            raise UrlCaptureError(URL_NOT_ALLOWED) from None
    if any(is_blocked_ip(ip) for ip in parsed):
        raise UrlCaptureError(URL_NOT_ALLOWED)
    return str(parsed[0])


@dataclass(frozen=True, slots=True)
class CapturedPage:
    data: bytes
    content_type: str
    final_url: str


class SafeFetcher:
    """Fetch one URL under the rules in the module docstring. ``resolver``/``transport`` inject."""

    def __init__(
        self,
        *,
        settings: UrlCaptureSettings | None = None,
        resolver: Resolver = system_resolver,
        transport: Transport = httpx_transport,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings or get_url_capture_settings()
        self._resolver = resolver
        self._transport = transport
        self._clock = clock

    def fetch(self, url: str) -> CapturedPage:
        cfg = self._settings
        deadline = self._clock() + cfg.URL_CAPTURE_TIMEOUT_SECONDS
        current = url
        for hop in range(cfg.URL_CAPTURE_MAX_REDIRECTS + 1):
            target = _parse(current, cfg)
            ip = _pin(target, self._resolver)
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise UrlCaptureError(FETCH_FAILED)
            default_port = 443 if target.scheme == "https" else 80
            host_header = (
                target.host if target.port == default_port else f"{target.host}:{target.port}"
            )
            request = PinnedRequest(
                scheme=target.scheme,
                host=target.host,
                port=target.port,
                ip=ip,
                path_query=target.path_query,
                headers={
                    "Host": host_header,
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,text/plain,application/pdf;q=0.9",
                    "Accept-Encoding": "gzip",
                },
                timeout=remaining,
            )
            try:
                response = self._transport(request)
            except UrlCaptureError:
                raise
            except Exception:  # noqa: BLE001 - any transport failure is a generic fetch_failed
                raise UrlCaptureError(FETCH_FAILED) from None
            try:
                if response.status_code in _REDIRECT_CODES:
                    location = response.headers.get("location")
                    if not location or hop >= cfg.URL_CAPTURE_MAX_REDIRECTS:
                        raise UrlCaptureError(FETCH_FAILED)
                    nxt = urljoin(current, location)
                    if target.scheme == "https" and urlsplit(nxt).scheme != "https":
                        raise UrlCaptureError(URL_NOT_ALLOWED)
                    current = nxt
                    continue
                if response.status_code != 200:
                    raise UrlCaptureError(FETCH_FAILED)
                return self._read_body(response, current, deadline)
            finally:
                response.close()
        raise UrlCaptureError(FETCH_FAILED)  # pragma: no cover - the loop always returns/raises

    def _read_body(self, response: FetchResponse, url: str, deadline: float) -> CapturedPage:
        cap = self._settings.URL_CAPTURE_MAX_BYTES
        content_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
        if content_type not in ALLOWED_CONTENT_TYPES:
            raise UrlCaptureError(UNSUPPORTED_TYPE)
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > cap:
            raise UrlCaptureError(TOO_LARGE)
        body = bytearray()
        try:
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > cap:
                    raise UrlCaptureError(TOO_LARGE)
                if self._clock() > deadline:
                    raise UrlCaptureError(FETCH_FAILED)
        except UrlCaptureError:
            raise
        except Exception:  # noqa: BLE001 - any stream failure is a generic fetch_failed
            raise UrlCaptureError(FETCH_FAILED) from None
        if not body:
            raise UrlCaptureError(FETCH_FAILED)
        return CapturedPage(bytes(body), content_type, url)


def get_fetcher() -> SafeFetcher:
    """Dependency (tests override it with a fetcher over a fake resolver/transport)."""
    return SafeFetcher()


# --- document creation (the ONE place to swap for the shared "version from bytes" service) ------


def _public_source_url(url: str) -> str:
    """The captured URL without query and fragment (they may carry tokens)."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}{parts.path}"[:500]


def _filename_for(url: str, content_type: str) -> str:
    segment = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    ext = {"application/pdf": ".pdf", "text/plain": ".txt", "text/markdown": ".md"}.get(
        content_type, ".html"
    )
    name = sanitize_filename(segment) if segment else ""
    if not name or "." not in name:
        name = (urlsplit(url).hostname or "page") + ext
    return name[:255]


def create_document_from_bytes(
    tenant: TenantRequest,
    store: ObjectStore,
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    *,
    data: bytes,
    content_type: str,
    filename: str,
    title: str | None,
    source_url: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Store ``data`` as a NEW document's first version and queue ingestion (finalize's work
    without the presigned PUT). Returns ``(document_id, version_id)``."""
    settings = get_settings()
    stored_key: list[str] = []
    try:
        with tenant.transaction(org_id) as session:
            upload_service.authorize_write(
                session,
                tenant.user_id,
                org_id,
                workspace_id,
                document_id=None,
                target_type="workspace",
                target_id=str(workspace_id),
            )
            if len(data) > settings.UPLOAD_MAX_BYTES:
                raise errors.file_too_large()
            upload_service.lock_org_uploads(session, org_id)
            source_id = upload_service.get_or_create_upload_source(
                session, org_id, workspace_id, tenant.user_id
            )
            try:
                result = ingest_bytes(
                    session,
                    store,
                    org_id=org_id,
                    workspace_id=workspace_id,
                    source_id=source_id,
                    created_by=tenant.user_id,
                    data=data,
                    filename=filename,
                    content_type=content_type,
                    title=title,
                    request_id=tenant.request_id,
                    audit_metadata={"captured_from": _public_source_url(source_url)},
                    actor_user_id=tenant.user_id,
                    on_stored=stored_key.append,
                )
            except QuotaExceeded:
                raise errors.quota_exceeded() from None
            except StorageError as exc:
                raise upload_service.storage_error_to_http(exc) from None
    except BaseException:
        for key in stored_key:
            try:
                store.delete_object(key)
            except StorageError:
                logger.warning("orphaned captured object after a failed create")
        raise
    return result.document_id, result.version_id


# --- route -------------------------------------------------------------------------------------

router = APIRouter(prefix="/v1/orgs/{org_id}/workspaces/{workspace_id}", tags=["documents"])


class FromUrlBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: Annotated[str, Field(min_length=1, max_length=_MAX_URL_CHARS)]
    title: Annotated[str, Field(min_length=1, max_length=255)] | None = None


class FromUrlResult(BaseModel):
    document_id: uuid.UUID
    version_id: uuid.UUID
    content_type: str
    size_bytes: int


@router.post(
    "/documents/from-url", response_model=FromUrlResult, status_code=status.HTTP_201_CREATED
)
def create_document_from_url(
    tenant: Tenant,
    store: ObjectStoreDep,
    fetcher: Annotated[SafeFetcher, Depends(get_fetcher)],
    org_id: uuid.UUID,
    workspace_id: uuid.UUID,
    body: FromUrlBody,
) -> FromUrlResult:
    # Authorize FIRST and release the transaction: no network activity for an unauthorized caller,
    # and no database transaction is held open during the fetch.
    with tenant.transaction(org_id) as session:
        upload_service.authorize_write(
            session,
            tenant.user_id,
            org_id,
            workspace_id,
            document_id=None,
            target_type="workspace",
            target_id=str(workspace_id),
        )
    try:
        page = fetcher.fetch(body.url.strip())
    except UrlCaptureError as exc:
        raise api_error(_HTTP_STATUS[exc.code], exc.code) from None
    document_id, version_id = create_document_from_bytes(
        tenant,
        store,
        org_id,
        workspace_id,
        data=page.data,
        content_type=page.content_type,
        filename=_filename_for(page.final_url, page.content_type),
        title=body.title,
        source_url=page.final_url,
    )
    return FromUrlResult(
        document_id=document_id,
        version_id=version_id,
        content_type=page.content_type,
        size_bytes=len(page.data),
    )
