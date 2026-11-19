"""Answer verification and abstention (M04.02.03, contract section 7). Pure and deterministic.

``verify(answer_text, citations, evidence)``:

* ``evidence`` is the context's ``used_evidence`` (marker ``n`` is ``evidence[n-1]``);
  ``citations`` optionally maps marker -> evidence_id (``PromptBundle.citation_map``); ``None``
  derives it from the order. A marker is valid only if it is in the map and its evidence exists.
* The answer is split into claim units (sentences, list items, table rows). Every claim needs a
  valid marker; support = lexical coverage of the claim's content words (light stemming, stopwords)
  by a cited evidence text, or (optional) embedding cosine through an injected provider; and every
  number/date literal of the claim must occur in the cited evidence.
* ``strict`` withholds unsupported/uncited claims, ``flag`` keeps and flags them. A claim with an
  unknown marker is always withheld. Markers that do not support a kept claim are dropped from its
  text (citation padding, a known gemma-3-4b habit).
* Statuses: ``grounded``, ``partial``, ``abstained``. The abstention text is fixed.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from app.modules.ai.context import EvidenceLike
from app.modules.ai.embeddings.errors import EmbeddingError
from app.modules.ai.embeddings.ports import EmbeddingProvider
from app.modules.ai.models.settings import ChatSettings

ABSTENTION_MESSAGE: Final = (
    "I could not find a supported answer to this question in the documents available to you."
)
MAX_ANSWER_CHARS: Final = 200_000
MAX_CLAIMS: Final = 1000
MAX_EMBEDDED_CLAIMS: Final = 60
EXCERPT_CHARS: Final = 300

ClaimStatus = Literal[
    "supported", "unsupported", "invalid_citation", "uncited", "abstain_statement"
]
VerificationStatus = Literal["grounded", "partial", "abstained"]


@dataclass(frozen=True)
class VerifyConfig:
    mode: Literal["strict", "flag"] = "strict"
    lexical_min: float = 0.35
    embed_min: float = 0.55

    @classmethod
    def from_settings(cls, s: ChatSettings) -> VerifyConfig:
        return cls(s.VERIFY_MODE, s.VERIFY_LEXICAL_MIN, s.VERIFY_EMBED_MIN)


@dataclass(frozen=True)
class Claim:
    index: int
    text: str
    markers: tuple[int, ...]
    status: ClaimStatus
    reason: str
    support: float
    supporting_markers: tuple[int, ...]
    kept: bool
    flagged: bool

    def to_dict(self) -> dict[str, Any]:
        """Persistable summary WITHOUT the claim text (no answer text in storage/logs)."""
        return {
            "index": self.index,
            "status": self.status,
            "reason": self.reason,
            "markers": list(self.markers),
            "support": round(self.support, 3),
            "kept": self.kept,
            "flagged": self.flagged,
        }


@dataclass(frozen=True)
class VerifiedCitation:
    marker: str
    evidence_id: str
    chunk_id: str
    document_id: str
    version_id: str
    page_start: int | None
    page_end: int | None
    excerpt: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "marker": self.marker,
            "evidence_id": self.evidence_id,
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "version_id": self.version_id,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "excerpt": self.excerpt,
        }


@dataclass(frozen=True)
class Verification:
    status: VerificationStatus
    claims: tuple[Claim, ...]
    withheld: tuple[Claim, ...]
    answer_text: str
    citations: tuple[VerifiedCitation, ...]
    mode: str
    embedding_used: bool = False
    flagged: tuple[Claim, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "mode": self.mode,
            "claims": [c.to_dict() for c in self.claims],
            "withheld": len(self.withheld),
            "flagged": len(self.flagged),
            "embedding_used": self.embedding_used,
        }


def abstention(reason: str = "no_evidence", mode: str = "strict") -> Verification:
    """The result when the model was never asked (no evidence) or nothing is supported."""
    del reason
    return Verification("abstained", (), (), ABSTENTION_MESSAGE, (), mode)


# ---------------------------------------------------------------------------- claim splitting

_MARKER = re.compile(r"\[(\d{1,6}(?:\s*,\s*\d{1,6})*)\]")
_BULLET = re.compile(r"^(\s*(?:[-*+•]|\d{1,3}[.)])\s+)(.*)$")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$")
_FENCE_LINE = re.compile(r"^\s*(```|~~~)")
_ABBREV = frozenset(
    [
        "mr",
        "mrs",
        "ms",
        "dr",
        "prof",
        "sr",
        "jr",
        "st",
        "vs",
        "etc",
        "eg",
        "ie",
        "cf",
        "fig",
        "figs",
        "no",
        "nos",
        "vol",
        "pp",
        "p",
        "approx",
        "inc",
        "ltd",
        "co",
        "corp",
        "dept",
        "est",
        "jan",
        "feb",
        "mar",
        "apr",
        "jun",
        "jul",
        "aug",
        "sep",
        "sept",
        "oct",
        "nov",
        "dec",
        "min",
        "max",
        "avg",
        "ca",
        "al",
    ]
)
_TERMINATORS = ".!?…。！？"  # noqa: RUF001
_CLOSERS = "\"')”’»"  # noqa: RUF001
_ABSTAIN_PATTERNS = re.compile(
    r"(?i)\b(i (?:do not|don'?t|cannot|can'?t|could not|couldn'?t) (?:know|answer|find)|"
    r"(?:no|not enough|insufficient) (?:relevant )?(?:information|evidence)|"
    r"(?:is|are) not (?:mentioned|stated|provided|specified|covered)|"
    r"(?:does|do) not (?:mention|state|provide|specify|say|contain|answer))\b"
)


@dataclass(frozen=True)
class _Unit:
    text: str
    score_text: str
    sep: str  # separator to put before this unit when rebuilding the kept answer
    prefix: str = ""  # bullet prefix, kept when rebuilding
    new_block: bool = False


def _is_boundary(text: str, i: int) -> int | None:
    """If a sentence ends at ``text[i]`` return the index after the terminator cluster."""
    c = text[i]
    n = len(text)
    if c not in _TERMINATORS:
        return None
    j = i + 1
    while j < n and (text[j] in _TERMINATORS or text[j] in _CLOSERS):
        j += 1
    while True:  # citation markers that follow the terminator belong to this sentence
        m = _MARKER.match(text, j) or (
            _MARKER.match(text, j + 1) if j < n and text[j] == " " else None
        )
        if m is None:
            break
        j = m.end()
        while j < n and text[j] in _CLOSERS:
            j += 1
    if c in "。！？":  # noqa: RUF001
        return j
    if j < n and not text[j].isspace():
        return None  # "3.5", "v1.2", "example.com"
    if c == ".":
        k = i
        while k > 0 and (text[k - 1].isalnum() or text[k - 1] == "."):
            k -= 1
        word = text[k:i]
        if word.lower().replace(".", "") in _ABBREV and word and j == i + 1:
            return None
        if "." in word.strip(".") and len(word.replace(".", "")) <= 3:
            return None  # e.g. "U.S", "e.g", "i.e"
        if len(word) == 1 and word.isalpha() and word.isupper():
            return None  # initials "J. Smith"
    rest = text[j:].lstrip()
    if rest and rest[0].islower():
        return None
    return j


def _split_sentences(text: str) -> list[str]:
    out: list[str] = []
    start = 0
    i = 0
    n = len(text)
    while i < n:
        end = _is_boundary(text, i) if text[i] in _TERMINATORS else None
        if end is not None:
            piece = text[start:end].strip()
            if piece:
                out.append(piece)
            start = i = end
        else:
            i += 1
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return out


def _clean_for_scoring(text: str) -> str:
    text = _MARKER.sub(" ", text)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[*_`~>]+", " ", text)
    return text.strip()


def split_claims(answer: str) -> list[_Unit]:
    """Split markdown-ish answer text into claim units (see module docstring)."""
    units: list[_Unit] = []
    lines = answer[:MAX_ANSWER_CHARS].replace("\r\n", "\n").replace("\r", "\n").split("\n")
    paragraph: list[str] = []
    gap = False  # a blank line was seen since the last unit
    in_code = False
    table_header_pending: str | None = None

    def sep_for(new_block: bool) -> str:
        nonlocal gap
        if not units:
            gap = False
            return ""
        s = "\n\n" if gap else ("\n" if new_block else " ")
        gap = False
        return s

    def flush() -> None:
        if not paragraph:
            return
        joined = " ".join(p.strip() for p in paragraph)
        paragraph.clear()
        for k, sentence in enumerate(_split_sentences(joined)):
            units.append(_Unit(sentence, _clean_for_scoring(sentence), sep_for(k == 0), "", k == 0))

    for raw in lines:
        if _FENCE_LINE.match(raw):
            flush()
            in_code = not in_code
            continue
        if in_code:
            if raw.strip():
                units.append(_Unit(raw.strip(), raw.strip(), sep_for(True), "", True))
            continue
        line = raw.rstrip()
        if not line.strip():
            flush()
            gap = bool(units)
            table_header_pending = None
            continue
        if _HEADING.match(line):
            flush()
            continue
        if line.lstrip().startswith("|"):
            flush()
            if _TABLE_SEP.match(line):
                table_header_pending = None
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            # a header row is the one directly followed by a separator row: handled by lookahead
            units.append(
                _Unit(line.strip(), _clean_for_scoring(" ".join(cells)), sep_for(True), "", True)
            )
            continue
        bullet = _BULLET.match(line)
        if bullet:
            flush()
            prefix, body = bullet.group(1).strip() + " ", bullet.group(2)
            for k, sentence in enumerate(_split_sentences(body)):
                units.append(
                    _Unit(
                        sentence,
                        _clean_for_scoring(sentence),
                        sep_for(k == 0),
                        prefix if k == 0 else "",
                        k == 0,
                    )
                )
            continue
        paragraph.append(line)
    flush()
    del table_header_pending
    return _drop_table_headers(units, lines)


def _drop_table_headers(units: list[_Unit], lines: list[str]) -> list[_Unit]:
    header_rows: set[str] = set()
    for i in range(len(lines) - 1):
        if (
            lines[i].lstrip().startswith("|")
            and _TABLE_SEP.match(lines[i + 1])
            and "|" in lines[i + 1]
        ):
            header_rows.add(lines[i].strip())
    if not header_rows:
        return units
    out = [u for u in units if u.text not in header_rows]
    return out


# ---------------------------------------------------------------------------- lexical scoring

_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "being",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "he",
        "her",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "may",
        "might",
        "more",
        "most",
        "must",
        "not",
        "of",
        "on",
        "or",
        "our",
        "she",
        "should",
        "so",
        "such",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "to",
        "too",
        "us",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
        "also",
        "any",
        "all",
        "each",
        "other",
        "some",
        "only",
        "both",
        "just",
        "very",
        "per",
        "via",
        "within",
        "without",
        "about",
        "after",
        "before",
        "over",
        "under",
        "again",
        "further",
        "once",
        "here",
    ]
)
_WORD = re.compile(r"[^\W_]+")
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]+")


def stem(word: str) -> str:
    """Light, deterministic suffix stripping ('rotated', 'rotates', 'rotation' -> 'rotat')."""
    w = word
    if len(w) <= 3:
        return w
    if w.endswith("ies") and len(w) > 4:
        w = w[:-3] + "y"
    elif w.endswith("sses"):
        w = w[:-2]
    elif w.endswith("s") and not w.endswith(("ss", "us", "is")):
        w = w[:-1]
    for suffix in ("ions", "ion", "ing", "ed", "ly", "er"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            w = w[: -len(suffix)]
            break
    if w.endswith("e") and len(w) > 3:
        w = w[:-1]
    if len(w) > 3 and w[-1] == w[-2] and w[-1] not in "aeiouls":
        w = w[:-1]
    if w.endswith("y") and len(w) > 3:
        w = w[:-1] + "i"
    return w


def content_tokens(text: str) -> list[str]:
    out: list[str] = []
    for tok in _WORD.findall(text.lower()):
        if _CJK.fullmatch(tok):
            out.extend(tok[i : i + 2] for i in range(max(1, len(tok) - 1)))
        elif tok.isdigit() or tok in _STOPWORDS:
            continue
        else:
            out.append(stem(tok))
    return out


def lexical_support(
    claim_text: str, evidence_text: str, *, _evidence_set: frozenset[str] | None = None
) -> float:
    claim = content_tokens(claim_text)
    if not claim:
        return 0.0
    ev = _evidence_set if _evidence_set is not None else frozenset(content_tokens(evidence_text))
    return sum(1 for t in claim if t in ev) / len(claim)


# ---------------------------------------------------------------------------- number / date literals  # noqa: E501

_MONTHS = {
    m: i + 1
    for i, m in enumerate(
        [
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ]
    )
}
_MONTHS.update({m[:3]: i for m, i in list(_MONTHS.items())})
_MONTHS["sept"] = 9
_MONTH_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DATE_PATTERNS = (
    re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"),
    re.compile(rf"(?i)\b({_MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b"),
    re.compile(rf"(?i)\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH_RE})\.?,?\s+(\d{{4}})\b"),
    re.compile(rf"(?i)\b({_MONTH_RE})\.?\s+(\d{{4}})\b"),
)
_NUMBER = re.compile(r"(?<![\w.])\d+(?:,\d{3})*(?:\.\d+)?")
_NUMBER_WORDS = {
    w: str(i)
    for i, w in enumerate(
        [
            "zero",
            "one",
            "two",
            "three",
            "four",
            "five",
            "six",
            "seven",
            "eight",
            "nine",
            "ten",
            "eleven",
            "twelve",
            "thirteen",
            "fourteen",
            "fifteen",
            "sixteen",
            "seventeen",
            "eighteen",
            "nineteen",
            "twenty",
        ]
    )
}
_NUMBER_WORDS.update(
    {
        "thirty": "30",
        "forty": "40",
        "fifty": "50",
        "sixty": "60",
        "seventy": "70",
        "eighty": "80",
        "ninety": "90",
        "hundred": "100",
        "thousand": "1000",
        "million": "1000000",
    }
)
_UNIT_AFTER_ONE = frozenset(
    [
        "second",
        "seconds",
        "minute",
        "minutes",
        "hour",
        "hours",
        "day",
        "days",
        "week",
        "weeks",
        "month",
        "months",
        "year",
        "years",
        "time",
        "times",
        "percent",
        "hundred",
        "thousand",
        "million",
    ]
)


def literals(text: str) -> frozenset[str]:
    """Normalised number and date literals in ``text`` (dates as ``D:y-m-d``/``D:y-m``)."""
    found: set[str] = set()
    work = text
    for idx, pattern in enumerate(_DATE_PATTERNS):
        for m in pattern.finditer(work):
            g = m.groups()
            try:
                if idx == 0:
                    y, mo, d = int(g[0]), int(g[1]), int(g[2])
                    found.add(f"D:{y}-{mo}-{d}")
                elif idx == 1:
                    found.add(f"D:{int(g[2])}-{_MONTHS[g[0].lower()]}-{int(g[1])}")
                elif idx == 2:
                    found.add(f"D:{int(g[2])}-{_MONTHS[g[1].lower()]}-{int(g[0])}")
                else:
                    found.add(f"D:{int(g[1])}-{_MONTHS[g[0].lower()]}")
            except (ValueError, KeyError):
                continue
        work = pattern.sub(" ", work)
    for m in _NUMBER.finditer(work):
        raw = m.group().replace(",", "")
        if "." in raw:
            raw = raw.rstrip("0").rstrip(".") if raw.split(".")[1].strip("0") else raw.split(".")[0]
        found.add(raw.lstrip("0") or "0")
    words = _WORD.findall(work.lower())
    for i, w in enumerate(words):
        if w in _NUMBER_WORDS:
            if w == "one" and (i + 1 >= len(words) or words[i + 1] not in _UNIT_AFTER_ONE):
                continue
            found.add(_NUMBER_WORDS[w])
    return frozenset(found)


def _date_covered(claim_lit: str, evidence_lits: frozenset[str]) -> bool:
    if claim_lit in evidence_lits:
        return True
    # a month-year claim is covered by any full date in that month
    return claim_lit.count("-") == 1 and any(e.startswith(claim_lit + "-") for e in evidence_lits)


def literals_present(claim_text: str, evidence_text: str) -> bool:
    claim = literals(claim_text)
    if not claim:
        return True
    ev = literals(evidence_text)
    for lit in claim:
        if lit.startswith("D:"):
            if not _date_covered(lit, ev):
                return False
        elif lit not in ev:
            return False
    return True


# ---------------------------------------------------------------------------- negation

_NEGATION = re.compile(r"(?i)\b(?:not|no|never|cannot|without|none|neither|nor)\b|n['’]t\b")  # noqa: RUF001


def _contradicts(claim_text: str, evidence_text: str) -> bool:
    """True when the evidence sentence closest to the claim has the opposite polarity."""
    claim = frozenset(content_tokens(claim_text))
    if not claim:
        return False
    sentences = _split_sentences(re.sub(r"\s+", " ", evidence_text).strip()) or [evidence_text]
    best = max(
        enumerate(sentences),
        key=lambda p: (len(claim & frozenset(content_tokens(p[1]))), -p[0]),
    )[1]
    if not claim & frozenset(content_tokens(best)):
        return False
    return bool(_NEGATION.search(claim_text)) != bool(_NEGATION.search(best))


# ---------------------------------------------------------------------------- verification


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = math.fsum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(math.fsum(x * x for x in a))
    nb = math.sqrt(math.fsum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def _rewrite_markers(text: str, keep: tuple[int, ...]) -> str:
    keep_set = set(keep)

    def repl(m: re.Match[str]) -> str:
        nums = [int(x) for x in re.split(r"\s*,\s*", m.group(2))]
        kept = [n for n in nums if n in keep_set]
        return m.group(1) + "[" + ",".join(str(n) for n in kept) + "]" if kept else ""

    return re.sub(r"(\s?)" + _MARKER.pattern, repl, text)


def _best_excerpt(evidence_text: str, claim_texts: Sequence[str]) -> str:
    sentences = _split_sentences(re.sub(r"\s+", " ", evidence_text).strip()) or [
        evidence_text.strip()
    ]
    claim_tokens = frozenset(t for c in claim_texts for t in content_tokens(c))
    best = max(
        enumerate(sentences),
        key=lambda p: (len(claim_tokens & frozenset(content_tokens(p[1]))), -p[0]),
    )[1]
    return best[:EXCERPT_CHARS]


def verify(
    answer_text: str,
    citations: Mapping[int, str] | None,
    evidence: Sequence[EvidenceLike],
    *,
    config: VerifyConfig | None = None,
    embedder: EmbeddingProvider | None = None,
) -> Verification:
    cfg = config if config is not None else VerifyConfig()
    if not evidence:
        return abstention("no_evidence", cfg.mode)
    by_id = {e.evidence_id: e for e in evidence}
    marker_map: dict[int, str] = (
        dict(citations)
        if citations is not None
        else {i + 1: e.evidence_id for i, e in enumerate(evidence)}
    )
    valid: dict[int, EvidenceLike] = {
        n: by_id[eid] for n, eid in marker_map.items() if eid in by_id
    }
    ev_tokens = {n: frozenset(content_tokens(e.text)) for n, e in valid.items()}
    embed_ok = embedder is not None
    ev_vectors: dict[int, list[float]] = {}
    embedded = 0

    claims: list[Claim] = []
    units = split_claims(answer_text)
    overflow = len(units) > MAX_CLAIMS
    units = units[:MAX_CLAIMS]
    for index, unit in enumerate(units):
        markers = tuple(
            dict.fromkeys(
                int(x)
                for m in _MARKER.finditer(unit.text)
                for x in re.split(r"\s*,\s*", m.group(1))
            )
        )
        body = unit.score_text
        if not markers and _ABSTAIN_PATTERNS.search(body) and len(content_tokens(body)) <= 14:
            claims.append(
                Claim(
                    index,
                    unit.text,
                    markers,
                    "abstain_statement",
                    "model_abstain",
                    0.0,
                    (),
                    False,
                    False,
                )
            )
            continue
        if not markers:
            keep = cfg.mode == "flag"
            claims.append(
                Claim(index, unit.text, markers, "uncited", "no_marker", 0.0, (), keep, keep)
            )
            continue
        if any(m not in valid for m in markers):
            claims.append(
                Claim(
                    index,
                    unit.text,
                    markers,
                    "invalid_citation",
                    "unknown_marker",
                    0.0,
                    (),
                    False,
                    False,
                )
            )
            continue

        claim_vec: list[float] | None = None
        if embed_ok and embedder is not None and embedded < MAX_EMBEDDED_CLAIMS and body:
            try:
                claim_vec = embedder.embed_query(body)
                embedded += 1
                missing = [m for m in markers if m not in ev_vectors]
                if missing:
                    vecs = embedder.embed_documents([valid[m].text for m in missing])
                    ev_vectors.update(zip(missing, vecs, strict=True))
            except EmbeddingError:
                embed_ok = False
                claim_vec = None

        supporting: list[int] = []
        best = 0.0
        number_failed = False
        contradicted = False
        for m in markers:
            lex = lexical_support(body, "", _evidence_set=ev_tokens[m])
            emb = (
                _cosine(claim_vec, ev_vectors[m])
                if claim_vec is not None and m in ev_vectors
                else -1.0
            )
            passes = lex >= cfg.lexical_min or emb >= cfg.embed_min
            score = max(lex, emb if emb >= cfg.embed_min else 0.0)
            best = max(best, score)
            if not passes:
                continue
            if _contradicts(body, valid[m].text):
                contradicted = True
                continue
            if not literals_present(body, valid[m].text):
                number_failed = True
                continue
            supporting.append(m)
        if supporting:
            claims.append(
                Claim(
                    index,
                    unit.text,
                    markers,
                    "supported",
                    "ok",
                    best,
                    tuple(supporting),
                    True,
                    False,
                )
            )
            continue
        reason = (
            "contradiction"
            if contradicted
            else "number_not_in_evidence"
            if number_failed
            else "low_support"
        )
        if not number_failed and not contradicted:
            elsewhere = [
                n
                for n in valid
                if n not in markers
                and lexical_support(body, "", _evidence_set=ev_tokens[n]) >= cfg.lexical_min
                and literals_present(body, valid[n].text)
            ]
            if elsewhere:
                reason = "wrong_marker"
        keep = cfg.mode == "flag"
        claims.append(Claim(index, unit.text, markers, "unsupported", reason, best, (), keep, keep))

    if overflow:  # everything past MAX_CLAIMS is withheld as one summarized claim
        claims.append(
            Claim(len(units), "", (), "unsupported", "answer_too_long", 0.0, (), False, False)
        )
    return _assemble(units, claims, valid, cfg, embed_ok and embedder is not None and embedded > 0)


def _assemble(
    units: list[_Unit],
    claims: list[Claim],
    valid: dict[int, EvidenceLike],
    cfg: VerifyConfig,
    embedding_used: bool,
) -> Verification:
    kept = [c for c in claims if c.kept]
    supported = [c for c in claims if c.status == "supported"]
    withheld = tuple(c for c in claims if not c.kept and c.status != "abstain_statement")
    flagged = tuple(c for c in claims if c.flagged)
    if not supported:
        return Verification(
            "abstained",
            tuple(claims),
            withheld,
            ABSTENTION_MESSAGE,
            (),
            cfg.mode,
            embedding_used,
            flagged,
        )

    parts: list[str] = []
    used_markers: set[int] = set()
    for claim in kept:
        unit = units[claim.index]
        text = (
            _rewrite_markers(unit.text, claim.supporting_markers)
            if claim.status == "supported"
            else unit.text
        )
        used_markers.update(
            claim.supporting_markers if claim.status == "supported" else claim.markers
        )
        if parts:
            parts.append(unit.sep)
        parts.append(unit.prefix + text)
    answer = "".join(parts).strip()

    cited: list[VerifiedCitation] = []
    for n in sorted(used_markers):
        e = valid.get(n)
        if e is None:
            continue
        related = [units[c.index].score_text for c in supported if n in c.supporting_markers]
        cited.append(
            VerifiedCitation(
                marker=f"[{n}]",
                evidence_id=e.evidence_id,
                chunk_id=str(e.chunk_id),
                document_id=str(e.document_id),
                version_id=str(e.version_id),
                page_start=e.page_start,
                page_end=e.page_end,
                excerpt=_best_excerpt(e.text, related or [e.title]),
            )
        )
    status: VerificationStatus = (
        "grounded" if len(kept) == len(supported) and not withheld else "partial"
    )
    if status == "grounded" and any(
        c.status != "supported" for c in claims if c.status != "abstain_statement"
    ):
        status = "partial"
    return Verification(
        status, tuple(claims), withheld, answer, tuple(cited), cfg.mode, embedding_used, flagged
    )
