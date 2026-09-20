"""Unit tests for the Literature MCP Server's normalization.

Only the pure functions are exercised. Nothing here touches the network: the
frozen record contract has to hold for whatever OpenAlex happens to send, so the
inputs are hand-written payloads rather than recorded responses.
"""

from __future__ import annotations

import pytest

from apps.literature_mcp.openalex import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MIN_LIMIT,
    OpenAlexError,
    build_search_params,
    clamp_limit,
    normalize_doi,
    normalize_paper,
    normalize_paper_id,
    normalize_search_result,
    parse_paper_id,
    reconstruct_abstract,
)

WORK = {
    "id": "https://openalex.org/W2741809807",
    "title": "Failure Recovery in MEO Satellite Backbone Networks",
    "publication_year": 2021,
    "cited_by_count": 43,
    "doi": "https://doi.org/10.1109/TNSM.2021.123456",
    "authorships": [
        {"author": {"display_name": "A. Author"}},
        {"author": {"display_name": "B. Author"}},
    ],
    "primary_location": {
        "source": {"display_name": "IEEE TNSM"},
        "landing_page_url": "https://ieeexplore.ieee.org/document/123456",
    },
    "abstract_inverted_index": {"We": [0], "propose": [1], "a": [2], "scheme": [3]},
}


def test_reconstruct_abstract_orders_words_by_position() -> None:
    inverted = {"world": [1], "hello": [0], "again": [2, 4], "and": [3]}
    assert reconstruct_abstract(inverted) == "hello world again and again"


def test_reconstruct_abstract_returns_none_when_absent_or_empty() -> None:
    assert reconstruct_abstract(None) is None
    assert reconstruct_abstract({}) is None
    assert reconstruct_abstract({"word": []}) is None


def test_reconstruct_abstract_ignores_malformed_positions() -> None:
    assert reconstruct_abstract({"kept": [0], "dropped": ["x", -1, None]}) == "kept"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://doi.org/10.1109/xxx", "10.1109/xxx"),
        ("HTTPS://DOI.ORG/10.1109/XXX", "10.1109/XXX"),
        ("http://doi.org/10.1109/xxx", "10.1109/xxx"),
        ("doi:10.1109/xxx", "10.1109/xxx"),
        ("10.1109/xxx", "10.1109/xxx"),
        ("", None),
        (None, None),
        (12345, None),
    ],
)
def test_normalize_doi_strips_every_prefix_form(raw: object, expected: str | None) -> None:
    assert normalize_doi(raw) == expected


def test_normalize_paper_id_adds_the_source_prefix() -> None:
    assert normalize_paper_id("https://openalex.org/W2741809807") == "openalex:W2741809807"
    assert normalize_paper_id("W2741809807") == "openalex:W2741809807"


@pytest.mark.parametrize("raw", ["https://openalex.org/A123", "", "W", None, "../../etc"])
def test_normalize_paper_id_rejects_anything_unshaped(raw: object) -> None:
    assert normalize_paper_id(raw) is None


def test_parse_paper_id_round_trips_the_prefixed_form() -> None:
    assert parse_paper_id("openalex:W2741809807") == "W2741809807"


@pytest.mark.parametrize(
    "paper_id",
    ["W2741809807", "semanticscholar:W1", "openalex:", "openalex:../works/W1", "openalex:A1"],
)
def test_parse_paper_id_refuses_unusable_identifiers(paper_id: str) -> None:
    with pytest.raises(OpenAlexError):
        parse_paper_id(paper_id)


def test_normalize_paper_fills_every_contract_field() -> None:
    record = normalize_paper(WORK)
    assert record == {
        "paper_id": "openalex:W2741809807",
        "title": "Failure Recovery in MEO Satellite Backbone Networks",
        "authors": ["A. Author", "B. Author"],
        "year": 2021,
        "venue": "IEEE TNSM",
        "doi": "10.1109/TNSM.2021.123456",
        "abstract": "We propose a scheme",
        "url": "https://ieeexplore.ieee.org/document/123456",
        "citation_count": 43,
    }


