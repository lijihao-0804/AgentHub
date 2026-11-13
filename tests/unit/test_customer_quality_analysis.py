"""Target contracts for the T04 customer-quality offline analysis."""

from __future__ import annotations

import json
from typing import Any

import pytest

from benchmarks.evaluation.experiments.customer_quality_20261105.analysis import (
    analyze_preference_biases,
    analyze_trials,
    build_blind_annotation_pack,
    calibrate_judge,
)


def _row(
    case_key: str,
    variant: str,
    repetition: int | None,
    *,
    source_group_id: str = "source-1",
    split: str = "dev",
    status: str = "SUCCEEDED",
    task_success: bool | None = True,
    semantic_score: float | None = 1.0,
    cost_estimate_cny: float | None = 0.1,
    input_tokens: int | None = 10,
    output_tokens: int | None = 5,
    latency_ms: float | None = 100.0,
    input_hash: str | None = "input-hash",
    output_hash: str | None = "output-hash",
    **extra: object,
) -> dict[str, Any]:
    return {
        "case_key": case_key,
        "source_group_id": source_group_id,
        "split": split,
        "variant": variant,
        "repetition": repetition,
        "status": status,
        "task_success": task_success,
        "semantic_score": semantic_score,
        "cost_estimate_cny": cost_estimate_cny,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "latency_ms": latency_ms,
        "input_hash": input_hash,
        "output_hash": output_hash,
        **extra,
    }


def test_zero_is_known_while_null_and_unknown_status_remain_unknown() -> None:
    rows = [
        _row(
            "zero",
            "baseline",
            0,
            semantic_score=0.0,
            cost_estimate_cny=0.0,
            input_tokens=0,
            output_tokens=0,
            latency_ms=0.0,
        ),
        _row(
            "unknown",
            "baseline",
            0,
            status="UNKNOWN_OUTCOME",
            task_success=None,
            semantic_score=None,
            cost_estimate_cny=None,
            input_tokens=None,
            output_tokens=None,
            latency_ms=None,
        ),
    ]

    result = analyze_trials(rows, k=1)["variants"]["baseline"]

    assert result["coverage"]["task_success"]["known_count"] == 1
    assert result["coverage"]["task_success"]["unknown_count"] == 1
    assert result["coverage"]["semantic_score"]["known_count"] == 1
    assert result["business_success"]["success_rate"] == 1
    assert result["observed_totals"]["cost_estimate_cny"]["sum"] == 0
    assert result["observed_totals"]["input_tokens"]["sum"] == 0
    assert result["status_counts"] == {"SUCCEEDED": 1, "UNKNOWN_OUTCOME": 1}


def test_missing_repetition_cannot_become_a_complete_case() -> None:
    rows = [
        _row("case-a", "baseline", 0),
        _row("case-a", "baseline", None),
    ]

    variant = analyze_trials(rows, k=2)["variants"]["baseline"]

    assert variant["coverage"]["task_success"]["known_count"] == 2
    assert variant["coverage"]["repetition"]["unknown_count"] == 1
    assert variant["complete_case_reliability"]["complete_case_count"] == 0
    assert variant["complete_case_reliability"]["pass_hat_k"] is None
    assert variant["literal_consistency"]["status"] == "NOT_AVAILABLE"


def test_repetitions_of_one_case_are_not_independent_pairing_units() -> None:
    rows = [
        _row("case-a", "baseline", repetition)
        | {"task_success": False}
        for repetition in range(2)
    ]
    rows += [
        _row("case-a", "candidate", repetition)
        | {"task_success": True}
        for repetition in range(2)
    ]
    rows += [
        _row("case-b", "baseline", repetition)
        | {"task_success": False}
        for repetition in range(2)
    ]
    rows += [
        _row("case-b", "candidate", repetition)
        | {"task_success": False}
        for repetition in range(2)
    ]

    paired = analyze_trials(
        rows,
        k=2,
        baseline_variant="baseline",
        candidate_variant="candidate",
        bootstrap_count=200,
        seed=17,
    )["paired"]["metrics"]["business_pass_hat_k"]

    assert paired["unit"] == "case"
    assert paired["case_count"] == 2
    assert paired["source_group_count"] == 1
    assert paired["estimate"] == pytest.approx(0.5)
    assert paired["interval"] == {"lower": pytest.approx(0.5), "upper": pytest.approx(0.5)}


