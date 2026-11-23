# ruff: noqa: S105  (type-name constants, not credentials)
"""Library-free PII and secret detectors with validators (M14.04).

Each detector yields candidate ``Finding``s; checksums (Luhn, mod-97, Verhoeff) and shape checks
keep false positives down. Matched values are never logged. Names and postal addresses are NOT
detected (no NLP dependency): see the m14 contract note.
"""

from __future__ import annotations

import ipaddress
import math
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Final

EMAIL: Final = "EMAIL"
PHONE: Final = "PHONE"
IPV4: Final = "IPV4"
IPV6: Final = "IPV6"
CREDIT_CARD: Final = "CREDIT_CARD"
IBAN: Final = "IBAN"
US_SSN: Final = "US_SSN"
AADHAAR: Final = "AADHAAR"
PAN: Final = "PAN"
PASSPORT: Final = "PASSPORT"
JWT: Final = "JWT"
AWS_KEY: Final = "AWS_KEY"
GCP_KEY: Final = "GCP_KEY"
GITHUB_TOKEN: Final = "GITHUB_TOKEN"
SLACK_TOKEN: Final = "SLACK_TOKEN"
OPENAI_KEY: Final = "OPENAI_KEY"
ANTHROPIC_KEY: Final = "ANTHROPIC_KEY"
STRIPE_KEY: Final = "STRIPE_KEY"
PRIVATE_KEY: Final = "PRIVATE_KEY"
SECRET: Final = "SECRET"

SECRET_TYPES: Final = frozenset(
    {
        JWT,
        AWS_KEY,
        GCP_KEY,
        GITHUB_TOKEN,
        SLACK_TOKEN,
        OPENAI_KEY,
        ANTHROPIC_KEY,
        STRIPE_KEY,
        PRIVATE_KEY,
        SECRET,
    }
)
PII_TYPES: Final = frozenset(
    {EMAIL, PHONE, IPV4, IPV6, CREDIT_CARD, IBAN, US_SSN, AADHAAR, PAN, PASSPORT}
)
ALL_TYPES: Final = SECRET_TYPES | PII_TYPES

# Lower number wins an overlap (secrets and checksummed IDs before generic shapes).
_PRIORITY: Final = dict.fromkeys(SECRET_TYPES, 0) | {
    CREDIT_CARD: 1,
    IBAN: 1,
    US_SSN: 2,
    AADHAAR: 2,
    PAN: 2,
    PASSPORT: 2,
    EMAIL: 3,
    IPV4: 4,
    IPV6: 4,
    PHONE: 5,
}


@dataclass(frozen=True, slots=True)
class Finding:
    type: str
    start: int
    end: int
    confidence: float


# ----------------------------------------------------------------------------- validators


def luhn_ok(digits: str) -> bool:
    if not digits.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def iban_ok(value: str) -> bool:
    v = value.replace(" ", "").upper()
    if not 15 <= len(v) <= 34 or not v.isalnum() or not v[:2].isalpha() or not v[2:4].isdigit():
        return False
    rearranged = v[4:] + v[:4]
    number = "".join(str(int(c, 36)) for c in rearranged)
    return int(number) % 97 == 1


_VERHOEFF_D: Final = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_VERHOEFF_P: Final = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)


def verhoeff_ok(digits: str) -> bool:
    if not digits.isdigit():
        return False
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _VERHOEFF_D[c][_VERHOEFF_P[i % 8][int(ch)]]
    return c == 0


def shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    n = len(value)
    counts: dict[str, int] = {}
    for ch in value:
        counts[ch] = counts.get(ch, 0) + 1
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


# ----------------------------------------------------------------------------- detectors

_Detector = Callable[[str], Iterator[Finding]]


def _regex_detector(type_: str, pattern: str, confidence: float) -> _Detector:
    rx = re.compile(pattern)

    def detect_one(text: str) -> Iterator[Finding]:
        for m in rx.finditer(text):
            yield Finding(type_, m.start(), m.end(), confidence)

    return detect_one


_EMAIL_RX = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
    r"(?![A-Za-z0-9-])"
)


def _email(text: str) -> Iterator[Finding]:
    for m in _EMAIL_RX.finditer(text):
        yield Finding(EMAIL, m.start(), m.end(), 0.95)


_PHONE_RX = re.compile(
    r"(?<![\w.+])(?:\+[1-9][\d\s().-]{6,18}\d|\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4})(?![\w-])"
)


def _phone(text: str) -> Iterator[Finding]:
    for m in _PHONE_RX.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if 8 <= len(digits) <= 15:
            yield Finding(PHONE, m.start(), m.end(), 0.8 if m.group().startswith("+") else 0.6)


_IPV4_RX = re.compile(
    r"(?<![\d.])(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
    r"(?!\d|\.\d)"
)


def _ipv4(text: str) -> Iterator[Finding]:
    for m in _IPV4_RX.finditer(text):
        yield Finding(IPV4, m.start(), m.end(), 0.7)


_IPV6_RX = re.compile(r"(?<![\w:])[0-9A-Fa-f:]{3,39}(?![\w:])")


def _ipv6(text: str) -> Iterator[Finding]:
    for m in _IPV6_RX.finditer(text):
        v = m.group()
        if v.count(":") < 2 or not re.search(r"[0-9A-Fa-f]", v):
            continue
        try:
            ipaddress.IPv6Address(v)
        except ValueError:
            continue
        yield Finding(IPV6, m.start(), m.end(), 0.8)


_CARD_RX = re.compile(r"(?<![\d-])\d(?:[ -]?\d){12,18}(?![\d-])")


