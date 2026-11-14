import numpy as np

from benchmarks.public_rag.article_transfer import top_lane


def test_exhaustive_lane_uses_stable_ids_for_ties_and_excludes_zero_sparse_hits():
    ids = np.array(["c", "b", "a", "z"])
    documents = np.array(["1", "2", "1", "3"])
    scores = np.array([0.0, 1.0, 1.0, -1.0])
    assert [r["chunk_id"] for r in top_lane(ids, documents, scores)] == ["a", "b", "c", "z"]
    assert [
        (r["chunk_id"], r["document_id"]) for r in top_lane(ids, documents, scores, positive=True)
    ] == [("a", "1"), ("b", "2")]
