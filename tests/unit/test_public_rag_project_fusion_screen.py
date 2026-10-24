import copy

import pytest

from benchmarks.public_rag.project_fusion_screen import replay
from packages.knowledge.contracts import VectorSearchHit
from packages.knowledge.retrieval import fuse_reciprocal_rank


def example():
    def hit(chunk, score):
        return {"chunk_id": chunk, "document_id": f"doc-{chunk}", "score": score}

    dense = [hit("unique", 1000.0)] + [hit(f"shared-{i:02}", 10.0) for i in range(29)]
    sparse = [hit(f"shared-{i:02}", 10.0) for i in range(29)] + [hit("tail", 0.0)]
    lanes = [
        tuple(VectorSearchHit(h["chunk_id"], h["score"], {"chunk_id": h["chunk_id"]}) for h in lane)
        for lane in (dense, sparse)
    ]
    fused = [
        hit(h.chunk_id, h.retrieval_score)
        for h in fuse_reciprocal_rank(*lanes, candidate_top_k=60, rrf_k=60)
    ]
    raw_scores = [hit(h["chunk_id"], 100.0 if h["chunk_id"] == "unique" else -1.0) for h in fused]
    row = {
        "query_id": "query",
        "stages": {
            name: {"ranking": ranking}
            for name, ranking in (
                ("dense", dense),
                ("sparse", sparse),
                ("fusion", fused),
                ("selection", copy.deepcopy(fused)),
                ("rerank", raw_scores),
            )
        },
    }
    mapping = {h["chunk_id"]: h["document_id"] for h in dense + sparse}
    return row, mapping


def test_score_strength_can_rescue_strong_single_lane_candidate_without_larger_budget():
    row, mapping = example()
    before = copy.deepcopy(row)
    result = replay(row, mapping)
    assert len(result["baseline"]["stages"]["fusion"]["ranking"]) == 20
    assert len(result["dbsf"]["stages"]["fusion"]["ranking"]) == 20
    assert "unique" not in {
        h["chunk_id"] for h in result["baseline"]["stages"]["fusion"]["ranking"]
    }
    assert result["dbsf"]["stages"]["rerank"]["ranking"][0]["chunk_id"] == "unique"
    assert result["dbsf"]["stages"]["rerank"]["ranking"][0]["score"] == 100.0
    assert row == before


@pytest.mark.parametrize("fault", ["missing_score", "mapping", "wrong_rrf", "score_bool"])
def test_replay_rejects_unscored_or_inconsistent_source(fault):
    row, mapping = example()
    if fault == "missing_score":
        row["stages"]["rerank"]["ranking"].pop()
    elif fault == "mapping":
        mapping["unique"] = "different-source"
    elif fault == "wrong_rrf":
        ranking = row["stages"]["fusion"]["ranking"]
        ranking[0], ranking[1] = ranking[1], ranking[0]
    else:
        row["stages"]["dense"]["ranking"][0]["score"] = True
    with pytest.raises(ValueError):
        replay(row, mapping)
