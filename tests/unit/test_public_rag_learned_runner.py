"""Experimental index identity and pre-label access guards."""

import json
from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from benchmarks.public_rag.learned_sparse_adapter import SharedDenseTreatmentIndex
from benchmarks.public_rag.learned_sparse_runner import run, validate_protocol
from packages.knowledge.contracts import SparseEncoding, VectorScope, VectorSearchHit


def _protocol() -> dict:
    if not Path("benchmarks/public_rag/results/learned-sparse-20261022/protocol.json").is_file():
        pytest.skip("requires the local-only learned-sparse experiment protocol")
    return json.loads(
        Path("benchmarks/public_rag/results/learned-sparse-20261022/protocol.json").read_text(
            encoding="utf-8"
        )
    )


@pytest.mark.parametrize(
    "field,value", [("collection", "rag_scifact_20261021"), ("modifier", "idf")]
)
def test_learned_profile_rejects_legacy_index_semantics(field: str, value: str) -> None:
    protocol = _protocol()
    protocol["arms"]["learned"][field] = value
    with pytest.raises(ValueError):
        validate_protocol(protocol, {"snapshot_id": protocol["snapshot_id"]})


@pytest.mark.asyncio
async def test_learned_regression_requires_freeze_before_reading_labels(tmp_path: Path) -> None:
    protocol = _protocol()
    state = {"chunks": 2}
    (tmp_path / "prepared-index.json").write_text(
        json.dumps({"protocol": protocol, "chunks": 2}), encoding="utf-8"
    )
    with patch("benchmarks.public_rag.learned_sparse_runner.load_scifact") as loader:
        with pytest.raises(ValueError, match="frozen DEV selection"):
            await run(Namespace(output=tmp_path, action="regression", frozen=None), protocol, state)
    loader.assert_not_called()


def test_sparse_treatment_preserves_the_original_dense_ann_results() -> None:
    baseline, rebuilt = Mock(), Mock()
    old_dense = (VectorSearchHit("old-point", 0.9, {"chunk_id": "old-dense"}),)
    new_dense = (VectorSearchHit("new-point", 0.8, {"chunk_id": "rebuilt-dense"}),)
    learned_sparse = (VectorSearchHit("sparse-point", 4.0, {"chunk_id": "learned"}),)
    baseline.dense_search.return_value = old_dense
    rebuilt.dense_search.return_value = new_dense
    rebuilt.sparse_search.return_value = learned_sparse
    index = SharedDenseTreatmentIndex(baseline, rebuilt)
    scope = VectorScope("workspace", "kb", ("revision",))
    assert index.dense_search((1.0,), scope=scope, limit=30) == old_dense
    assert (
        index.sparse_search(SparseEncoding((4,), (1.0,)), scope=scope, limit=30) == learned_sparse
    )
