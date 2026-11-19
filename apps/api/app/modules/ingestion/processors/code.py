"""Source-code processor (M06.02.02): code blocks with 1-based line locators.

Blocks break at top-level definition lines (``def``/``class``/``function``/``func``/``fn``/...) and
at runs of blank lines once a block is at least ``MIN_BLOCK_LINES`` long, and never exceed
``MAX_BLOCK_LINES``. Layout (indentation) is preserved; ``line_start``/``line_end`` are the lines of
the decoded file, so a citation can open the file at the exact line.

The language is taken from the content type (``text/x-python`` ...) or the ``language=`` parameter
of ``text/x-source`` and stored in ``ExtractedDocument.language``.

Repository identity (repo, path, commit) is NOT inside the bytes: connectors record it with
``repository_metadata`` in the version metadata, and ``repository_locator`` renders the pinned
reference ``owner/name@<40-hex commit>:path`` shown next to a citation.
"""

from __future__ import annotations

import re
from typing import Final

from app.modules.ingestion.chunking import clean_newlines_and_controls
from app.modules.ingestion.contracts import ExtractedDocument, ProcessingError, SourceInfo
from app.modules.ingestion.processors.base import DocumentBuilder, Limits, check_input_size
from app.modules.ingestion.processors.decoding import charset_from_content_type, decode_bytes

NAME: Final = "code"
MIN_BLOCK_LINES: Final = 8
MAX_BLOCK_LINES: Final = 80
SOURCE_CONTENT_TYPE: Final = "text/x-source"

_DEF_LINE = re.compile(
    r"(?:async\s+def|def|class|function|func|fn|pub\s+fn|public|export|interface|struct|impl"
    r"|module|package)\b"
)
_LANG_PARAM = re.compile(r"language\s*=\s*\"?([A-Za-z0-9_+#.-]{1,30})", re.IGNORECASE)

# Extension -> programming language, for ``content_type_for_filename`` (connectors, uploads).
CODE_EXTENSIONS: Final[dict[str, str]] = {
    ".py": "python", ".js": "javascript", ".ts": "typescript", ".tsx": "typescript",
    ".jsx": "javascript", ".java": "java", ".go": "go", ".rs": "rust", ".c": "c", ".h": "c",
    ".cpp": "cpp", ".hpp": "cpp", ".cs": "csharp", ".rb": "ruby", ".php": "php",
    ".kt": "kotlin", ".swift": "swift", ".sh": "shell", ".sql": "sql", ".yaml": "yaml",
    ".yml": "yaml", ".toml": "toml", ".json": "json",
}  # fmt: skip
_CONTENT_TYPE_LANG: Final[dict[str, str]] = {
    "text/x-python": "python",
    "text/javascript": "javascript",
    "application/javascript": "javascript",
    "text/x-java-source": "java",
    "text/x-go": "go",
    "text/x-rust": "rust",
    "text/x-csrc": "c",
    "text/x-c": "c",
    "text/x-sh": "shell",
    "application/x-sh": "shell",
}
CODE_CONTENT_TYPES: Final[frozenset[str]] = frozenset({SOURCE_CONTENT_TYPE, *_CONTENT_TYPE_LANG})
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def content_type_for_filename(filename: str) -> str | None:
    """``text/x-source; language=...`` for a known source extension, else ``None``."""
    dot = filename.rfind(".")
    lang = CODE_EXTENSIONS.get(filename[dot:].lower()) if dot >= 0 else None
    return f"{SOURCE_CONTENT_TYPE}; language={lang}" if lang else None


def repository_metadata(repo: str, path: str, commit_sha: str) -> dict[str, str]:
    """Version metadata of a repository file. The commit must be a full 40-hex sha (pinned)."""
    if not _SHA_RE.match(commit_sha):
        raise ValueError("commit_sha must be a full 40-character lowercase hex sha")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        raise ValueError("repo must look like owner/name")
    if not path or path.startswith("/") or ".." in path.split("/"):
        raise ValueError("path must be repository-relative")
    return {"repo": repo, "path": path, "commit_sha": commit_sha}


def repository_locator(meta: dict[str, str], line_start: int | None = None) -> str:
    """``owner/name@commit:path`` (+ ``#Lnn``): stable even after the branch moves."""
    ref = f"{meta['repo']}@{meta['commit_sha']}:{meta['path']}"
    return f"{ref}#L{line_start}" if line_start else ref


def _language(content_type: str) -> str | None:
    base = content_type.partition(";")[0].strip().lower()
    if base in _CONTENT_TYPE_LANG:
        return _CONTENT_TYPE_LANG[base]
    match = _LANG_PARAM.search(content_type.partition(";")[2])
    return match.group(1).lower() if match else None


def extract_code(
    data: bytes, content_type: str, *, source: SourceInfo, limits: Limits
) -> ExtractedDocument:
    check_input_size(data, limits)
    if not data.strip():
        raise ProcessingError("no_text_extracted", detail="empty input")
    text = decode_bytes(data, hint=charset_from_content_type(content_type))
    lines = clean_newlines_and_controls(text).split("\n")
    builder = DocumentBuilder("code", source, NAME, limits)
    start = 0  # index of the first line of the current block

    def emit(end: int) -> None:  # lines[start:end]
        first, last = start, end
        while first < last and not lines[first].strip():
            first += 1
        while last > first and not lines[last - 1].strip():
            last -= 1
        if first < last:
            builder.add("code", "\n".join(lines[first:last]), line_start=first + 1, line_end=last)

    blanks = 0
    for index, line in enumerate(lines):
        size = index - start
        if index > start and size > 0:
            boundary = (
                (bool(_DEF_LINE.match(line)) and size >= 2)
                or (line.strip() and blanks >= 2 and size >= MIN_BLOCK_LINES)
                or size >= MAX_BLOCK_LINES
            )
            if boundary:
                emit(index)
                start = index
        blanks = blanks + 1 if not line.strip() else 0
    emit(len(lines))
    doc = builder.build()
    language = _language(content_type)
    return doc.model_copy(update={"language": language}) if language and len(language) >= 2 else doc
