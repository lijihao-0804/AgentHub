"""Unit tests for large tool output offloading and paginated slice readback."""

from __future__ import annotations

import json

from packages.agent_runtime.tool_offload import (
    READ_TOOL_ARTIFACT,
    maybe_offload_tool_output,
    read_offloaded_slice,
    read_tool_artifact_definition,
)


def test_small_or_rag_outputs_are_not_offloaded() -> None:
    small = maybe_offload_tool_output(
        tool_name="run_sql",
        tool_call_id="c1",
        serialized_content='{"rows": [1, 2, 3]}',
        threshold_bytes=1024,
    )
    assert small.offloaded is False
    assert small.inline_content == '{"rows": [1, 2, 3]}'

    big_rag = maybe_offload_tool_output(
        tool_name="search_knowledge",
        tool_call_id="c2",
        serialized_content="x" * 20_000,
        threshold_bytes=1024,
    )
    assert big_rag.offloaded is False


def test_large_tool_output_offloads_and_paginates_cleanly() -> None:
    payload = "\n".join(f"row_{i}: value={i * 10}" for i in range(400))
    res = maybe_offload_tool_output(
        tool_name="run_sql",
        tool_call_id="call-42",
        serialized_content=payload,
        threshold_bytes=1024,
        preview_chars=120,
    )
    assert res.offloaded is True
    assert res.artifact_ref == "offload-call-42"
    assert res.stored_entry is not None

    stub = json.loads(res.inline_content)
    assert stub["offloaded"] is True
    assert stub["artifact_ref"] == "offload-call-42"
    assert stub["total_lines"] == 400
    assert len(stub["preview"]) == 120
    assert READ_TOOL_ARTIFACT in stub["hint"]

    store = {res.artifact_ref: res.stored_entry}
    page1 = read_offloaded_slice(
        store, {"artifact_ref": res.artifact_ref, "offset": 0, "limit": 500}
    )
    assert page1["has_more"] is True
    assert page1["next_offset"] == 500
    assert page1["content"] == payload[:500]

    page_end = read_offloaded_slice(
        store,
        {"artifact_ref": res.artifact_ref, "offset": len(payload) - 100, "limit": 500},
    )
    assert page_end["has_more"] is False
    assert page_end["next_offset"] is None
    assert page_end["content"] == payload[-100:]


def test_read_offloaded_slice_validates_errors_and_tool_definition() -> None:
    defn = read_tool_artifact_definition()
    assert defn.identity == READ_TOOL_ARTIFACT
    assert defn.effect.value == "READ"

    missing = read_offloaded_slice({}, {"artifact_ref": "offload-none"})
    assert missing["error_code"] == "ARTIFACT_NOT_FOUND"

    bad_offset = read_offloaded_slice(
        {"offload-1": {"content": "abc", "tool": "run_sql"}},
        {"artifact_ref": "offload-1", "offset": -5},
    )
    assert bad_offset["error_code"] == "INVALID_ARGUMENT"
