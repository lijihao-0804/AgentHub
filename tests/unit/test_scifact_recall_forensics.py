import numpy as np
import pytest
from scipy import sparse

from benchmarks.public_rag.data import SourceDocument
from benchmarks.public_rag.scifact_recall_forensics import (
    best_rank,
    document_ranks,
    fuse,
    historical_ranks,
    idf_weights,
    ranking,
    rationale_coverage,
    replacement_rank,
    sentence_ranges,
    sparse_scores,
)
from packages.knowledge.contracts import SparseEncoding


def test_rrf_credits_the_same_chunk_and_document_ordinal_uses_first_occurrence():
    combined = fuse(["gold-left", "decoy", "x"], ["gold-right", "decoy", "y"])
    assert combined[0] == "decoy"
    assert combined.index("gold-left") == 1
    sources = {"gold-left": "gold", "gold-right": "gold", "decoy": "wrong", "x": "x", "y": "y"}
    assert document_ranks(combined, sources)["gold"] == 2
    assert best_rank(["missing"], document_ranks(combined, sources)) is None


def test_changed_historical_branch_invalidates_alternate_rerank_comparison():
    a = {"chunk_id": "a", "score": 0.5}
    b = {"chunk_id": "b", "score": 0.25}
    stages = {k: {"ranking": [a, b]} for k in ("dense", "sparse", "fusion", "selection", "rerank")}
    assert historical_ranks(stages, stages)["rrf"] == {"a": 1, "b": 2}
    other = dict(stages, sparse={"ranking": [a, dict(b, score=0.26)]})
    with pytest.raises(ValueError, match="branch data differ"):
        historical_ranks(stages, other)


def test_sparse_ranks_ignore_zero_scores_and_hold_idf_and_competitors_fixed():
    docs = sparse.csr_matrix([[2.0, 1.0], [1.0, 0.0], [0.0, 3.0]], dtype=np.float32)
    idf = idf_weights(docs)
    assert np.allclose(idf, np.log(1 + (3 - 2 + 0.5) / (2 + 0.5)))
    scores = sparse_scores(docs, idf, SparseEncoding(indices=(0,), values=(1.5,)))
    assert np.allclose(scores, [3 * idf[0], 1.5 * idf[0], 0])
    assert ranking(scores, np.array(["j", "k", "l"]), positive=True) == [0, 1]
    assert replacement_rank(5, np.array([5, 3, 7, 5])) == [2, 4]


def test_official_ranges_use_actual_text_whitespace_and_entire_rationale_sets():
    doc = SourceDocument("doc", "Title", "One  two. Three four.")
    locations = sentence_ranges(doc, {"title": "Title", "abstract": ["One two.\n", " Three four."]})
    assert [doc.retrieval_text[a:b] for a, b in locations] == ["One  two.", "Three four."]
    windows = [
        {"ordinal": 0, "char_start": 0, "char_end": 17},
        {"ordinal": 1, "char_start": 13, "char_end": 29},
    ]
    covered = rationale_coverage([{"sentences": [0, 1], "label": "SUPPORT"}], locations, windows)
    assert covered[0]["containing_chunks_per_sentence"] == [[0], [1]]
    assert covered[0]["chunks_containing_entire_set"] == []
    assert rationale_coverage([], locations, windows) == []
    with pytest.raises(ValueError, match="could not be aligned"):
        sentence_ranges(doc, {"title": "Title", "abstract": ["Different sentence."]})
