"""The closed graph ontology (M05.01.01, contract m05 section 2).

Everything the extractor may produce is validated against these sets: untrusted chunk text can
never introduce a new entity type or predicate.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

ENTITY_TYPES: Final = (
    "person",
    "organization",
    "project",
    "technology",
    "concept",
    "location",
    "event",
    "document_ref",
    "other",
)
PREDICATES: Final = (
    "works_for",
    "member_of",
    "owns",
    "depends_on",
    "uses",
    "part_of",
    "located_in",
    "related_to",
    "authored",
    "mentions",
    "supersedes",
    "caused_by",
)

LABEL_ENTITY: Final = "Entity"
LABEL_CLAIM: Final = "Claim"
LABEL_EVIDENCE: Final = "Evidence"

VERIFIED: Final = "verified"
UNVERIFIED: Final = "unverified"
VERIFICATIONS: Final = (VERIFIED, UNVERIFIED)

ENTITY_ACTIVE: Final = "active"
ENTITY_MERGED: Final = "merged"
ENTITY_REJECTED: Final = "rejected"
ENTITY_STATES: Final = (ENTITY_ACTIVE, ENTITY_MERGED, ENTITY_REJECTED)

MAX_NAME_CHARS: Final = 200
MIN_NAME_CHARS: Final = 2

_ENTITY_TYPE_SET: Final = frozenset(ENTITY_TYPES)
_PREDICATE_SET: Final = frozenset(PREDICATES)
_PUNCT: Final = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE: Final = re.compile(r"\s+")


def is_entity_type(value: object) -> bool:
    return isinstance(value, str) and value in _ENTITY_TYPE_SET


def is_predicate(value: object) -> bool:
    return isinstance(value, str) and value in _PREDICATE_SET


def normalize_name(name: str) -> str:
    """NFC, casefold, punctuation to space, collapsed whitespace (the entity identity key)."""
    folded = unicodedata.normalize("NFC", name).casefold()
    return _SPACE.sub(" ", _PUNCT.sub(" ", folded)).strip()


def clean_surface(name: object) -> str | None:
    """A displayable surface form (NFC, collapsed whitespace) or ``None`` when unusable."""
    if not isinstance(name, str):
        return None
    cleaned = _SPACE.sub(" ", unicodedata.normalize("NFC", name)).strip()
    if not MIN_NAME_CHARS <= len(cleaned) <= MAX_NAME_CHARS or not normalize_name(cleaned):
        return None
    return cleaned
