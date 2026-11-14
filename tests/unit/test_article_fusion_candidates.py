import pytest

from benchmarks.public_rag import article_fusion_verify
from benchmarks.public_rag.article_fusion_candidates import (
    select_article_candidates,
    select_local_passages,
)


def hit(chunk, doc, text="generic"):
    return {"chunk_id": chunk, "document_id": doc, "score": 1.0, "text": text}


def test_document_credit_across_different_chunks():
    dense = [hit("a", "A"), hit("b1", "B")]
    sparse = [hit("c", "C"), hit("b2", "B")]
    assert [h["chunk_id"] for h in select_article_candidates("article_first", dense, sparse)] == [
        "b1",
        "a",
        "c",
        "b2",
    ]


def test_reopen_unique_source_blocks_outside_both_lanes():
    dense = [hit("1", "A"), hit("2", "B")]
    sparse = [hit("3", "A"), hit("4", "B")]
    blocks = {
        "A": [dense[0], sparse[0], hit("a", "A", "oxygen")],
        "B": [dense[1], sparse[1], hit("b", "B", "oxygen")],
    }
    result = select_local_passages("oxygen", dense, sparse, blocks)
    assert [(h["chunk_id"], h["document_id"]) for h in result] == [
        ("a", "A"),
        ("b", "B"),
        ("1", "A"),
        ("2", "B"),
    ]
    with pytest.raises(ValueError):
        select_local_passages("oxygen", dense, sparse, {**blocks, "A": [dense[0]]})


def test_protected_refill_retains_head_and_first_block_of_each_source():
    lane = [hit(f"h{i:02d}", str(i // 2)) for i in range(30)]
    result = select_article_candidates("article_refill4", lane, [])
    chosen = [h["chunk_id"] for h in result]
    assert chosen[:8] == [h["chunk_id"] for h in lane[:8]]
    assert all(h["chunk_id"] in chosen for h in lane[:20:2])
    assert chosen[-4:] == [h["chunk_id"] for h in lane[20:28:2]]
    assert len(set(chosen)) == 20


def test_published_fusion_counts():
    article_fusion_verify.verify()
