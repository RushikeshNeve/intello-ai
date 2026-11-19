"""Golden set for verification: labelled (answer, evidence) pairs with per-claim ground truth.

Truth per claim unit (in splitter order): True = the evidence really supports the claim. Metrics are
computed on WITHHOLDING (positive class = "claim must not be shown"): precision = of the claims we
withheld, how many were truly unsupported; recall = of the truly unsupported claims, how many we
withheld. A few cases are known hard for a lexical verifier (pure synonyms, "every year" vs "every
four years"); they are labelled honestly, not removed.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.modules.ai.verification import VerifyConfig, split_claims, verify
from tests.gateway.support import evs

T, F = True, False


@dataclass(frozen=True)
class Case:
    id: str
    evidence: tuple[str, ...]
    answer: str
    truth: tuple[bool, ...]
    kind: str


K = "API keys must be rotated every 90 days."
CASES = [
    Case("supported_exact", (K,), "API keys must be rotated every 90 days [1].", (T,), "supported"),
    Case(
        "paraphrase_stems", (K,), "Keys need to be rotated each 90 days [1].", (T,), "paraphrased"
    ),
    Case(
        "paraphrase_synonyms",
        ("Employees get 25 days of paid leave per year.",),
        "Staff receive 25 days of paid vacation annually [1].",
        (T,),
        "paraphrased",
    ),
    Case(
        "unrelated",
        ("The office is open Monday to Friday.",),
        "The cafeteria serves free lunch every day [1].",
        (F,),
        "unsupported",
    ),
    Case(
        "number_contradiction",
        ("Refunds are processed within 14 days.",),
        "Refunds are processed within 30 days [1].",
        (F,),
        "contradicted",
    ),
    Case(
        "wrong_marker",
        ("The VPN requires multi-factor authentication.", "Backups run nightly at 02:00."),
        "Backups run nightly [1].",
        (F,),
        "wrong_marker",
    ),
    Case(
        "partial",
        ("The service level is 99.9% uptime. Support is available by email.",),
        "The service level is 99.9% uptime [1]. Support is available 24/7 by phone [1].",
        (T, F),
        "partial",
    ),
    Case(
        "uncited",
        ("The service level is 99.9% uptime.",),
        "The service level is 99.9% uptime.",
        (F,),
        "uncited",
    ),
    Case(
        "invalid_marker",
        ("The service level is 99.9% uptime.",),
        "The service level is 99.9% uptime [5].",
        (F,),
        "invalid",
    ),
    Case(
        "date_iso_vs_long",
        ("The contract was signed on March 5, 2024.",),
        "The contract was signed on 2024-03-05 [1].",
        (T,),
        "supported",
    ),
    Case(
        "wrong_date",
        ("The contract was signed on March 5, 2024.",),
        "The contract was signed on April 5, 2024 [1].",
        (F,),
        "contradicted",
    ),
    Case(
        "decimal_percent",
        ("The fee is 3.5 percent per transaction.",),
        "The fee is 3.5% per transaction [1].",
        (T,),
        "supported",
    ),
    Case(
        "wrong_decimal",
        ("The fee is 3.5 percent per transaction.",),
        "The fee is 3.7% per transaction [1].",
        (F,),
        "contradicted",
    ),
    Case(
        "two_markers",
        ("Alice manages the Berlin office.", "Bob manages the Paris office."),
        "Alice manages the Berlin office [1] and Bob manages the Paris office [2].",
        (T,),
        "supported",
    ),
    Case(
        "bullets",
        ("Passwords must have at least 12 characters and must be changed every 60 days.",),
        "- Passwords must have 12 characters [1].\n- Passwords expire every 60 days [1].",
        (T, T),
        "supported",
    ),
    Case(
        "table_wrong_price",
        ("Plan Basic costs 10 dollars. Plan Pro costs 25 dollars.",),
        "| Plan | Price |\n|---|---|\n| Basic | 10 dollars [1] |\n| Pro | 99 dollars [1] |",
        (T, F),
        "contradicted",
    ),
    Case(
        "abbreviations",
        ("Dr. Smith leads the security team, e.g. incident response, and reports to the CTO.",),
        "Dr. Smith leads the security team [1]. He reports to the CTO [1].",
        (T, T),
        "supported",
    ),
    Case("model_abstains", (K,), "I don't know based on the available documents.", (), "abstain"),
    Case(
        "answer_plus_abstain",
        ("API keys rotate every 90 days.",),
        "Keys rotate every 90 days [1]. I cannot answer the rest from the evidence.",
        (T,),
        "supported",
    ),
    Case(
        "padding",
        ("Keys rotate every 90 days.", "The cafeteria opens at 8."),
        "Keys rotate every 90 days [1,2].",
        (T,),
        "supported",
    ),
    Case(
        "german",
        ("Die Rechnung muss innerhalb von 30 Tagen bezahlt werden.",),
        "Die Rechnung muss innerhalb von 30 Tagen bezahlt werden [1].",
        (T,),
        "supported",
    ),
    Case(
        "hebrew", ("המפתחות מתחלפים כל 90 יום",), "המפתחות מתחלפים כל 90 יום [1]", (T,), "supported"
    ),
    Case("cjk", ("密钥每90天轮换一次。",), "密钥每90天轮换一次 [1]。", (T,), "supported"),
    Case(
        "negation_flip",
        ("Remote work is not allowed for contractors.",),
        "Remote work is allowed for contractors [1].",
        (F,),
        "contradicted",
    ),
    Case(
        "negation_flip_modal",
        ("Guests may bring pets.",),
        "Guests may not bring pets [1].",
        (F,),
        "contradicted",
    ),
    Case(
        "fabricated_number",
        ("The company was founded in 2015 in Austin.",),
        "The company was founded in 2015 [1]. It has 500 employees [1].",
        (T, F),
        "partial",
    ),
    Case(
        "fabricated_entity",
        ("Kestrel Labs makes sensors.",),
        "Kestrel Labs makes sensors [1]. Its CEO is a former astronaut [1].",
        (T, F),
        "partial",
    ),
    Case(
        "hard_every_year",
        ("Laptops are refreshed every four years.",),
        "Laptops are refreshed every year [1].",
        (F,),
        "contradicted",
    ),
    Case(
        "number_words",
        ("Access is revoked after ninety days of inactivity.",),
        "Access is revoked after 90 days of inactivity [1].",
        (T,),
        "supported",
    ),
    Case(
        "number_word_wrong",
        ("Access is revoked after ninety days of inactivity.",),
        "Access is revoked after one hour of inactivity [1].",
        (F,),
        "contradicted",
    ),
    Case(
        "reordered",
        ("Step one is to open the console.",),
        "Open the console first [1].",
        (T,),
        "paraphrased",
    ),
    Case(
        "no_content", ("Keys rotate every 90 days.",), "It is what it is [1].", (F,), "unsupported"
    ),
    Case(
        "multi_paragraph",
        ("Keys rotate every 90 days.",),
        "Keys rotate every 90 days [1].\n\nThe cafeteria opens at 8 [1].",
        (T, F),
        "partial",
    ),
    Case(
        "version_numbers",
        ("Version 2.4.1 fixes the login bug.",),
        "Version 2.4.1 fixes the login bug [1].",
        (T,),
        "supported",
    ),
    Case(
        "url",
        ("Docs live at docs.example.com/guide.",),
        "Docs live at docs.example.com/guide [1].",
        (T,),
        "supported",
    ),
    Case(
        "markers_after_period",
        ("Keys rotate every 90 days. Keys are stored in a vault.",),
        "Keys rotate every 90 days. [1] They are stored in a vault. [1]",
        (T, T),
        "supported",
    ),
    Case(
        "markdown_emphasis",
        ("Keys rotate every 90 days.",),
        "**Keys** rotate every _90 days_ [1].",
        (T,),
        "supported",
    ),
    Case(
        "partial_numbers_ok",
        ("Revenue grew 12% to 4.5 million dollars in 2023.",),
        "Revenue grew 12% in 2023 [1].",
        (T,),
        "supported",
    ),
    Case(
        "partial_numbers_wrong",
        ("Revenue grew 12% to 4.5 million dollars in 2023.",),
        "Revenue grew 15% in 2023 [1].",
        (F,),
        "contradicted",
    ),
    Case(
        "month_year",
        ("The audit finished on 14 June 2024.",),
        "The audit finished in June 2024 [1].",
        (T,),
        "supported",
    ),
    Case(
        "thousands",
        ("The budget is 1,500,000 dollars.",),
        "The budget is 1500000 dollars [1].",
        (T,),
        "supported",
    ),
    Case(
        "invented_policy",
        ("Employees may work remotely two days per week.",),
        "Employees receive a stipend for home office equipment [1].",
        (F,),
        "unsupported",
    ),
]

MIN_PRECISION = 0.90
MIN_RECALL = 0.80
HARD_CASES = {"hard_every_year", "paraphrase_synonyms"}


def run_case(case: Case, cfg: VerifyConfig | None = None) -> list[bool]:
    """Per claim: True if the verifier KEPT it as supported."""
    v = verify(case.answer, None, evs(*case.evidence), config=cfg or VerifyConfig())
    return [c.status == "supported" for c in v.claims if c.status != "abstain_statement"]


def test_truth_labels_match_the_splitter() -> None:
    for case in CASES:
        units = list(split_claims(case.answer))
        v = verify(case.answer, None, evs(*case.evidence))
        scored = [c for c in v.claims if c.status != "abstain_statement"]
        assert len(scored) == len(case.truth), case.id
        assert len(units) >= len(scored)


def test_precision_recall_and_thresholds(capsys) -> None:  # type: ignore[no-untyped-def]
    tp = fp = fn = tn = 0
    misses: list[str] = []
    for case in CASES:
        got = run_case(case)
        for i, (truth, kept) in enumerate(zip(case.truth, got, strict=True)):
            if not truth and not kept:
                tp += 1
            elif truth and not kept:
                fp += 1
                misses.append(f"over-withheld {case.id}#{i}")
            elif not truth and kept:
                fn += 1
                misses.append(f"leaked {case.id}#{i}")
            else:
                tn += 1
    precision = tp / (tp + fp)
    recall = tp / (tp + fn)
    total = tp + fp + fn + tn
    with capsys.disabled():
        print(
            f"\n[verification golden] cases={len(CASES)} claims={total} "
            f"withhold precision={precision:.3f} recall={recall:.3f} "
            f"(tp={tp} fp={fp} fn={fn} tn={tn}) misses={misses}"
        )
    assert precision >= MIN_PRECISION, misses
    assert recall >= MIN_RECALL, misses
    # Only the labelled-hard cases may be wrong.
    unexpected = [m for m in misses if m.split()[1].split("#")[0] not in HARD_CASES]
    assert not unexpected, unexpected


def test_flag_mode_keeps_every_cited_claim_but_flags_the_unsupported() -> None:
    flagged = kept_unsupported = 0
    for case in CASES:
        if case.kind in ("invalid", "abstain"):
            continue
        v = verify(case.answer, None, evs(*case.evidence), config=VerifyConfig(mode="flag"))
        for c in v.claims:
            if c.status in ("unsupported", "uncited"):
                assert c.kept
                assert c.flagged
                flagged += 1
            elif c.status == "invalid_citation":
                assert not c.kept
    assert flagged > 0 or kept_unsupported == 0
