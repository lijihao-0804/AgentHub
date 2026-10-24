from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from benchmarks.public_rag.consumer_report import score_rows

PROTOCOL_PATH = (
    Path(__file__).resolve().parents[2]
    / "benchmarks"
    / "public_rag"
    / "results"
    / "consumer-rag-20261023"
    / "protocol.json"
)


def _protocol() -> dict:
    if not PROTOCOL_PATH.is_file():
        pytest.skip("requires the local-only consumer experiment protocol")
    return json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))


def _arm(
    f1: float,
    *,
    recall: float,
    status: str = "VALID",
    usage: dict | None = None,
) -> dict:
    return {
        "api": {
            "status": status,
            "usage": usage,
            "request_ms": 10.0 if status != "NOT_ATTEMPTED" else None,
        },
        "token_f1": f1,
        "exact_match": int(f1 == 1.0),
        "evidence_recall": recall,
        "complete_evidence_set": int(recall == 1.0),
        "full_text_evidence_recall": recall,
        "final_evidence_bytes": 1000,
        "citation": {
            "validity": 1.0,
            "unknown": 0,
            "duplicates": 0,
            "has_citations": True,
        },
    }


def _fixture(*, aligned: bool = True) -> tuple[list[dict], dict, dict, dict]:
    protocol = _protocol()
    profiles = ["legacy5", "structured5", "structured10", "no_rag", "gold_budgeted"]
    if aligned:
        profiles.insert(1, "legacy5_aligned")
    usage = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
    rows = []
    for index in range(100):
        arms = {
            "legacy5": _arm(0.4, recall=0.8, usage=usage),
            "structured5": _arm(0.5, recall=0.8, usage=usage),
            "structured10": _arm(0.6, recall=0.8, usage=usage),
            "no_rag": _arm(1.0, recall=1.0, usage=usage),
            "gold_budgeted": _arm(1.0, recall=1.0, usage=usage),
        }
        if aligned:
            arms["legacy5_aligned"] = _arm(0.45, recall=0.85, usage=usage)
        rows.append(
            {
                "query_id": f"q{index}",
                "group_id": f"g{index % 20}",
                "hops": 2 + index % 3,
                "arms": arms,
            }
        )
    meta = {
        "protocol": protocol,
        "profiles": profiles,
        "queries": len(rows),
        "prepared": "/recorded/prepared",
    }
    completed = {"expected": len(rows), "queries": len(rows), "run_state": "COMPLETE"}
    return rows, meta, protocol, completed


def test_broken_baseline_cannot_manufacture_an_adoptable_gain() -> None:
    rows, meta, protocol, completed = _fixture()
    for row in rows[:6]:
        row["arms"]["legacy5"] = _arm(0, recall=0.8, status="INVALID_OUTPUT")
    report = score_rows(rows, meta=meta, protocol=protocol, completed=completed)
    assert report["selected_profile"] == "legacy5"
    assert not report["comparisons"]["structured5"]["gates"]["baseline_valid_api_fraction"][
        "passed"
    ]


def test_development_prefers_structured5_and_reports_aligned_diagnostic() -> None:
    rows, meta, protocol, completed = _fixture()

    report = score_rows(rows, meta=meta, protocol=protocol, completed=completed)

    assert report["selection"]["selected_profile"] == "structured5"
    assert report["selection"]["quality_adoptable"] is True
    assert set(report["comparisons"]) == {"structured5", "structured10"}
    assert report["aligned_diagnostic"]["status"] == "AVAILABLE"
    assert report["aligned_diagnostic"]["token_f1_difference"] == pytest.approx(0.05)
    assert report["aligned_diagnostic"]["record_recall_difference"] == pytest.approx(-0.05)
    assert report["aligned_diagnostic"]["used_for_selection"] is False
    assert report["aligned_diagnostic"]["significance_test"] is False
    assert report["arms"]["structured5"]["api"]["usage"]["prompt_tokens"] == {
        "sum": 1000,
        "observed_count": 100,
        "unknown_count": 0,
    }
    assert report["arms"]["structured5"]["api"]["request_latency_ms"]["p50"] == 10.0


def test_needs_attention_keeps_failed_question_and_refuses_quality_adoption() -> None:
    rows, meta, protocol, completed = _fixture()
    rows[0]["arms"]["structured5"] = _arm(
        0.0,
        recall=0.8,
        status="UNKNOWN_OUTCOME",
        usage=None,
    )
    completed["run_state"] = "NEEDS_ATTENTION"

    report = score_rows(rows, meta=meta, protocol=protocol, completed=completed)

    assert report["denominators"]["captured_questions"] == 100
    assert report["arms"]["structured5"]["metrics"]["token_f1"] == pytest.approx(0.495)
    assert report["arms"]["structured5"]["api"]["unknown_outcome_count"] == 1
    assert report["comparisons"]["structured5"]["gates"]["unknown_outcomes_zero"]["passed"] is False
    assert report["selection"]["selected_profile"] == "legacy5"
    assert report["selection"]["quality_adoptable"] is False


def test_confirmation_binds_protocol_and_aligned_is_optional() -> None:
    rows, meta, protocol, completed = _fixture(aligned=False)
    frozen = {
        "protocol": protocol,
        "dataset": "musique-ans",
        "selected_profile": "structured5",
        "development_run": "/recorded/development",
    }

    report = score_rows(
        rows,
        meta=meta,
        protocol=protocol,
        completed=completed,
        frozen=frozen,
    )

    assert report["selected_profile"] == "structured5"
    assert report["confirmation"] == {
        "frozen_profile": "structured5",
        "compared_with": "legacy5",
        "passed": True,
        "no_reselection": True,
        "confidence": 0.95,
    }
    assert set(report["comparisons"]) == {"structured5"}
    assert report["comparisons"]["structured5"]["ci"]["confidence"] == 0.95
    assert report["aligned_diagnostic"]["status"] == "SKIPPED_MISSING_PROFILE"

    mismatched = deepcopy(frozen)
    mismatched["protocol"]["seed"] += 1
    with pytest.raises(ValueError, match="protocol differs"):
        score_rows(
            rows,
            meta=meta,
            protocol=protocol,
            completed=completed,
            frozen=mismatched,
        )
