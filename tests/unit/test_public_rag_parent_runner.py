from types import SimpleNamespace

import pytest

from benchmarks.public_rag.data import SourceDocument
from benchmarks.public_rag.parent_rerank_runner import token_audit, verified_parents


def fixture_rows():
    document = SourceDocument("source", "Title", "abcdefghij")
    text = document.retrieval_text
    rows = [
        SimpleNamespace(
            chunk_id="a",
            document_revision_id="revision",
            text=text[:10],
            locator={"char_start": 0, "char_end": 10},
        ),
        SimpleNamespace(
            chunk_id="b",
            document_revision_id="revision",
            text=text[8:],
            locator={"char_start": 8, "char_end": len(text)},
        ),
    ]
    return (
        rows,
        {"chunk_sources": {"a": "source", "b": "source"}, "documents": 1},
        {"source": document},
    )


def test_authorized_complete_parent_is_recovered():
    rows, state, corpus = fixture_rows()
    result = verified_parents(rows, state, corpus)
    assert result == {key: ("revision", corpus["source"].retrieval_text) for key in ("a", "b")}


@pytest.mark.parametrize("fault", ["missing", "text", "gap", "suffix", "range", "unknown"])
def test_incomplete_or_mismatched_parent_is_rejected(fault):
    rows, state, corpus = fixture_rows()
    if fault == "missing":
        rows.pop()
    elif fault == "text":
        rows[0].text = "different content"
    elif fault == "gap":
        rows[1].locator["char_start"] = 11
        rows[1].text = corpus["source"].retrieval_text[11:]
    elif fault == "suffix":
        rows[1].locator["char_end"] -= 1
        rows[1].text = rows[1].text[:-1]
    elif fault == "range":
        rows[0].locator["char_start"] = False
    else:
        state["chunk_sources"]["b"] = "unavailable"
    with pytest.raises(ValueError):
        verified_parents(rows, state, corpus)


def test_token_audit_counts_actual_truncation():
    def tokenizer(queries, texts, *, padding, truncation, verbose):
        assert queries == ["query", "query"]
        return {
            "input_ids": [
                list(range(min(len(text), 4) if truncation else len(text))) for text in texts
            ]
        }

    pairs = [SimpleNamespace(text="abc"), SimpleNamespace(text="abcdef")]
    assert token_audit(tokenizer, "query", pairs) == {
        "encoded_tokens": 7,
        "padded_tokens": 8,
        "truncated_pairs": 1,
        "max_untruncated_tokens": 6,
    }
