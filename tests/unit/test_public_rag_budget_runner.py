"""Reject mismatched experiments before exposing evaluation labels or loading models."""

import json
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

import pytest

from benchmarks.public_rag.budget_ablation import ARMS, run


@pytest.mark.asyncio
async def test_mismatched_protocol_is_rejected_before_label_access(tmp_path: Path) -> None:
    protocol = tmp_path / "protocol.json"
    protocol.write_text(json.dumps({"arms": {}, "final_top_k": 20}), encoding="utf-8")
    with patch("benchmarks.public_rag.budget_ablation.load_scifact") as loader:
        with pytest.raises(ValueError, match="declared protocol"):
            await run(Namespace(protocol=protocol))
    loader.assert_not_called()


@pytest.mark.asyncio
async def test_regression_requires_frozen_selection_before_labels(tmp_path: Path) -> None:
    protocol = tmp_path / "protocol.json"
    protocol.write_text(
        json.dumps({"arms": ARMS, "final_top_k": 20, "snapshot_id": "snapshot"}), encoding="utf-8"
    )
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"snapshot_id": "snapshot"}), encoding="utf-8")
    with patch("benchmarks.public_rag.budget_ablation.load_scifact") as loader:
        with pytest.raises(ValueError, match="frozen DEV selection"):
            await run(Namespace(protocol=protocol, state=state, phase="regression", frozen=None))
    loader.assert_not_called()
