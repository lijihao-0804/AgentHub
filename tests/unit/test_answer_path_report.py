"""Tests for the pure answer-path diagnostic scorer."""

from __future__ import annotations

from copy import deepcopy

import pytest

from benchmarks.public_rag.answer_path_report import score_diagnostic


def _dataset() -> dict:
    cases = []
    for number, paper_id in ((1, "paper-a"), (2, "paper-b")):
        records = [
            {
                "source_id": f"s{number}-actual",
                "text": "visible actual evidence",
            },
            {
                "source_id": f"s{number}-support",
                "text": "visible sufficient evidence",
            },
        ]
        cases.append(
            {
                "query_id": f"q{number}",
                "paper_id": paper_id,
                "question": f"question {number}?",
                "answer_references": [f"answer {number}", f"alias {number}"],
                "conditions": {
                    "actual": {"records": records[:1]},
                    "sufficient": {"records": records},
                    "removed": {"records": records[1:]},
                },
            }
        )
    return {
        "schema_version": 1,
        "state": "FROZEN_DEVELOPMENT",
        "ledger": [
            {"query_id": "q1", "annotation_state": "REVIEWED_SOURCE_CONTRAST"},
            {"query_id": "q2", "annotation_state": "REVIEWED_SOURCE_CONTRAST"},
            {"query_id": "proposal", "annotation_state": "CURATION_PROPOSED"},
            {"query_id": "pending", "annotation_state": "PENDING"},
            {"query_id": "removed-case", "selected": False, "eligibility": "INELIGIBLE"},
        ],
        "cases": cases,
        "natural_controls": [
            {
                "query_id": "natural",
                "paper_id": "paper-c",
                "question": "not in this paper?",
                "conditions": {
                    "actual": {
                        "records": [
                            {
                                "source_id": "natural-source",
                                "text": "unrelated visible evidence",
                            }
                        ]
                    }
                },
            }
        ],
    }


def _valid(
    answer: str,
    cited: list[str],
    *,
    refused: bool = False,
    usage: dict | None = None,
    latency_ms: float | None = 10.0,
) -> dict:
    return {
        "status": "VALID",
        "answer": answer,
        "unanswerable": refused,
        "cited_source_ids": cited,
        "usage": usage,
        "latency_ms": latency_ms,
    }


def _answer(number: int, visible_id: str) -> dict:
    return _valid(f"answer {number}", [visible_id])


def _refusal() -> dict:
    return _valid("", [], refused=True)


def _rows(dataset: dict | None = None) -> dict:
    dataset = dataset or _dataset()
    return {
        "cases": [
            {
                "query_id": case["query_id"],
                "paper_id": case["paper_id"],
                "conditions": {
                    "actual": _answer(number, f"s{number}-actual"),
                    "sufficient": _answer(number, f"s{number}-support"),
                    "removed": _refusal(),
                },
            }
            for number, case in enumerate(dataset["cases"], start=1)
        ],
        "natural_controls": [
            {
                "query_id": "natural",
                "paper_id": "paper-c",
                "conditions": {"actual": _refusal()},
            }
        ],
    }


def test_missing_planned_row_or_arm_is_visible_and_scores_zero() -> None:
    dataset = _dataset()
    rows = _rows(dataset)
    rows["cases"].pop()
    result = score_diagnostic(dataset, rows)
    main = result["answer_path_main"]
    assert main["denominator"] == 2
    assert result["population"]["active_cases"] == 2
    assert result["population"]["main_planned_requests"] == 6
    assert main["cases"][1]["row_rejected"] is True
    assert main["cases"][1]["removed"]["status"] == "NOT_ATTEMPTED"
    assert main["conditions"]["removed"]["invalid_or_error"] == 1

    rows = _rows(dataset)
    rows["cases"][0]["conditions"].pop("removed")
    result = score_diagnostic(dataset, rows)
    first = result["answer_path_main"]["cases"][0]
    assert first["row_rejected"] is True
    assert first["removed"]["status"] == "NOT_ATTEMPTED"
    assert first["removed"]["correct_refusal"] is False

    rows = _rows(dataset)
    rows["cases"][0]["paper_id"] = "wrong-paper"
    first = score_diagnostic(dataset, rows)["answer_path_main"]["cases"][0]
    assert first["row_rejected"] is True
    assert first["actual"]["token_f1"] == 0


def test_invalid_output_is_zero_and_never_a_successful_refusal() -> None:
    dataset = _dataset()
    rows = _rows(dataset)
    rows["cases"][0]["conditions"]["removed"] = {
        **_refusal(),
        "status": "INVALID_OUTPUT",
    }
    result = score_diagnostic(dataset, rows)
    removed = result["answer_path_main"]["conditions"]["removed"]
    assert removed["correct_refusals"] == 1
    assert removed["invalid_or_error"] == 1
    assert result["answer_path_main"]["cases"][0]["removed"]["correct_refusal"] is False


