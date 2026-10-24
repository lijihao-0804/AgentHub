from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from benchmarks.public_rag import scifact_runner as runner


@pytest.mark.asyncio
async def test_test_labels_cannot_be_opened_before_dev_selection(tmp_path, monkeypatch):
    state = tmp_path / "state.json"
    runner.dump(
        state,
        {
            "database": runner.DATABASE,
            "collection": runner.COLLECTION,
            "snapshot_id": "snapshot-a",
        },
    )
    args = SimpleNamespace(
        state=state,
        output=tmp_path / "run.jsonl",
        split="test",
        limit=0,
        frozen=None,
        rrf_k=60,
    )

    def forbidden_load(*_):
        pytest.fail("test labels were accessed before the freeze guard")

    monkeypatch.setattr(runner, "load_scifact", forbidden_load)
    with pytest.raises(ValueError, match="frozen DEV selection"):
        await runner.run(args)


@pytest.mark.asyncio
async def test_holdout_access_is_recorded_before_loader_can_fail(tmp_path, monkeypatch):
    state, frozen = tmp_path / "state.json", tmp_path / "frozen.json"
    runner.dump(
        state,
        {
            "database": runner.DATABASE,
            "collection": runner.COLLECTION,
            "snapshot_id": "snapshot-a",
            "retrieval_identity": {},
        },
    )
    runner.dump(
        frozen,
        {
            "snapshot_id": "snapshot-a",
            "baseline_implementation": "legacy_revision_in",
            "selected_implementation": "snapshot_membership_join",
            "rrf_k": 60,
            "seed": 20261021,
            "controls": {},
        },
    )
    args = SimpleNamespace(
        state=state,
        frozen=frozen,
        output=tmp_path / "run.jsonl",
        split="test",
        limit=0,
        rrf_k=60,
        seed=20261021,
        data=tmp_path / "fixture",
    )

    def failing_loader(*_):
        exposure = json.loads((tmp_path / "test-exposures.jsonl").read_text())
        assert exposure["event"] == "TEST_LABEL_ACCESS"
        assert exposure["selection"]["snapshot_id"] == "snapshot-a"
        raise ValueError("fixture loader failed")

    monkeypatch.setattr(runner, "load_scifact", failing_loader)
    with pytest.raises(ValueError, match="fixture loader failed"):
        await runner.run(args)
