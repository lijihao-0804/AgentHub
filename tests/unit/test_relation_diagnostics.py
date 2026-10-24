import pytest

from benchmarks.public_rag.relation_diagnostics import align_views, trace_case, union_length


def test_official_idx_is_not_confused_with_payload_position():
    paragraphs = [
        {"idx": 9, "title": "A", "paragraph_text": "Alpha beta gamma"},
        {"idx": 42, "title": "B", "paragraph_text": "Delta epsilon"},
    ]
    view = {
        "title": "B",
        "text": "Delta",
        "locator": {"paragraph_ordinal": 1, "start": 0, "end": 13},
    }
    assert list(align_views(paragraphs, [view], text_key="text", title_key="title")) == [42]
    view["text"] = "Gamma"
    with pytest.raises(ValueError, match="exact"):
        align_views(paragraphs, [view], text_key="text", title_key="title")


def test_overlapping_views_do_not_inflate_visible_coverage():
    assert union_length([(0, 9), (3, 12), (12, 15), (0, 9)]) == 15


def test_literal_answer_presence_never_becomes_entailment():
    raw = "A released the results in 2010."
    locator = {"paragraph_ordinal": 0, "start": 0, "end": len(raw)}
    case = {
        "all_original_paragraphs": [{"idx": 7, "title": "A", "paragraph_text": raw}],
        "visible_records": [{"document_name": "A", "snippet": raw, "locator": locator}],
        "source_decomposition": [
            {"paragraph_support_idx": 7, "question": "When was work carried out?", "answer": "2010"}
        ],
        "query_id": "test",
        "question": "When was work carried out?",
        "baseline": {},
        "official_aliases": ["2010"],
    }
    result = trace_case(case, [{"title": "A", "text": raw, "locator": locator}])
    assert result["steps"][0]["stages"]["answer_input"]["literal_answer_present"]
    assert result["semantic_support"].startswith("UNASSESSED")
