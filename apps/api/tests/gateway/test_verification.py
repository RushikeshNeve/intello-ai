"""M04.02.03 acceptance: unsupported claims are flagged or withheld. Unit + adversarial tests."""

from __future__ import annotations

import time

import pytest

from app.modules.ai.embeddings import FakeEmbeddingClient
from app.modules.ai.embeddings.errors import EmbeddingUnavailable
from app.modules.ai.verification import (
    ABSTENTION_MESSAGE,
    VerifyConfig,
    abstention,
    content_tokens,
    literals,
    literals_present,
    split_claims,
    stem,
    verify,
)
from tests.gateway.support import Ev, evs

E = evs("API keys must be rotated every 90 days. Backups run nightly at 02:00.")
STRICT = VerifyConfig()
FLAG = VerifyConfig(mode="flag")


def texts(answer: str) -> list[str]:
    return [u.text for u in split_claims(answer)]


# ------------------------------------------------------------------ splitter


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("One thing. Another thing.", ["One thing.", "Another thing."]),
        ("Dr. Smith leads it. He agrees.", ["Dr. Smith leads it.", "He agrees."]),
        ("Use e.g. tokens, i.e. keys. Done.", ["Use e.g. tokens, i.e. keys.", "Done."]),
        (
            "The fee is 3.5 percent. Version 2.4.1 ships.",
            ["The fee is 3.5 percent.", "Version 2.4.1 ships."],
        ),
        ("It runs in the U.S. market. Next.", ["It runs in the U.S. market.", "Next."]),
        ("See docs.example.com/x for more. Ok.", ["See docs.example.com/x for more.", "Ok."]),
        ("Keys rotate. [1] Backups run. [2]", ["Keys rotate. [1]", "Backups run. [2]"]),
        ("Keys rotate [1,2]. Backups run [3].", ["Keys rotate [1,2].", "Backups run [3]."]),
        ("Is it so? Yes! It is.", ["Is it so?", "Yes!", "It is."]),
        ("It was J. Smith who left. Then more.", ["It was J. Smith who left.", "Then more."]),
        ("a soft\nwrapped sentence. Next one.", ["a soft wrapped sentence.", "Next one."]),
        ("# Heading\nText here.", ["Text here."]),
        ("- one [1]\n- two [1]\n3. three [1]", ["one [1]", "two [1]", "three [1]"]),
        ("| h1 | h2 |\n|---|---|\n| a | b [1] |", ["| a | b [1] |"]),
        ("", []),
        ("   \n\n  ", []),
        ("密钥每90天轮换。备份每晚运行。", ["密钥每90天轮换。", "备份每晚运行。"]),
    ],
)
def test_splitter(answer: str, expected: list[str]) -> None:
    assert texts(answer) == expected


def test_splitter_is_linear_on_adversarial_input() -> None:
    start = time.perf_counter()
    huge = ("A sentence with a marker [1]. " * 5000) + "x. " * 50_000 + "[1]" * 10_000
    units = split_claims(huge)
    assert units
    assert time.perf_counter() - start < 5.0


# ------------------------------------------------------------------ helpers


def test_stemming_and_tokens() -> None:
    assert (
        stem("rotated") == stem("rotates") == stem("rotate") == stem("rotating") == stem("rotation")
    )
    assert stem("policies") == stem("policy")
    assert stem("keys") == stem("key")
    assert content_tokens("The keys are rotated, and 90 days.") == [
        stem("keys"),
        stem("rotated"),
        stem("days"),
    ]


def test_literals_numbers_dates_words() -> None:
    assert literals("1,500,000 and 3.50 and 007") >= {"1500000", "3.5", "7"}
    assert (
        literals("March 5, 2024")
        == literals("5 March 2024")
        == literals("2024-03-05")
        == {"D:2024-3-5"}
    )
    assert literals("in June 2024") == {"D:2024-6"}
    assert literals_present("in June 2024", "on 14 June 2024")
    assert not literals_present("on 15 June 2024", "on 14 June 2024")
    assert "90" in literals("ninety days")
    assert "1" in literals("one hour")
    assert not literals("one of them")
    assert literals("no digits here") == frozenset()


# ------------------------------------------------------------------ verify behaviour


