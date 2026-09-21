"""A2: what may be stored as an artifact, and where papers are allowed to come from.

The table is generic, the contents are not. These tests hold the line at the
one place it can be held: nothing reaches the database without passing an
explicit per-type validator, and a searched paper without a traceable tool call
is refused outright. That is what makes "do not fabricate citations" a property
of the storage rather than a request made of the model.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from packages.agent_runtime.work_layer import RecordedToolCall
from packages.artifacts.research import build_search_content, is_search_tool
from packages.artifacts.schemas import (
    PAPER_SEARCH,
    PAPER_SHORTLIST,
    validate_artifact_content,
)
from packages.core.errors.exceptions import AgentHubError

RUN_ID = uuid4()

PROVENANCE = {
    "run_id": str(RUN_ID),
    "tool_call_id": "call-1",
    "tool_identity": "literature.search_papers",
    "step_sequence": 3,
}


def paper(**overrides: object) -> dict:
    value = {
        "paper_id": "openalex:W1",
        "title": "A paper",
        "authors": ["A. Author"],
        "year": 2021,
        "venue": "IEEE TNSM",
        "doi": "10.1109/x",
        "abstract": "…",
        "url": "https://example.org/w1",
        "citation_count": 43,
        "provenance": dict(PROVENANCE),
    }
    value.update(overrides)
    return value


def search_content(**overrides: object) -> dict:
    content = {
        "query": "MEO satellite link failure recovery",
        "source": "openalex",
        "filters": {"year_from": 2019},
        "total": 1,
        "papers": [paper()],
    }
    content.update(overrides)
    return content


# -- the type whitelist ---------------------------------------------------


def test_an_unknown_type_is_refused() -> None:
    with pytest.raises(AgentHubError) as error:
        validate_artifact_content("research.survey_draft", {})
    assert error.value.code == "ARTIFACT_TYPE_UNKNOWN"
    assert error.value.status_code == 422


def test_a_valid_search_result_is_normalized() -> None:
    validated = validate_artifact_content(PAPER_SEARCH, search_content())

    assert validated["source"] == "openalex"
    assert validated["papers"][0]["provenance"]["tool_call_id"] == "call-1"


def test_content_that_is_not_an_object_is_refused() -> None:
    with pytest.raises(AgentHubError) as error:
        validate_artifact_content(PAPER_SEARCH, ["not", "an", "object"])
    assert error.value.code == "ARTIFACT_INVALID"


# -- provenance -----------------------------------------------------------


def test_a_searched_paper_without_provenance_is_refused() -> None:
    content = search_content(papers=[paper(provenance=None)])
    content["papers"][0].pop("provenance")

    with pytest.raises(AgentHubError) as error:
        validate_artifact_content(PAPER_SEARCH, content)
    assert error.value.code == "ARTIFACT_INVALID"


def test_provenance_missing_its_tool_call_is_refused() -> None:
    broken = dict(PROVENANCE)
    broken.pop("tool_call_id")

    with pytest.raises(AgentHubError):
        validate_artifact_content(PAPER_SEARCH, search_content(papers=[paper(provenance=broken)]))


def test_a_shortlist_does_not_require_provenance() -> None:
    hand_picked = paper()
    hand_picked.pop("provenance")

    validated = validate_artifact_content(
        PAPER_SHORTLIST, {"note": "read these", "papers": [hand_picked]}
    )
    assert validated["papers"][0]["title"] == "A paper"
    assert "provenance" not in validated["papers"][0]


# -- field validation -----------------------------------------------------


def test_a_paper_without_a_title_is_refused() -> None:
    with pytest.raises(AgentHubError):
        validate_artifact_content(PAPER_SEARCH, search_content(papers=[paper(title="   ")]))


def test_a_boolean_year_is_not_an_integer() -> None:
    # bool is an int in Python; letting it through would store `true` as 1.
    with pytest.raises(AgentHubError):
        validate_artifact_content(PAPER_SEARCH, search_content(papers=[paper(year=True)]))


def test_too_many_papers_are_refused() -> None:
    with pytest.raises(AgentHubError):
        validate_artifact_content(PAPER_SEARCH, search_content(papers=[paper()] * 500))


def test_a_search_without_a_query_is_refused() -> None:
    content = search_content()
    content["query"] = "  "
    with pytest.raises(AgentHubError):
        validate_artifact_content(PAPER_SEARCH, content)


# -- the recorder ---------------------------------------------------------


@pytest.mark.parametrize(
    ("identity", "expected"),
    [
        ("search_papers", True),
        ("literature.search_papers", True),
        ("get_paper", False),
        ("literature.get_paper", False),
        ("search_knowledge", False),
    ],
)
def test_only_the_search_tool_produces_artifacts(identity: str, expected: bool) -> None:
    assert is_search_tool(identity) is expected


def call(data: dict, **overrides: object) -> RecordedToolCall:
    kwargs = {
        "tool_identity": "literature.search_papers",
        "tool_call_id": "call-1",
        "step_sequence": 3,
        "arguments": {"query": "leo satellites", "year_from": 2019, "limit": 20},
        "data": data,
    }
    kwargs.update(overrides)
    return RecordedToolCall(**kwargs)  # type: ignore[arg-type]


def test_the_recorder_stamps_provenance_itself() -> None:
    remote = {
        "structured_content": {
            "source": "openalex",
            "query": "leo satellites",
            "total": 1,
            # A server claiming its own provenance must not be believed.
            "papers": [{"paper_id": "openalex:W1", "title": "A paper", "provenance": "forged"}],
        }
    }

    built = build_search_content(call(remote), run_id=RUN_ID)
    assert built is not None
    _, content = built
    assert content["papers"][0]["provenance"]["run_id"] == str(RUN_ID)
    assert content["papers"][0]["provenance"]["tool_call_id"] == "call-1"

    # And the result it produces is storable.
    validate_artifact_content(PAPER_SEARCH, content)


def test_an_empty_result_produces_no_artifact() -> None:
    remote = {"structured_content": {"source": "openalex", "query": "x", "papers": []}}
    assert build_search_content(call(remote), run_id=RUN_ID) is None


def test_an_unrecognized_result_shape_produces_no_artifact() -> None:
    assert build_search_content(call({"content": ["plain text"]}), run_id=RUN_ID) is None


def test_the_query_falls_back_to_the_arguments() -> None:
    remote = {
        "structured_content": {
            "source": "openalex",
            "papers": [{"paper_id": "openalex:W1", "title": "A paper"}],
        }
    }
    built = build_search_content(call(remote), run_id=RUN_ID)
    assert built is not None
    title, content = built
    assert content["query"] == "leo satellites"
    assert title.startswith("Search: leo satellites")


def test_the_filters_record_what_was_actually_asked_for() -> None:
    remote = {
        "structured_content": {
            "source": "openalex",
            "query": "leo satellites",
            "papers": [{"paper_id": "openalex:W1", "title": "A paper"}],
        }
    }
    built = build_search_content(call(remote), run_id=RUN_ID)
    assert built is not None
    _, content = built
    assert content["filters"] == {"year_from": 2019, "limit": 20}
