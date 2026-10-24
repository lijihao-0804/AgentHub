from __future__ import annotations

import copy
import json
import tempfile
import zipfile
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from benchmarks.public_rag import fiqa_confirmation as confirmation
from benchmarks.public_rag.fiqa_baseline import configuration, retrieval_identity


def _scratch() -> Iterator[Path]:
    root = Path(tempfile.gettempdir()) / "agenthub-fiqa-confirmation-fixtures"
    root.mkdir(parents=True, exist_ok=True)
    directory = root / uuid4().hex
    directory.mkdir()
    yield directory


def _frozen():
    if not confirmation.DEV_FROZEN.is_file():
        pytest.skip("requires the local-only frozen FiQA development selection")
    return json.loads(confirmation.DEV_FROZEN.read_text(encoding="utf-8"))


def _state():
    identifiers = [uuid4() for _ in range(5)]
    identity = retrieval_identity(configuration())
    state = {
        "dataset": "fiqa",
        "database": "agenthub_rag_scifact_20261021",
        "collection": "rag_fiqa_20261022",
        "user_id": str(identifiers[0]),
        "organization_id": str(identifiers[1]),
        "workspace_id": str(identifiers[2]),
        "knowledge_base_id": str(identifiers[3]),
        "kb": {"id": str(identifiers[3])},
        "snapshot_id": str(identifiers[4]),
        "snapshot_hash": "a" * 64,
        "documents": 2,
        "indexed_documents": 1,
        "chunk_sources": {"chunk": "doc"},
        "retrieval_identity": identity,
        "index_status": "COMPLETE",
    }
    return state


def _protocol(state):
    if not (
        confirmation.ROOT
        / "benchmarks/public_rag/results/fiqa-confirmation-20261022/method-protocol.json"
    ).is_file():
        pytest.skip("requires the local-only FiQA method protocol")
    protocol = json.loads(
        (
            confirmation.ROOT
            / "benchmarks/public_rag/results/fiqa-confirmation-20261022/method-protocol.json"
        ).read_text(encoding="utf-8")
    )
    protocol["snapshot_id"] = state["snapshot_id"]
    return protocol


def _patch_frozen(monkeypatch):
    frozen = _frozen()
    monkeypatch.setattr(confirmation, "_frozen", lambda: copy.deepcopy(frozen))
    monkeypatch.setattr(confirmation, "_measured_prompt", lambda: confirmation.SYSTEM_PROMPT)
    return frozen


