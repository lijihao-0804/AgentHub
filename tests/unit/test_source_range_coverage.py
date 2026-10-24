from benchmarks.public_rag.source_range_coverage import (
    annotation_coverage,
    covered_characters,
    locate_unique,
)


def test_reordered_overlapping_chunks_cover_source_without_concatenation_match():
    article = "Alpha beta gamma delta."
    sources = {
        "one": {"paper_id": "p", "chunk_text": "Alpha beta gamma"},
        "two": {"paper_id": "p", "chunk_text": "gamma delta."},
    }
    records = [
        {"chunk_id": "two", "snippet": "gamma delta."},
        {"chunk_id": "one", "snippet": "Alpha beta gamma"},
    ]
    assert article not in " ".join(r["snippet"] for r in records)
    result = annotation_coverage(article, [[article]], records, sources, paper_id="p")
    assert result["annotations"][0]["coverage"]["complete"] is True
    assert result["annotations"][0]["coverage"]["fraction"] == 1
    assert result["semantic_sufficiency"] == "UNKNOWN"


def test_duplicates_do_not_inflate_coverage_and_missing_ranges_remain_visible():
    result = covered_characters([(0, 10)], [(0, 3), (0, 3), (7, 10)])
    assert result == {"required_chars": 10, "covered_chars": 6, "fraction": 0.6, "complete": False}
    assert locate_unique("Repeated. Repeated.", "Repeated.") is None
    assert locate_unique("One\n\n two.", "One two.") == (0, 10)


def test_ambiguous_or_cross_source_mapping_cannot_receive_complete_credit():
    source = {"c": {"paper_id": "other", "chunk_text": "fact"}}
    value = annotation_coverage(
        "fact", [["fact"]], [{"chunk_id": "c", "snippet": "fact"}], source, paper_id="p"
    )
    assert value["annotations"][0]["status"] == "UNKNOWN"
    assert value["annotations"][0]["coverage"] is None
