"""Selective retrieval router (M05.02.02, contract m05 §4).

``classify`` is a pure regex over the question: a question without a relationship/multi-hop cue
is ``basic`` and the graph is NEVER called for it. Only a cue (or ``GRAPH_ROUTER_MODE=force``)
leads to entity linking, which is the first graph call; no linked entity means ``basic`` again.
Mode ``off`` never touches the graph. (Deviation: the "several named entities" cue needs the
graph, so it is applied only after a textual cue, never as a pre-filter.)
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Final, Literal

from app.modules.graph.ports_read import GraphReadRepository
from app.modules.retrieval.scope import RetrievalScope

RouteKind = Literal["basic", "graph"]
RouterMode = Literal["auto", "off", "force"]

MAX_SEEDS: Final = 5
MAX_LOOKUPS: Final = 16

_CUES: Final = re.compile(
    r"\bwho (works?|worked|collaborates?|cooperates?) with\b"
    r"|\b(related|connected|linked|tied) (to|with)\b"
    r"|\b(relationship|relation|connection|link) (between|of|to|with)\b"
    r"|\bdepend(s|ed|ing|ent|encies)? (on|upon)\b|\bdependenc(y|ies) of\b"
    r"|\bhow (are|is|do|does|did) .{2,80}? (related|connected|linked|relate|connect)\b"
    r"|\bsame (team|owner|group|department|org\w*|language|stack)\b"
    r"|\b(shares?|sharing) (a |the )?(team|owner|dependency|dependencies)\b"
    r"|\bbetween\b.{2,80}?\band\b"
    r"|\b(indirectly|transitively) (depend|use|relate)\w*",
    re.IGNORECASE,
)
_STOP: Final = frozenset(
    [
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "with",
        "by",
        "is",
        "are",
        "was",
        "were",
        "be",
        "do",
        "does",
        "did",
        "how",
        "what",
        "which",
        "who",
        "whom",
        "whose",
        "when",
        "where",
        "why",
        "that",
        "this",
        "these",
        "those",
        "it",
        "its",
        "as",
        "at",
        "from",
        "into",
        "than",
        "then",
        "there",
        "same",
        "other",
        "any",
        "all",
        "each",
        "both",
        "between",
        "related",
        "connected",
        "linked",
        "team",
        "teams",
        "service",
        "services",
        "depends",
        "depend",
        "uses",
        "use",
        "owned",
        "owns",
        "works",
        "work",
    ]
)
_TOKEN: Final = re.compile(r"[\w][\w.\-]*")


@dataclass(frozen=True, slots=True)
class RouteDecision:
    route: RouteKind
    reason: str
    seeds: tuple[uuid.UUID, ...] = ()


def classify(question: str, mode: RouterMode = "auto") -> RouteKind:
    """Pure and graph-free: does the question look like a relationship/multi-hop question?"""
    if mode == "off":
        return "basic"
    if mode == "force":
        return "graph"
    return "graph" if _CUES.search(question) else "basic"


def link_entities(
    scope: RetrievalScope, repo: GraphReadRepository, question: str
) -> tuple[uuid.UUID, ...]:
    """Entities of the workspace graph whose whole canonical name occurs in the question."""
    tokens = _TOKEN.findall(question)
    phrases: list[str] = []
    for i, tok in enumerate(tokens):
        if len(tok) >= 3 and tok.casefold() not in _STOP:
            phrases.append(tok)
        if i + 1 < len(tokens) and not (
            tok.casefold() in _STOP and tokens[i + 1].casefold() in _STOP
        ):
            phrases.append(f"{tok} {tokens[i + 1]}")
    found: dict[uuid.UUID, str] = {}
    for phrase in list(dict.fromkeys(p.casefold() for p in phrases))[:MAX_LOOKUPS]:
        for e in repo.search_entities(scope, phrase, None, 5):
            name = e.canonical_name
            if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", question, re.IGNORECASE):
                found[e.id] = name
    ranked = sorted(found, key=lambda i: (-len(found[i]), str(i)))
    return tuple(ranked[:MAX_SEEDS])


def route(
    scope: RetrievalScope,
    repo: GraphReadRepository,
    question: str,
    *,
    mode: RouterMode = "auto",
) -> RouteDecision:
    kind = classify(question, mode)
    if kind == "basic":
        return RouteDecision("basic", "off" if mode == "off" else "no_relationship_cue")
    seeds = link_entities(scope, repo, question)
    if not seeds:
        return RouteDecision("basic", "no_entities")
    return RouteDecision("graph", "forced" if mode == "force" else "relationship_cue", seeds)
