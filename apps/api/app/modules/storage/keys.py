"""Object keys (contract m02-contracts.md section 2).

``org/<org_id>/doc/<document_id>/<version_id>/original``. The key never contains the filename and is
built only from three canonical UUIDs, so no other character can reach it.
"""

from __future__ import annotations

import re
import uuid
from typing import NamedTuple

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_KEY_RE = re.compile(
    r"org/(?P<org>[0-9a-f-]{36})/doc/(?P<doc>[0-9a-f-]{36})/(?P<ver>[0-9a-f-]{36})/original"
)


class OriginalKeyParts(NamedTuple):
    org_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID


def _canonical(value: uuid.UUID | str, name: str) -> str:
    """Canonical lower-case hyphenated form; rejects anything that is not exactly a UUID."""
    if isinstance(value, uuid.UUID):
        return str(value)
    if not isinstance(value, str) or not _UUID_RE.fullmatch(value):
        # Strict: no braces, urn: prefix, upper case, missing hyphens or surrounding whitespace.
        raise ValueError(f"{name} must be a canonical lower-case UUID")
    return str(uuid.UUID(value))


def original_key(
    org_id: uuid.UUID | str, document_id: uuid.UUID | str, version_id: uuid.UUID | str
) -> str:
    """Key of a document version's original file. Raises ``ValueError`` for non-UUID input."""
    return (
        f"org/{_canonical(org_id, 'org_id')}/doc/{_canonical(document_id, 'document_id')}"
        f"/{_canonical(version_id, 'version_id')}/original"
    )


def parse_original_key(key: str) -> OriginalKeyParts:
    """Inverse of :func:`original_key`. Raises ``ValueError`` if ``key`` is not an original key."""
    match = _KEY_RE.fullmatch(key)
    if match is None:
        raise ValueError("not an original object key")
    try:
        parts = OriginalKeyParts(_uuid(match["org"]), _uuid(match["doc"]), _uuid(match["ver"]))
    except ValueError:
        raise ValueError("not an original object key") from None
    if original_key(*parts) != key:  # reject non-canonical spellings
        raise ValueError("not an original object key")
    return parts


def _uuid(value: str) -> uuid.UUID:
    return uuid.UUID(value)
