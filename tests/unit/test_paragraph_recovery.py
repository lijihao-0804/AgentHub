from copy import deepcopy

import pytest

from benchmarks.public_rag.experimental.paragraph_recovery import leaf_view, recover_paragraphs


def test_expansion_gets_new_range_identity_without_mutating_leaf_or_concatenating_neighbors():
    article = "First paragraph.\n\nComparison lists A, B and C.\n\nUnrelated next paragraph."
    start, end = article.index("Comparison"), article.index("\n\nUnrelated")
    parent = {"key": "p:1", "start": start, "end": end, "text": article[start:end]}
    anchor = {
        "chunk_id": "immutable-leaf",
        "text": article[start : start + 12],
        "locator": {"char_start": start, "char_end": start + 12},
    }
    before = deepcopy(anchor)
    recovered = recover_paragraphs([anchor, anchor], article, [parent], "p", "revision")
    assert anchor == before and len(recovered) == 1
    assert recovered[0]["text"] == "Comparison lists A, B and C."
    assert recovered[0]["source_id"] != anchor["chunk_id"]
    assert recovered[0]["source_chunk_ids"] == [anchor["chunk_id"]]
    assert recovered[0]["complete_parent"] is True


def test_long_parent_stays_bounded_and_forged_original_range_fails():
    article = "abcdefghijklmnopqrstuvwxyz"
    anchor = {"chunk_id": "leaf", "text": "klmn", "locator": {"char_start": 10, "char_end": 14}}
    parent = {"key": "p:1", "text": article, "start": 0, "end": len(article)}
    view = recover_paragraphs([anchor], article, [parent], "p", "r", max_chars=8)[0]
    assert len(view["text"]) == 8 and not view["complete_parent"]
    assert "klmn" in view["text"]
    with pytest.raises(ValueError, match="identity"):
        leaf_view({**anchor, "text": "fabricated"}, article, "p", "r")