def _card(text: str) -> Iterator[Finding]:
    for m in _CARD_RX.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if 13 <= len(digits) <= 19 and digits[0] in "23456" and luhn_ok(digits):
            yield Finding(CREDIT_CARD, m.start(), m.end(), 0.9)


_IBAN_RX = re.compile(r"(?<![A-Za-z0-9])[A-Z]{2}\d{2}(?: ?[A-Z0-9]{1,4}){3,9}(?![A-Za-z0-9])")


def _iban(text: str) -> Iterator[Finding]:
    for m in _IBAN_RX.finditer(text):
        tokens = m.group().split(" ")
        for k in range(len(tokens), 0, -1):  # trim trailing words that are not part of the IBAN
            candidate = " ".join(tokens[:k])
            if iban_ok(candidate):
                yield Finding(IBAN, m.start(), m.start() + len(candidate), 0.95)
                break


_SSN_RX = re.compile(r"(?<![\d-])(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}(?![\d-])")


def _ssn(text: str) -> Iterator[Finding]:
    for m in _SSN_RX.finditer(text):
        yield Finding(US_SSN, m.start(), m.end(), 0.75)


_AADHAAR_RX = re.compile(r"(?<![\d-])[2-9]\d{3}([ -]?)\d{4}\1\d{4}(?![\d-])")
_AADHAAR_CTX = re.compile(r"(?i)(aadhaar|aadhar|\buid\b|\buidai\b)")


def _aadhaar(text: str) -> Iterator[Finding]:
    for m in _AADHAAR_RX.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if not verhoeff_ok(digits):
            continue
        context = bool(_AADHAAR_CTX.search(text[max(0, m.start() - 40) : m.start()]))
        if m.group(1) or context:  # a bare 12-digit run needs context (10% pass Verhoeff)
            yield Finding(AADHAAR, m.start(), m.end(), 0.9 if context else 0.7)


_PAN_RX = re.compile(r"(?<![A-Za-z0-9])[A-Z]{3}[ABCFGHLJPT][A-Z]\d{4}[A-Z](?![A-Za-z0-9])")


def _pan(text: str) -> Iterator[Finding]:
    for m in _PAN_RX.finditer(text):
        yield Finding(PAN, m.start(), m.end(), 0.8)


_PASSPORT_RX = re.compile(
    r"(?i)\bpassport(?:\s*(?:no\.?|number|num|#))?\s*[:#-]?\s*([A-Z0-9]{6,9})(?![A-Za-z0-9])"
)


def _passport(text: str) -> Iterator[Finding]:
    for m in _PASSPORT_RX.finditer(text):
        if re.search(r"\d", m.group(1)):
            yield Finding(PASSPORT, m.start(1), m.end(1), 0.7)


_HIGH_ENTROPY_RX = re.compile(
    r"(?i)\b[a-z0-9_.-]*(?:api[_-]?key|secret|token|passw(?:or)?d|pwd|auth)[a-z0-9_.-]*"
    r"[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9+/_=.~-]{12,})"
)


def _high_entropy(text: str) -> Iterator[Finding]:
    for m in _HIGH_ENTROPY_RX.finditer(text):
        value = m.group(1)
        classes = sum(
            bool(re.search(p, value)) for p in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]")
        )
        if shannon_entropy(value) >= 3.3 and classes >= 2:
            yield Finding(SECRET, m.start(1), m.end(1), 0.6)


_PEM = (
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----[\s\S]*?"
    r"(?:-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----|\Z)"
)

_DETECTORS: Final[tuple[_Detector, ...]] = (
    _regex_detector(PRIVATE_KEY, _PEM, 0.99),
    _regex_detector(
        JWT,
        r"\beyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}(?![A-Za-z0-9_-])",
        0.95,
    ),
    _regex_detector(AWS_KEY, r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA)[A-Z0-9]{16}\b", 0.95),
    _regex_detector(GCP_KEY, r"\bAIza[0-9A-Za-z_-]{35}(?![0-9A-Za-z_-])", 0.95),
    _regex_detector(
        GITHUB_TOKEN,
        r"\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{50,255})\b",
        0.97,
    ),
    _regex_detector(SLACK_TOKEN, r"\bxox[abeprs]-[A-Za-z0-9-]{10,}", 0.95),
    _regex_detector(ANTHROPIC_KEY, r"\bsk-ant-[A-Za-z0-9_-]{20,}", 0.98),
    _regex_detector(
        OPENAI_KEY, r"\bsk-(?!ant-)(?:(?:proj|svcacct|admin)-)?[A-Za-z0-9_-]{20,}", 0.9
    ),
    _regex_detector(STRIPE_KEY, r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{16,}\b", 0.97),
    _high_entropy,
    _card,
    _iban,
    _ssn,
    _aadhaar,
    _pan,
    _passport,
    _email,
    _ipv4,
    _ipv6,
    _phone,
)


def detect(text: str, *, types: frozenset[str] | None = None) -> list[Finding]:
    """Non-overlapping findings sorted by position. ``types`` restricts the detected types."""
    if not text:
        return []
    candidates = [f for d in _DETECTORS for f in d(text) if types is None or f.type in types]
    candidates.sort(key=lambda f: (_PRIORITY[f.type], -(f.end - f.start), f.start))
    taken: list[Finding] = []
    for cand in candidates:
        if all(cand.end <= t.start or cand.start >= t.end for t in taken):
            taken.append(cand)
    taken.sort(key=lambda f: f.start)
    return taken