def _append(path: Path, event: str) -> None:
    path.write_text(
        json.dumps(
            {
                "event": event,
                "dataset": "fiqa",
                "split": "test",
                "state_snapshot_id": uuid4(),
                "snapshot_hash": "a" * 64,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_recipe_rejects_each_drift_but_allows_fiqa_bindings(monkeypatch):
    _patch_frozen(monkeypatch)
    state = _state()
    protocol = _protocol(state)
    confirmation.validate_frozen_method(protocol)

    changed = copy.deepcopy(protocol)
    changed["model"] = "other"
    with pytest.raises(ValueError, match="model"):
        confirmation.validate_frozen_method(changed)

    changed = copy.deepcopy(protocol)
    changed["context_budget"]["max_retrieval_byte_units"] = 1
    with pytest.raises(ValueError, match="context_budget"):
        confirmation.validate_frozen_method(changed)

    changed = copy.deepcopy(protocol)
    changed["temperature"] = 1
    with pytest.raises(ValueError, match="temperature"):
        confirmation.validate_frozen_method(changed)

    changed = copy.deepcopy(protocol)
    del changed["selection_rule"]["max_unknown_outcomes"]
    with pytest.raises(ValueError, match="six"):
        confirmation.validate_frozen_method(changed)


@pytest.mark.parametrize("status", ["INVALID_OUTPUT", "UNKNOWN_OUTCOME", "VALID"])
def test_known_failure_continuation_keeps_fallback_and_never_accepts_unknown(status):
    tmp_path = next(_scratch())
    state = _state()
    protocol = _protocol(state)
    ranking = [{"chunk_id": "c", "document_id": "d", "score": -1.0}]
    baseline = [{"query_id": "q", "stages": {"rerank": {"ranking": ranking}}}]
    row = {
        "query_id": "q",
        "ordinal": 0,
        "status": status,
        "attempted": True,
        "http_status": 200,
        "response_model_verified": True,
        "error_type": "ValueError",
        "baseline_ranking": ranking,
        "final_ranking": ranking,
        "selection": [],
        "usage": {"total_tokens": 14},
    }
    meta = {
        "protocol": protocol,
        "snapshot_id": state["snapshot_id"],
        "snapshot_hash": state["snapshot_hash"],
        "model": confirmation.MODEL,
        "completed_queries": 1,
        "unknown_outcomes": 0,
        "run_state": "STOPPED",
    }
    (tmp_path / "run.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (tmp_path / "run.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    (tmp_path / "adapter-repair.json").write_text(
        json.dumps(
            {
                "repair": "allow_blank_title_in_promotion",
                "retry_failed_query": False,
            }
        ),
        encoding="utf-8",
    )
    if status == "INVALID_OUTPUT":
        assert confirmation.continued_first(tmp_path, protocol, state, baseline) == row
        row["final_ranking"] = []
        (tmp_path / "run.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
        with pytest.raises(ValueError):
            confirmation.continued_first(tmp_path, protocol, state, baseline)
    else:
        with pytest.raises(ValueError):
            confirmation.continued_first(tmp_path, protocol, state, baseline)


def test_prompt_drift_is_rejected(monkeypatch):
    _patch_frozen(monkeypatch)
    monkeypatch.setattr(confirmation, "_measured_prompt", lambda: "different")
    with pytest.raises(ValueError, match="SYSTEM_PROMPT"):
        confirmation.validate_frozen_method(_protocol(_state()))


def test_real_dev_and_fiqa_method_versions_pass_the_exact_recipe_boundary():
    state = _state()
    confirmation.validate_frozen_method(_protocol(state))


def test_measured_prompt_is_static_literal_without_execution(monkeypatch):
    path = Path.cwd() / f".fiqa-source-{uuid4()}.zip"
    source = "SYSTEM_PROMPT = 'literal'\nraise RuntimeError('must not execute')\n"
    try:
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(confirmation.DEV_ADAPTER, source)
        monkeypatch.setattr(confirmation, "DEV_SOURCE", path)
        assert confirmation._measured_prompt() == "literal"
    finally:
        path.unlink(missing_ok=True)


def test_state_uses_full_documents_for_parent_inventory_and_indexed_for_chunks():
    state = _state()
    identifiers = confirmation.validate_state(state)
    assert len(identifiers) == 5
    state["indexed_documents"] = 0
    with pytest.raises(ValueError, match="indexed"):
        confirmation.validate_state(state)


def test_baseline_requires_exact_split_and_candidate_set():
    scratch = next(_scratch())
    state = _state()
    meta = {
        "dataset": "fiqa",
        "split": "test",
        "queries": confirmation.QUERIES,
        "snapshot_id": state["snapshot_id"],
        "snapshot_hash": state["snapshot_hash"],
        "retrieval_identity": state["retrieval_identity"],
        "arm": "baseline",
    }
    rows = [
        {
            "query_id": f"q{index}",
            "ordinal": index,
            "stages": {
                "rerank": {"ranking": [{"chunk_id": "c", "document_id": "d", "score": 1.0}]},
                "selection": {"ranking": [{"chunk_id": "c", "document_id": "d", "score": 1.0}]},
            },
        }
        for index in range(1)
    ]
    # Count drift is rejected before any ranking details are trusted.
    path = scratch / "baseline.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    with pytest.raises(ValueError, match="648"):
        confirmation._baseline(path, meta, state)


def test_source_helper_uses_frozen_seed_and_patched_blank_title_helper(monkeypatch):
    state = {"chunk_sources": {"c": "d"}}
    corpus = {"d": SimpleNamespace(title="")}
    calls = []

    def sections(ranking, *, chunks, source_mapping, titles):
        calls.append((ranking, source_mapping, titles))
        return [
            {
                "section_id": 0,
                "source_id": "d",
                "title": "",
                "content": "text",
                "chunk_ids": ["c"],
            }
        ]

    monkeypatch.setattr(confirmation, "build_sections", sections)
    rows = [
        {
            "query_id": f"q{index}",
            "stages": {
                "rerank": {"ranking": [{"chunk_id": "c", "document_id": "d", "score": 1.0}]}
            },
        }
        for index in range(5)
    ]
    first = confirmation._source(state, corpus, rows, {"c": object()})[0]
    second = confirmation._source(state, corpus, rows, {"c": object()})[0]
    assert first == second
    assert set(first) == {"q0", "q1", "q2", "q3", "q4"}
    assert calls[0][2] == {"d": ""}


def test_report_transformation_always_keeps_dev_arm_and_marks_confirmation_scope():
    frozen = _frozen()
    gates = {name: {"passed": True, "observed": 1} for name in confirmation.GATE_KEYS}
    report = {
        "selection": {"selected_arm": "baseline", "gates": gates},
        "provider_usage": {"attempted_query_count": confirmation.QUERIES},
        "scope": {"split": "test"},
    }
    actual = confirmation.confirmation(report, frozen)
    assert actual["frozen_arm"] == "onyx_selection"
    assert set(actual["confirmation"]) == {"passed", "checks"}
    assert actual["confirmation"]["passed"] is True
    assert actual["scope"]["no_test_reselection"] is True
    assert actual["scope"]["repeated_dev_exploratory"] is False
    assert "FiQA test" in actual["limitations"][0]
    assert actual["default_change"]["default_changed"] is False
    assert actual["actual_usage"]["attempted_query_count"] == confirmation.QUERIES


def test_transformation_never_rewrites_dev_frozen_or_serves_as_test_selection(monkeypatch):
    frozen = _frozen()
    report = {
        "selection": {"selected_arm": "baseline", "gates": {}},
        "scope": {"split": "test"},
    }
    with pytest.raises(ValueError, match="six gates"):
        confirmation.confirmation(report, frozen)

    frozen = _patch_frozen(monkeypatch)
    frozen["selected_arm"] = "baseline"
    report["selection"] = {
        "selected_arm": "onyx_selection",
        "gates": {name: {"passed": True} for name in confirmation.GATE_KEYS},
    }
    actual = confirmation.confirmation(report, frozen)
    assert actual["frozen_arm"] == "baseline"
    assert "selection" not in actual


def test_baseline_meta_requires_exact_snapshot_identity_and_test_split():
    scratch = next(_scratch())
    state = _state()
    meta = {
        "dataset": "fiqa",
        "split": "test",
        "queries": confirmation.QUERIES,
        "snapshot_id": state["snapshot_id"],
        "snapshot_hash": state["snapshot_hash"],
        "retrieval_identity": state["retrieval_identity"],
        "arm": "baseline",
    }
    path = scratch / "baseline.jsonl"
    path.write_text("", encoding="utf-8")
    for field, value in (
        ("split", "train"),
        ("snapshot_hash", "b" * 64),
        ("retrieval_identity", {}),
        ("arm", "candidate"),
    ):
        changed = {**meta, field: value}
        with pytest.raises(ValueError, match="metadata"):
            confirmation._baseline(path, changed, state)


def test_score_access_event_precedes_contract_and_label_load(monkeypatch):
    scratch = next(_scratch())
    _patch_frozen(monkeypatch)
    state = _state()
    protocol = _protocol(state)
    protocol_path = scratch / "protocol.json"
    state_path = scratch / "state.json"
    protocol_path.write_text(json.dumps(protocol), encoding="utf-8")
    state_path.write_text(json.dumps(state), encoding="utf-8")
    data = scratch / "output"
    data.mkdir()
    data_root = scratch / "labels"
    data_root.mkdir()
    run_meta = {
        "protocol": protocol,
        "dataset": "fiqa",
        "split": "test",
        "queries": 648,
        "snapshot_id": state["snapshot_id"],
        "snapshot_hash": state["snapshot_hash"],
        "model": confirmation.MODEL,
        "source": confirmation.DEV_SOURCE.relative_to(confirmation.ROOT).as_posix(),
        "evaluation_role": "independent_confirmation",
        "pairing": "cached baseline with actual incremental model calls",
        "generation_task": "retrieval selection only",
        "completed_queries": 648,
        "unknown_outcomes": 0,
        "run_state": "COMPLETED",
    }
    (data / "run.meta.json").write_text(json.dumps(run_meta), encoding="utf-8")
    (data / "run.jsonl").write_text("", encoding="utf-8")
    observed = []

    def load(*_args, **_kwargs):
        observed.append((data / "label-access.jsonl").exists())
        return SimpleNamespace(
            qrels={f"q{index}": {"d": 1} for index in range(648)},
            corpus={},
            queries={},
        )

    monkeypatch.setattr(confirmation, "load_beir", load)
    monkeypatch.setattr(
        confirmation,
        "score_rows",
        lambda *_args, **_kwargs: pytest.fail("scorer requires a complete fake contract"),
    )
    args = SimpleNamespace(
        protocol=protocol_path,
        state=state_path,
        data=scratch / "unused",
        data_root=data_root,
        output=data,
    )
    with pytest.raises(pytest.fail.Exception, match="complete fake contract"):
        confirmation.asyncio.run(confirmation._score(args))
    assert observed == [True]
    event = json.loads((data / "label-access.jsonl").read_text(encoding="utf-8"))
    assert event["event"] == "INDEPENDENT_TEST_SCORING_LABEL_ACCESS"
