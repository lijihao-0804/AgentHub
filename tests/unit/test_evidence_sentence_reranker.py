import math

import pytest

from packages.knowledge.contracts import KnowledgeProviderError, RerankCandidate
from packages.knowledge.evidence_sentence import (
    SentenceEvidenceReranker,
    evidence_question,
    sentence_offsets,
)


def test_adjacent_windows_use_exact_source_slices_ending_at_paragraph_boundaries():
    text = "  One. Two! Three?\n\nHeading\n\nA final sentence."
    spans = sentence_offsets(text)
    assert [text[start:end] for start, end in spans] == [
        "One. Two!",
        "Two! Three?",
        "Heading",
        "A final sentence.",
    ]
    assert sentence_offsets(" \r\n ") == ()


def test_focus_only_removes_an_explicit_quoted_source_qualifier():
    assert evidence_question('In the article "A, B and C", Which property changes?') == (
        "Which property changes?"
    )
    for query in ("How many pages are in the paper?", "In the paper mill, where was the leak?"):
        assert evidence_question(query) == query


def test_whole_sources_keep_their_ids_and_order_and_repeated_windows_score_once():
    query = "In the paper “Study”, What mechanism was found?"
    candidates = [
        RerankCandidate("first-real-chunk", "A. B. C."),
        RerankCandidate("other-real-chunk", "B. C. D."),
    ]

    class Recorder:
        def __init__(self):
            self.calls = []

        def rerank(self, needle, batch):
            self.calls.append((needle, [c.text for c in batch]))
            if needle == query:
                return (5.0, 1.0)
            return tuple({"A. B.": -1.0, "B. C.": 3.0, "C. D.": 9.0}[c.text] for c in batch)

    recorder = Recorder()
    details = SentenceEvidenceReranker(recorder).inspect(query, candidates)
    assert recorder.calls == [
        (query, [c.text for c in candidates]),
        ("What mechanism was found?", ["A. B.", "B. C.", "C. D."]),
    ]
    assert [(r.chunk_id, r.score) for r in details] == [
        ("first-real-chunk", 4.0),
        ("other-real-chunk", 5.0),
    ]
    strongest = max(details[1].windows, key=lambda w: w.score)
    assert candidates[1].text[strongest.start : strongest.end] == "C. D."


def test_nonfinite_window_cannot_disappear_in_max_pooling():
    class NonfiniteWindow:
        def rerank(self, query, batch):
            return (1.0,) if len(batch) == 1 else (1.0, math.nan)

    with pytest.raises(KnowledgeProviderError, match="Nonfinite"):
        SentenceEvidenceReranker(NonfiniteWindow()).rerank("q", [RerankCandidate("id", "A. B. C.")])
