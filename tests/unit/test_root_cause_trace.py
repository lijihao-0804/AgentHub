from __future__ import annotations

from pathlib import Path

import pytest

from benchmarks.public_rag.root_cause_trace import (
    UNKNOWN,
    OutputExistsError,
    _arm,
    _build_source_index,
    _prepare_gold,
    _source_id_disappearance,
    replay,
)


def test_non_monotonic_stage_reports_id_loss_and_recovery_is_unknown() -> None:
    stages = {
        name: {"source_ids": ids}
        for name, ids in {
            "dense": ["a", "b"],
            "sparse": ["c"],
            "candidate_union": ["a", "b", "c"],
            "fusion": ["a", "c"],
            "selection": ["a"],
            "rerank": ["a"],
            "final_evidence": ["a"],
            "model_snippets": ["a"],
        }.items()
    }

    loss = _source_id_disappearance(stages)

    assert loss["first_disappearance_stage"] == "fusion"
    assert loss["disappeared_source_ids"] == ["b"]
    assert loss["possible_recovery_stage"] is None
    assert loss["not_evidence_loss"] is True


def test_unattempted_row_keeps_unknown_stages_and_does_not_claim_reading_failure() -> None:
    text = "Alpha evidence."
    case = {
        "query_id": "q1",
        "paper_id": "p1",
        "question": "Which evidence?",
        "consensus": "answerable",
        "annotations": [
            {
                "answer_type": "extractive",
                "answer": "alpha",
                "evidence": [text],
            }
        ],
    }
    source_map = {
        "c1": {
            "paper_id": "p1",
            "arm": "fixed",
            "chunk_text": text,
            "paragraphs": [{"key": "p1:1", "text": text}],
        },
        "other-arm": {
            "paper_id": "p1",
            "arm": "structural",
            "chunk_text": "Other policy.",
            "paragraphs": [{"key": "p1:2", "text": "Other policy."}],
        },
    }
    result = {
        "run_status": "NOT_ATTEMPTED",
        "failure_code": None,
        "answer": None,
        "visible_records": [],
        "retrievals": [],
        "model_calls": [],
        "physical_outcomes": [],
        "run_usage": {},
        "tool_calls": 0,
        "latency_ms": None,
        "workflow_valid": False,
    }
    sources = _build_source_index(source_map)

    payload = _arm(
        paper_id="p1",
        result=result,
        query_id="q1",
        gold=_prepare_gold(case, sources),
        sources=sources,
        consensus="answerable",
        messages=None,
        prepared_version=None,
        official={"run_status": "NOT_ATTEMPTED", "token_f1": 0.0, "exact_match": 0},
        source_map=source_map,
        arm_name="fixed",
    )

    assert payload["stages"]["dense"]["status"] == UNKNOWN
    assert payload["stages"]["model_snippets"]["status"] == UNKNOWN
    assert (
        payload["visibility_by_stage"]["model_snippets"]["strict_original_text_visible_rate"]
        is None
    )
    assert payload["actual_model_messages"] is None
    assert payload["stages"]["runtime_admission"]["status"] == UNKNOWN
    assert payload["annotation_substring_proxy_loss"]["first_coverage_drop_stage"] is None
    assert payload["semantic_correctness"] == UNKNOWN
    assert payload["stages"]["chunk"]["source_ids"] == ["c1"]


def test_existing_output_is_rejected_without_reading_inputs() -> None:
    output = Path("benchmarks/public_rag/results")

    with pytest.raises(OutputExistsError):
        replay(Path("missing-source"), "live-run-2", output)