def test_grounded_answer_keeps_text_and_builds_citations() -> None:
    v = verify("API keys must be rotated every 90 days [1].", None, E, config=STRICT)
    assert v.status == "grounded"
    assert v.answer_text == "API keys must be rotated every 90 days [1]."
    assert [c.marker for c in v.citations] == ["[1]"]
    assert v.citations[0].evidence_id == "e1"
    assert v.citations[0].excerpt.startswith("API keys must be rotated")
    assert not v.withheld


def test_unsupported_claim_withheld_in_strict_and_flagged_in_flag_mode() -> None:
    answer = "API keys must be rotated every 90 days [1]. The cafeteria serves free lunch [1]."
    strict = verify(answer, None, E, config=STRICT)
    assert strict.status == "partial"
    assert "cafeteria" not in strict.answer_text
    assert [c.reason for c in strict.withheld] == ["low_support"]
    flag = verify(answer, None, E, config=FLAG)
    assert flag.status == "partial"
    assert "cafeteria" in flag.answer_text
    assert len(flag.flagged) == 1
    assert flag.flagged[0].status == "unsupported"
    assert not flag.withheld


def test_all_unsupported_abstains_with_fixed_message_in_both_modes() -> None:
    for cfg in (STRICT, FLAG):
        v = verify("The moon is made of cheese [1].", None, E, config=cfg)
        assert v.status == "abstained"
        assert v.answer_text == ABSTENTION_MESSAGE
        assert not v.citations


def test_model_dont_know_and_no_evidence_abstain() -> None:
    v = verify("I don't know based on the available documents.", None, E)
    assert v.status == "abstained"
    assert v.answer_text == ABSTENTION_MESSAGE
    assert not v.withheld
    assert abstention().status == "abstained"
    assert verify("Anything [1].", None, []).status == "abstained"
    assert verify("", None, E).status == "abstained"


def test_trailing_abstain_sentence_is_dropped_when_answer_present() -> None:
    v = verify(
        "API keys must be rotated every 90 days [1]. I cannot answer the rest from the evidence.",
        None,
        E,
    )
    assert v.status == "grounded"
    assert v.answer_text == "API keys must be rotated every 90 days [1]."


def test_invalid_markers_always_withheld_even_in_flag_mode() -> None:
    for cfg in (STRICT, FLAG):
        v = verify(
            "API keys must be rotated every 90 days [1]. Backups run nightly [7]. Keys rotate [0].",
            None,
            E,
            config=cfg,
        )
        assert [c.status for c in v.claims] == ["supported", "invalid_citation", "invalid_citation"]
        assert "[7]" not in v.answer_text
        assert len(v.withheld) == 2


def test_citations_map_restricts_valid_markers() -> None:
    v = verify("API keys must be rotated every 90 days [1].", {2: "e1"}, E)
    assert v.status == "abstained"  # marker 1 is not in the context's map
    assert verify("API keys must be rotated every 90 days [2].", {2: "e1"}, E).status == "grounded"
    assert (
        verify("API keys must be rotated every 90 days [1].", {1: "unknown"}, E).status
        == "abstained"
    )


def test_wrong_marker_is_unsupported_with_reason() -> None:
    ev = evs("The VPN requires multi-factor authentication.", "Backups run nightly at 02:00.")
    v = verify("Backups run nightly [1].", None, ev)
    assert v.claims[0].reason == "wrong_marker"
    assert v.status == "abstained"


def test_numeric_date_and_contradiction_reasons() -> None:
    ev = evs(
        "Refunds are processed within 14 days. Remote work is not allowed for contractors. Signed on March 5, 2024."  # noqa: E501
    )
    cases = {
        "Refunds are processed within 30 days [1].": "number_not_in_evidence",
        "Remote work is allowed for contractors [1].": "contradiction",
        "Signed on April 5, 2024 [1].": "number_not_in_evidence",
    }
    for answer, reason in cases.items():
        assert verify(answer, None, ev).claims[0].reason == reason, answer
    assert verify("Signed on 2024-03-05 [1].", None, ev).status == "grounded"


def test_citation_padding_markers_are_trimmed() -> None:
    ev = evs("Keys rotate every 90 days.", "The cafeteria opens at 8.")
    v = verify("Keys rotate every 90 days [1,2].", None, ev)
    assert v.answer_text == "Keys rotate every 90 days [1]."
    assert [c.marker for c in v.citations] == ["[1]"]