def test_unknown_outcome_is_not_retried_and_remains_zero() -> None:
    dataset = _dataset()
    rows = _rows(dataset)
    rows["cases"][0]["conditions"]["removed"]["status"] = "UNKNOWN_OUTCOME"
    result = score_diagnostic(dataset, rows)
    removed = result["answer_path_main"]["conditions"]["removed"]
    assert removed["status_counts"]["UNKNOWN_OUTCOME"] == 1
    assert removed["correct_refusals"] == 1


def test_valid_refusal_requires_empty_answer_and_no_citations() -> None:
    dataset = _dataset()
    rows = _rows(dataset)
    rows["cases"][0]["conditions"]["actual"]["answer"] = ""
    rows["cases"][0]["conditions"]["actual"]["unanswerable"] = True
    rows["cases"][0]["conditions"]["actual"]["cited_source_ids"] = []
    result = score_diagnostic(dataset, rows)
    actual = result["answer_path_main"]["conditions"]["actual"]
    assert actual["valid_refusals"] == 1
    assert actual["token_f1"] == 0.5


def test_valid_status_with_citation_outside_view_is_not_valid() -> None:
    dataset = _dataset()
    rows = _rows(dataset)
    rows["cases"][0]["conditions"]["actual"]["cited_source_ids"] = ["another-paper"]
    result = score_diagnostic(dataset, rows)
    assert result["identifier_metrics"]["structure_invalid_valid_status"] == 1
    assert result["answer_path_main"]["cases"][0]["actual"]["valid"] is False


def test_removed_gold_answer_is_unsupported_and_gets_no_positive_credit() -> None:
    dataset = _dataset()
    rows = _rows(dataset)
    rows["cases"][0]["conditions"]["removed"] = _valid(
        "answer 1",
        ["s1-support"],
    )
    result = score_diagnostic(dataset, rows)
    removed = result["answer_path_main"]["conditions"]["removed"]
    paired = result["answer_path_main"]["paired"]
    assert removed["unsupported_nonrefusals"] == 1
    assert removed["correct_refusals"] == 1
    assert paired["actual_exact_and_removed_correct_refusal"] == 1
    assert paired["positive_exact_actual"] == 2


def test_constructed_strata_do_not_mix_with_natural_controls_or_controls_with_f1() -> None:
    dataset = _dataset()
    rows = _rows(dataset)
    rows["natural_controls"][0]["conditions"]["actual"] = _valid(
        "answer 1",
        ["natural-source"],
    )
    result = score_diagnostic(dataset, rows)
    main = result["answer_path_main"]
    controls = result["natural_controls"]
    assert main["denominator"] == 2
    assert controls["denominator"] == 1
    assert controls["valid_nonrefusals"] == 1
    assert "token_f1" not in controls
    assert controls["never_combined_with_constructed_negatives_or_answerable_f1"] is True


def test_unknown_usage_remains_unknown_instead_of_becoming_zero() -> None:
    dataset = _dataset()
    rows = _rows(dataset)
    rows["cases"][0]["conditions"]["actual"]["usage"] = {
        "input_tokens": 3,
        "output_tokens": 2,
        "total_tokens": 5,
    }
    result = score_diagnostic(dataset, rows)
    usage = result["usage"]
    assert usage["totals"]["input_tokens"] is None
    assert usage["unknown_counts"]["input_tokens"] == 6
    assert usage["latency_ms"]["n"] == 7
    assert usage["cost"] is None


def test_paper_cluster_interval_is_descriptive_and_not_significance_claim() -> None:
    dataset = _dataset()
    result = score_diagnostic(dataset, _rows(dataset))
    diagnostic = result["paper_cluster_diagnostics"]
    assert diagnostic["paper_groups"] == 2
    assert diagnostic["status"] == "DESCRIPTIVE_ONLY"
    assert diagnostic["no_p_values"] is True
    assert diagnostic["not_method_promotion"] is True


def test_paper_cluster_interval_is_withheld_when_arms_are_incomplete() -> None:
    dataset = _dataset()
    rows = _rows(dataset)
    rows["cases"][0]["conditions"]["actual"]["status"] = "INVALID_OUTPUT"
    result = score_diagnostic(dataset, rows)
    diagnostic = result["paper_cluster_diagnostics"]
    assert diagnostic["status"] == "INCOMPLETE_INPUTS"
    assert diagnostic["sufficient_minus_actual_token_f1"]["ci95_low"] is None


@pytest.mark.parametrize("stratum", ("cases", "natural_controls"))
def test_rows_must_not_mix_strata(stratum: str) -> None:
    dataset = _dataset()
    rows = _rows(dataset)
    misplaced = deepcopy(rows["cases" if stratum == "natural_controls" else "natural_controls"])
    rows[stratum].extend(misplaced)
    with pytest.raises(ValueError, match="unregistered|duplicate"):
        score_diagnostic(dataset, rows)
