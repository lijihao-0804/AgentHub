"""Tests for the pure entity-delivery scorer."""

from __future__ import annotations

from copy import deepcopy

import pytest

from benchmarks.public_rag.entity_delivery_report import score_delivery


def _case(
    number: int,
    *,
    removed_status: str = "PARTIAL",
    supported: list[str] | None = None,
) -> dict:
    gold = "Alpha" if number % 2 else "Beta"
    supported = [gold] if supported is None and removed_status == "PARTIAL" else supported or []
    return {
        "query_id": f"q{number}",
        "paper_id": f"paper-{number}",
        "answer_references": [gold],
        "gold_entities": [{"value": gold, "aliases": [f"{gold} alias", gold.upper()]}],
        "conditions": {
            "full": {
                "records": [{"source_id": f"source-{number}"}],
                "expected_status": "ANSWERED",
                "supported_entities": [gold],
            },
            "removed": {
                "records": [{"source_id": f"removed-{number}"}],
                "expected_status": removed_status,
                "supported_entities": supported,
            },
        },
    }


def _dataset(count: int = 2) -> dict:
    return {
        "state": "FROZEN_DEVELOPMENT",
        "ledger": [{"proposal": number} for number in range(count)],
        "cases": [_case(number) for number in range(1, count + 1)],
    }


def _result(
    values: list[str],
    *,
    status: str = "VALID",
    delivery_status: str | None = "ANSWERED",
    explanation: str = "",
    usage: dict | None = None,
    latency_ms: float | None = 10.0,
) -> dict:
    return {
        "status": status,
        "delivery_status": delivery_status,
        "answer_values": values,
        "explanation": explanation,
        "cited_source_ids": [],
        "usage": usage,
        "latency_ms": latency_ms,
    }


def _row(case: dict) -> dict:
    gold = case["gold_entities"][0]["value"]
    supported = case["conditions"]["removed"]["supported_entities"]
    removed_status = case["conditions"]["removed"]["expected_status"]
    baseline_removed = (
        _result([], delivery_status="INSUFFICIENT")
        if not supported
        else _result([], delivery_status="INSUFFICIENT")
    )
    typed_removed = (
        _result([], delivery_status="INSUFFICIENT")
        if removed_status == "INSUFFICIENT"
        else _result(supported, delivery_status="PARTIAL")
    )
    return {
        "query_id": case["query_id"],
        "paper_id": case["paper_id"],
        "conditions": {
            "full": {
                "baseline": _result([gold]),
                "typed": _result([gold]),
            },
            "removed": {
                "baseline": baseline_removed,
                "typed": typed_removed,
            },
        },
    }


def _rows(dataset: dict) -> list[dict]:
    return [_row(case) for case in dataset["cases"]]


def test_full_uses_complete_alias_sets_short_values_and_auditable_per_case_data() -> None:
    dataset = _dataset()
    rows = _rows(dataset)
    rows[0]["conditions"]["full"]["baseline"] = _result(
        ["Alpha alias", "unexpected"],
        explanation="ignored explanation",
    )
    rows[0]["conditions"]["full"]["typed"] = _result(
        ["Alpha alias"],
        explanation="also ignored",
    )

    result = score_delivery(dataset, rows)
    first = result["cases"][0]
    baseline = first["conditions"]["full"]["baseline"]["metrics"]
    typed = first["conditions"]["full"]["typed"]["metrics"]

    assert baseline["entity_set_exact"] is False
    assert baseline["entity_false_positives"] == 1
    assert baseline["unexpected_values"] == ["unexpected"]
    assert typed["entity_set_exact"] is True
    assert typed["strict_primary_success"] is True
    assert typed["answer_exact_match"] == 0
    assert 0 < typed["answer_token_f1"] < 1
    assert first["answer_references"] == ["Alpha"]
    assert first["conditions"]["full"]["typed"]["result"]["explanation"] == "also ignored"

    rows[0]["conditions"]["full"]["typed"]["raw_content"] = "optional recorded provider output"
    assert score_delivery(dataset, rows)["cases"][0]["conditions"]["full"]["typed"]["valid"]
    rows[0]["conditions"]["full"]["typed"]["cited_source_ids"] = ["invisible-source"]
    assert not score_delivery(dataset, rows)["cases"][0]["conditions"]["full"]["typed"]["valid"]