def test_normalize_paper_reports_missing_fields_as_null_not_absent() -> None:
    record = normalize_paper({"id": "https://openalex.org/W9", "title": "Bare"})
    assert record is not None
    assert record["authors"] == []
    for field in ("year", "venue", "doi", "abstract", "citation_count"):
        assert field in record
        assert record[field] is None
    # A work always has an addressable OpenAlex page, so url stays populated.
    assert record["url"] == "https://openalex.org/W9"


def test_normalize_paper_falls_back_to_host_venue_and_doi_url() -> None:
    record = normalize_paper(
        {
            "id": "https://openalex.org/W9",
            "display_name": "Named by display_name",
            "host_venue": {"display_name": "Legacy Venue"},
            "doi": "https://doi.org/10.1/abc",
        }
    )
    assert record is not None
    assert record["title"] == "Named by display_name"
    assert record["venue"] == "Legacy Venue"
    assert record["url"] == "https://doi.org/10.1/abc"
    assert record["doi"] == "10.1/abc"


@pytest.mark.parametrize(
    "work",
    [
        None,
        "not a mapping",
        {"title": "No id"},
        {"id": "https://openalex.org/A1", "title": "Not a work id"},
        {"id": "https://openalex.org/W1"},
        {"id": "https://openalex.org/W1", "title": "   "},
    ],
)
def test_normalize_paper_drops_records_nobody_can_address_or_name(work: object) -> None:
    assert normalize_paper(work) is None


def test_normalize_paper_ignores_booleans_masquerading_as_counts() -> None:
    record = normalize_paper(
        {"id": "https://openalex.org/W1", "title": "T", "cited_by_count": True}
    )
    assert record is not None
    assert record["citation_count"] is None


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (None, DEFAULT_LIMIT),
        (0, MIN_LIMIT),
        (-5, MIN_LIMIT),
        (1, 1),
        (20, 20),
        (50, MAX_LIMIT),
        (5000, MAX_LIMIT),
    ],
)
def test_clamp_limit_forces_the_declared_range(given: int | None, expected: int) -> None:
    assert clamp_limit(given) == expected


def test_build_search_params_carries_both_year_bounds() -> None:
    params = build_search_params("meo satellite", year_from=2019, year_to=2023, limit=5)
    assert params == {
        "search": "meo satellite",
        "per-page": "5",
        "filter": "from_publication_date:2019-01-01,to_publication_date:2023-12-31",
    }


def test_build_search_params_omits_the_filter_when_unbounded() -> None:
    params = build_search_params("  meo  ")
    assert params == {"search": "meo", "per-page": str(DEFAULT_LIMIT)}


def test_build_search_params_clamps_an_out_of_range_limit() -> None:
    assert build_search_params("meo", limit=900)["per-page"] == str(MAX_LIMIT)


def test_build_search_params_refuses_an_empty_query() -> None:
    with pytest.raises(OpenAlexError):
        build_search_params("   ")


def test_normalize_search_result_echoes_the_query_and_the_corpus_total() -> None:
    payload = {"meta": {"count": 12345}, "results": [WORK]}
    result = normalize_search_result(payload, query="meo", limit=20)
    assert result["source"] == "openalex"
    assert result["query"] == "meo"
    assert result["total"] == 12345
    assert [paper["paper_id"] for paper in result["papers"]] == ["openalex:W2741809807"]


def test_normalize_search_result_skips_unusable_records() -> None:
    payload = {"meta": {"count": 3}, "results": [WORK, {"title": "No id"}, None]}
    assert len(normalize_search_result(payload, query="meo")["papers"]) == 1


def test_normalize_search_result_clamps_an_oversized_page() -> None:
    results = [dict(WORK, id=f"https://openalex.org/W{index}") for index in range(10)]
    result = normalize_search_result({"results": results}, query="meo", limit=3)
    assert len(result["papers"]) == 3


def test_normalize_search_result_falls_back_to_the_page_size_for_total() -> None:
    result = normalize_search_result({"results": [WORK]}, query="meo")
    assert result["total"] == 1


@pytest.mark.parametrize("payload", [None, {}, {"results": "not a list"}, "nonsense"])
def test_normalize_search_result_survives_an_unexpected_payload(payload: object) -> None:
    result = normalize_search_result(payload, query="meo")
    assert result["papers"] == []
    assert result["total"] == 0
