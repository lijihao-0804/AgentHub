"""Unit tests for in-run micro-compaction of stale tool observations."""

from __future__ import annotations

import json

from packages.agent_runtime.micro_compact import micro_compact_tool_observations
from packages.model_gateway.contracts import ModelMessage


def _tool_msg(name: str, call_id: str, body: str) -> ModelMessage:
    return ModelMessage(role="tool", name=name, tool_call_id=call_id, content=body)


def test_micro_compact_skips_below_pressure_trigger_to_preserve_kv_cache() -> None:
    msgs = [
        ModelMessage(role="system", content="sys"),
        _tool_msg("run_sql", "c1", json.dumps({"status": "ok", "rows": "x" * 2000})),
        _tool_msg("run_sql", "c2", json.dumps({"status": "ok", "rows": "y" * 2000})),
        _tool_msg("run_sql", "c3", json.dumps({"status": "ok", "rows": "z" * 2000})),
        _tool_msg("run_sql", "c4", json.dumps({"status": "ok", "rows": "w" * 2000})),
    ]
    out, report = micro_compact_tool_observations(
        msgs,
        estimated_input_tokens=4000,
        available_input_tokens=10000,
        pressure_trigger_ratio=0.75,
        keep_recent_tool_messages=2,
    )
    assert report.triggered is False
    assert report.compacted_count == 0
    assert out == msgs


def test_micro_compact_folds_only_stale_non_rag_tool_results_under_high_pressure() -> None:
    rag_payload = json.dumps(
        {"evidence_format": "compact_v1", "results": [{"id": "R1", "t": "a" * 1500}]}
    )
    stale_sql = json.dumps(
        {"status": "SUCCESS", "artifact_ref": "offload-c1", "data": "b" * 1500}
    )
    recent_sql_1 = json.dumps({"status": "SUCCESS", "data": "c" * 1500})
    recent_sql_2 = json.dumps({"status": "SUCCESS", "data": "d" * 1500})

    msgs = [
        ModelMessage(role="system", content="sys"),
        _tool_msg("search_knowledge", "rag-1", rag_payload),
        _tool_msg("run_sql", "c1", stale_sql),
        _tool_msg("run_sql", "c2", recent_sql_1),
        _tool_msg("run_sql", "c3", recent_sql_2),
    ]
    out, report = micro_compact_tool_observations(
        msgs,
        estimated_input_tokens=8500,
        available_input_tokens=10000,
        pressure_trigger_ratio=0.75,
        keep_recent_tool_messages=2,
        min_chars_to_compact=500,
    )
    assert report.triggered is True
    assert report.compacted_count == 1
    assert report.saved_chars > 1000
    # RAG evidence is never mutated
    assert out[1].content == rag_payload
    # Stale non-RAG tool result is folded with status & artifact_ref preserved
    folded = json.loads(out[2].content)
    assert folded["micro_compacted"] is True
    assert folded["tool"] == "run_sql"
    assert folded["status"] == "SUCCESS"
    assert folded["artifact_ref"] == "offload-c1"
    # Most recent 2 tool results stay intact
    assert out[3].content == recent_sql_1
    assert out[4].content == recent_sql_2
