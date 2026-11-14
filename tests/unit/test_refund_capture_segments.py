"""Offline receipt merge regression: two execution segments and a shared wire ledger."""

from copy import deepcopy

import pytest

from benchmarks.public_rag import refund_capture_segments as segments


def test_merge_segments_and_reject_duplicated_run(tmp_path, monkeypatch):
    first, tail = tmp_path / "first", tmp_path / "tail"
    first.mkdir()
    tail.mkdir()
    dev = [{"case_id": f"S0-{i:03}", "input": str(i)} for i in range(1, 51)]
    monkeypatch.setattr(segments, "reviewed_cases", lambda: dev)
    new = segments.read(segments.FIXTURES / "new_ticket_cases.json")["cases"]
    cases = dev + new
    schedule = {c["case_id"]: "refund_dev" if c in dev else "refund_new" for c in cases}
    group = {
        "version": {"id": "v", "workspace_id": "w", "resolved_spec_hash": "spec"},
        "snapshot": {
            "id": "s",
            "workspace_id": "w",
            "knowledge_base_id": "kb",
            "content_hash": "h",
        },
        "documents": {},
    }
    report = {
        "db_name": "db",
        "workspace_id": "w",
        "mode": "paid",
        "schedule": schedule,
        "groups": {"refund_dev": group},
        "code_commit": "A",
        "python_source_sha256": "H",
        "rows": [],
    }
    continuation = deepcopy(report)
    continuation.update(groups={"refund_new": group}, code_commit="B", python_source_sha256="I")
    wires = []
    for i, case in enumerate(cases):
        key = case["case_id"]
        wire = {
            "id": i + 1,
            "stage": schedule[key],
            "task": key,
            "provider_id": str(i),
            "status": "returned",
            "input": 2,
            "output": 1,
        }
        wires.append(wire)
        record = dict(
            case_id=key,
            stage=schedule[key],
            expected=case,
            run_id=key,
            thread_id=key,
            calls=[{"response": {"usage": {"input_tokens": 2, "output_tokens": 1}}}],
            semantic_verdict="RUN_ERROR",
            physical_attempts=[wire],
            run={
                "id": key,
                "workspace_id": "w",
                "agent_version_id": "v",
                "resolved_spec_hash": "spec",
                "effective_knowledge_snapshots": [
                    {"snapshot_id": "s", "snapshot_hash": "h", "knowledge_base_id": "kb"}
                ],
            },
            turn={"thread_id": key, "agent_run_id": key},
        )
        if key == "S0-038":
            del record["run"], record["turn"]
        dest, segment = (first, report) if i < 38 else (tail, continuation)
        segments.save(dest / (key + ".json"), record)
        segment["rows"].append({k: record.get(k) for k in segments.ROW_KEYS})
    segments.save(first / "report.json", report)
    continuation.update(
        prior_report_sha256=segments.sha(first / "report.json"),
        original_commit="A",
        original_python_source_sha256="H",
    )
    segments.save(tail / "report.json", continuation)
    monkeypatch.setattr(
        segments,
        "reconcile",
        lambda _: {"wires": wires, "stages": dict.fromkeys(schedule.values(), {})},
    )
    target = tmp_path / "merged"
    segments.combine(first, tail, None, target)
    result = segments.read(target / "report.json")
    assert [r["case_id"] for r in result["rows"]] == list(schedule)
    assert result["rows"][37]["semantic_verdict"] == "RUN_ERROR"
    assert [s["code_commit"] for s in result["capture_segments"]] == ["A", "B"]
    with pytest.raises(ValueError, match="new directory"):
        segments.combine(first, tail, None, target)
    path = tail / "S0-039.json"
    data = segments.read(path)
    data["run_id"] = continuation["rows"][0]["run_id"] = "S0-038"
    path.unlink()
    (tail / "report.json").unlink()
    segments.save(path, data)
    segments.save(tail / "report.json", continuation)
    with pytest.raises(ValueError, match="duplicate Run"):
        segments.combine(first, tail, None, tmp_path / "invalid")
    assert not (tmp_path / "invalid").exists()