def test_unpaired_cases_keep_each_missing_data_reason() -> None:
    rows = [
        _row("paired", "baseline", repetition) for repetition in range(2)
    ]
    rows += [
        _row("paired", "candidate", repetition) for repetition in range(2)
    ]
    rows += [
        _row("no-candidate", "baseline", repetition) for repetition in range(2)
    ]
    rows += [
        _row("short-candidate", "baseline", repetition) for repetition in range(2)
    ]
    rows += [_row("short-candidate", "candidate", 0)]
    rows += [
        _row("unknown-candidate", "baseline", repetition) for repetition in range(2)
    ]
    rows += [
        _row(
            "unknown-candidate",
            "candidate",
            repetition,
            status="UNKNOWN_OUTCOME",
            task_success=None,
        )
        for repetition in range(2)
    ]

    paired = analyze_trials(
        rows,
        k=2,
        baseline_variant="baseline",
        candidate_variant="candidate",
        bootstrap_count=200,
        seed=3,
    )["paired"]["metrics"]["business_pass_hat_k"]

    assert paired["status"] == "AVAILABLE"
    assert paired["case_count"] == 1
    assert paired["unpaired_reasons"]["candidate_variant_missing"] == ["no-candidate"]
    assert paired["unpaired_reasons"]["candidate:incomplete_repetitions"] == [
        "short-candidate"
    ]
    assert paired["unpaired_reasons"]["candidate:unknown_task_success"] == [
        "unknown-candidate"
    ]


def test_blind_pack_removes_variant_and_model_identity_without_fake_bias_runs() -> None:
    rows = [
        _row(
            "case-a",
            "candidate-model",
            0,
            model="model-secret",
            provider="provider-secret",
            answer_text="candidate-model from model-secret via provider-secret",
        ),
        _row(
            "case-a",
            "baseline-model",
            0,
            answer_text="baseline-model answer",
        ),
    ]

    first = build_blind_annotation_pack(rows, seed=9)
    second = build_blind_annotation_pack(rows, seed=9)
    encoded = json.dumps(first, ensure_ascii=False)

    assert first == second
    assert "candidate-model" not in encoded
    assert "baseline-model" not in encoded
    assert "model-secret" not in encoded
    assert "provider-secret" not in encoded
    assert all("variant" not in item and "model" not in item for item in first["items"])
    assert analyze_preference_biases()["position_swap"]["status"] == "NOT_RUN"
    assert analyze_preference_biases()["length_preference"]["status"] == "NOT_RUN"


def test_judge_calibration_reports_confusion_kappa_and_reference_boundary() -> None:
    result = calibrate_judge(
        ["FAIL", "FAIL", "PARTIAL", "PASS", "PASS", "PARTIAL"],
        ["FAIL", "PARTIAL", "PARTIAL", "PASS", "PASS", "PARTIAL"],
        label_order=["FAIL", "PARTIAL", "PASS"],
    )

    assert result["annotation_source"] == "assistant_authored_reference"
    assert result["source_boundary"]["reference_is_human"] is False
    assert result["source_boundary"]["human_annotation_claim"] is False
    assert result["confusion_matrix"]["FAIL"]["FAIL"] == 1
    assert result["confusion_matrix"]["FAIL"]["PARTIAL"] == 1
    assert result["raw_agreement"] == pytest.approx(5 / 6)
    assert result["weighted_kappa_status"] == "AVAILABLE"
    assert isinstance(result["weighted_kappa"], float)
