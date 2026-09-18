from packages.knowledge.contracts import VectorSearchHit
from packages.knowledge.retrieval import fuse_reciprocal_rank


def _hit(chunk_id: str, score: float) -> VectorSearchHit:
    return VectorSearchHit(
        point_id=f"point-{chunk_id}",
        score=score,
        payload={"chunk_id": chunk_id},
    )


def test_rrf_is_equal_weighted_deduplicated_and_stably_sorted() -> None:
    dense = (_hit("b", 0.9), _hit("a", 0.8), _hit("c", 0.7))
    sparse = (_hit("a", 0.9), _hit("b", 0.8), _hit("d", 0.7))

    fused = fuse_reciprocal_rank(dense, sparse, rrf_k=60, candidate_top_k=20)

    assert [item.chunk_id for item in fused] == ["a", "b", "c", "d"]
    assert fused[0].retrieval_score == fused[1].retrieval_score
    assert fused[0].best_source_rank == 1
    assert fused[-1].best_source_rank == 3


def test_rrf_candidate_top_k_is_applied_after_fusion() -> None:
    dense = tuple(_hit(str(index), 1.0) for index in range(30))
    sparse = tuple(_hit(str(index), 1.0) for index in range(30, 60))

    fused = fuse_reciprocal_rank(dense, sparse, rrf_k=60, candidate_top_k=20)

    assert len(fused) == 20