def test_structure_is_rebuilt_for_kept_claims() -> None:
    ev = evs(
        "Passwords need 12 characters. Passwords expire every 60 days. Plan Pro costs 25 dollars."
    )
    answer = "- Passwords need 12 characters [1].\n- The CEO is Bob [1].\n- Passwords expire every 60 days [1].\n\nPlan Pro costs 25 dollars [1]."  # noqa: E501
    v = verify(answer, None, ev)
    assert (
        v.answer_text
        == "- Passwords need 12 characters [1].\n- Passwords expire every 60 days [1].\n\nPlan Pro costs 25 dollars [1]."  # noqa: E501
    )
    assert v.status == "partial"


def test_multiple_claims_status_grounded_needs_all() -> None:
    v = verify("API keys must be rotated every 90 days [1]. Backups run nightly [1].", None, E)
    assert v.status == "grounded"
    assert len(v.claims) == 2


def test_determinism_and_to_dict_has_no_text() -> None:
    a = verify("API keys must be rotated every 90 days [1]. The moon is cheese [1].", None, E)
    b = verify("API keys must be rotated every 90 days [1]. The moon is cheese [1].", None, E)
    assert a == b
    dumped = repr(a.to_dict())
    assert "moon" not in dumped
    assert "rotated" not in dumped
    assert a.to_dict()["withheld"] == 1


# ------------------------------------------------------------------ embedding path


def test_embedding_cosine_can_support_a_paraphrase_and_failure_is_skipped() -> None:
    ev = [Ev("e1", "Staff members get twenty five vacation days every year.")]
    answer = "Staff members get vacation days every year today [1]."
    emb = FakeEmbeddingClient()
    v = verify(answer, None, ev, config=VerifyConfig(lexical_min=0.95, embed_min=0.3), embedder=emb)
    assert v.status == "grounded"
    assert v.embedding_used
    assert emb.query_calls == 1
    off = FakeEmbeddingClient()
    off.fail_next(EmbeddingUnavailable("down"), times=5)
    v2 = verify(
        answer, None, ev, config=VerifyConfig(lexical_min=0.95, embed_min=0.3), embedder=off
    )
    assert v2.status == "abstained"
    assert not v2.embedding_used


def test_embedding_never_overrides_literal_or_contradiction_checks() -> None:
    ev = [Ev("e1", "Refunds are processed within 14 days.")]
    emb = FakeEmbeddingClient()
    v = verify(
        "Refunds are processed within 30 days [1].",
        None,
        ev,
        config=VerifyConfig(lexical_min=0.95, embed_min=-1.0),
        embedder=emb,
    )
    assert v.status == "abstained"
    assert v.claims[0].reason == "number_not_in_evidence"


# ------------------------------------------------------------------ adversarial inputs


def test_ten_thousand_markers_and_huge_answers_are_bounded() -> None:
    start = time.perf_counter()
    v = verify("Keys rotate every 90 days " + "[1]" * 10_000 + ".", None, E)
    assert v.status == "grounded"
    big = "API keys must be rotated every 90 days [1]. " * 30_000  # > MAX_ANSWER_CHARS
    v2 = verify(big, None, E)
    assert len(v2.claims) <= 1001
    assert v2.status == "partial"
    assert v2.claims[-1].reason == "answer_too_long"
    assert time.perf_counter() - start < 20.0


def test_giant_digit_run_is_not_a_marker_and_does_not_crash() -> None:
    v = verify("Keys rotate every 90 days [" + "9" * 6000 + "].", None, E)
    assert v.status in ("grounded", "abstained")


@pytest.mark.parametrize(
    "answer",
    [
        "המפתחות מתחלפים [1].",
        "| a | b |\n|---|\n| [1] | [1] |\n|||",
        "```\ncode [1]\n```\n```",
        "\u0000‮ [1] 😀",
        "[1][2][3] [] [,] [1,] [ 1 ] [1 , 2]",
        "..... ??? !!! ... [1]",
        "x" * 300_000,
    ],
)
def test_weird_inputs_never_raise_and_never_keep_unsupported_text(answer: str) -> None:
    v = verify(answer, None, E)
    assert v.status in ("grounded", "partial", "abstained")
    if v.status == "abstained":
        assert v.answer_text == ABSTENTION_MESSAGE


def test_unicode_equivalence_of_supported_claims() -> None:
    ev = [Ev("e1", "המפתחות מתחלפים כל 90 יום")]
    v = verify("המפתחות מתחלפים כל 90 יום [1]", None, ev)
    assert v.status == "grounded"
