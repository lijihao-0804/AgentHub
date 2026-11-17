from __future__ import annotations

from types import SimpleNamespace

import pytest

from benchmarks.retrieval.revision_reuse import Source, digest


def test_revision_benchmark_validates_full_text_and_revision_citation():
    raw = "  source evidence\n"
    chunk = SimpleNamespace(
        chunk_id="chunk",
        ordinal=0,
        text="source evidence",
        locator={"type": "text_range", "char_start": 2, "char_end": 17},
    )
    source = Source("source.md", raw.encode(), "doc", "rev", {"chunk": chunk})
    hit = {
        "chunk_id": "chunk",
        "document_id": "doc",
        "document_revision_id": "rev",
        "source": "document:doc/revision:rev",
        "locator": chunk.locator,
        "snippet": chunk.text,
    }
    assert source.verify(hit)["sha256"] == digest(chunk.text.encode())
    assert len(source.manifest_hash()) == 64
    with pytest.raises(AssertionError):
        source.verify({**hit, "document_revision_id": "old"})
    with pytest.raises(AssertionError):
        source.verify({**hit, "locator": {**chunk.locator, "char_start": 0}})
    chunk.locator["char_end"] = 16
    with pytest.raises(AssertionError):
        source.manifest_hash()