def test_removed_separates_safe_refusal_safe_partial_and_wrong_full_delivery() -> None:
    dataset = _dataset(3)
    rows = _rows(dataset)
    rows[0]["conditions"]["removed"]["typed"] = _result(
        ["Alpha alias"],
        delivery_status="PARTIAL",
    )
    rows[1]["conditions"]["removed"]["typed"] = _result(
        ["unrelated"],
        delivery_status="PARTIAL",
    )
    rows[2]["conditions"]["removed"]["typed"] = _result(
        ["Alpha"],
        delivery_status="ANSWERED",
    )

    result = score_delivery(dataset, rows)
    first = result["cases"][0]["conditions"]["removed"]
    second = result["cases"][1]["conditions"]["removed"]["typed"]["metrics"]
    third = result["cases"][2]["conditions"]["removed"]["typed"]["metrics"]

    assert first["baseline"]["metrics"]["safe_refusal"] is True
    assert first["baseline"]["metrics"]["entity_value_coverage"] == 0
    assert first["typed"]["metrics"]["safe_partial_delivery"] is True
    assert first["typed"]["metrics"]["entity_value_coverage"] == 1
    assert first["typed"]["metrics"]["out_of_support_values"] == []
    assert second["safe_delivery"] is False
    assert second["wrong_full_delivery"] is True
    assert third["safe_delivery"] is False
    assert third["wrong_full_delivery"] is True


def test_missing_rows_conditions_and_invalid_results_remain_zero_in_planned_denominators() -> None:
    dataset = _dataset()
    rows = _rows(dataset)
    rows.pop()
    rows[0]["conditions"]["full"].pop("baseline")
    rows[0]["conditions"]["full"]["typed"] = _result(
        ["Alpha"],
        status="INVALID_OUTPUT",
    )
    rows[0]["conditions"]["removed"]["typed"] = {
        **_result([], delivery_status="INSUFFICIENT"),
        "status": "INVALID_OUTPUT",
    }

    result = score_delivery(dataset, rows)
    full = result["aggregate"]["conditions"]["full"]["arms"]["typed"]
    removed = result["aggregate"]["conditions"]["removed"]["arms"]["typed"]
    missing = result["cases"][1]

    assert full["planned"] == 2
    assert full["observed"] == 1
    assert full["invalid_or_unobserved"] == 2
    assert full["strict_primary_success_rate"] == 0
    assert full["entity_recall"] == 0
    assert removed["status_counts"]["INVALID_OUTPUT"] == 1
    assert removed["safe_delivery_rate"] == 0
    assert missing["row_present"] is False
    assert missing["conditions"]["full"]["typed"]["result"]["status"] == "NOT_ATTEMPTED"


def test_unknown_duplicate_and_mismatched_rows_are_rejected() -> None:
    dataset = _dataset()
    rows = _rows(dataset)

    duplicate = deepcopy(rows)
    duplicate.append(deepcopy(duplicate[0]))
    with pytest.raises(ValueError, match="unique"):
        score_delivery(dataset, duplicate)

    unknown = deepcopy(rows)
    unknown[0]["query_id"] = "not-planned"
    with pytest.raises(ValueError, match="unknown"):
        score_delivery(dataset, unknown)

    mismatched = deepcopy(rows)
    mismatched[0]["paper_id"] = "paper-wrong"
    with pytest.raises(ValueError, match="paper_id"):
        score_delivery(dataset, mismatched)


def test_usage_is_partially_known_and_cluster_ci_is_descriptive_only_when_complete() -> None:
    dataset = _dataset(8)
    rows = _rows(dataset)
    rows[0]["conditions"]["full"]["typed"]["usage"] = {
        "input_tokens": 5,
        "output_tokens": 3,
        "total_tokens": 8,
    }
    rows[1]["conditions"]["full"]["typed"]["latency_ms"] = None
    rows[0]["conditions"]["full"]["typed"]["usage"]["cached_tokens"] = 0

    result = score_delivery(dataset, rows)
    usage = result["aggregate"]["usage"]["conditions"]["full"]["typed"]
    diagnostic = result["aggregate"]["conditions"]["full"]["paper_cluster_diagnostics"]

    assert usage["known_subtotals"]["total_tokens"] == 8
    assert usage["totals"]["total_tokens"] is None
    assert usage["unknown_counts"]["total_tokens"] == 7
    assert usage["money"] is None
    assert diagnostic["status"] == "DESCRIPTIVE_ONLY"
    assert diagnostic["estimate"] == 0
    assert diagnostic["not_automatic_promotion"] is True
    assert result["aggregate"]["automatic_promotion"] is False

    rows[2]["conditions"]["full"].pop("typed")
    withheld = score_delivery(dataset, rows)["aggregate"]["conditions"]["full"][
        "paper_cluster_diagnostics"
    ]
    assert withheld["status"] == "MISSING_ARMS"
    assert withheld["ci95_low"] is None
