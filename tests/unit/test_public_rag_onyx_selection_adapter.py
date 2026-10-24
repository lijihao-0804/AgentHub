from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from types import SimpleNamespace

import pytest

from benchmarks.public_rag.onyx_selection_adapter import (
    SYSTEM_PROMPT,
    build_sections,
    model_sections,
    parse_selection,
    promote_selection,
)


def _chunk(text: str, ordinal: int, revision: str = "revision-a") -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        ordinal=ordinal,
        document_revision_id=revision,
    )


def _hit(chunk_id: str, document_id: str, score: float) -> dict:
    return {"chunk_id": chunk_id, "document_id": document_id, "score": score}


def test_blank_upstream_title_is_preserved_but_missing_title_is_rejected():
    args = {"chunks": {"a": _chunk("Evidence", 0)}, "source_mapping": {"a": "source"}}
    sections = build_sections([_hit("a", "source", 0.5)], titles={"source": ""}, **args)
    assert model_sections(sections)[0]["title"] == ""
    with pytest.raises(ValueError, match="title"):
        build_sections([_hit("a", "source", 0.5)], titles={}, **args)


def test_build_sections_uses_first_three_baseline_children_then_chronology() -> None:
    baseline_hits = [
        _hit("a3", "source-a", 0.9),
        _hit("b2", "source-b", 0.8),
        _hit("a1", "source-a", 0.7),
        _hit("c1", "source-c", 0.6),
        _hit("a2", "source-a", 0.5),
        _hit("a4", "source-a", 0.4),
        _hit("b1", "source-b", 0.3),
    ]
    chunks = {
        "a1": _chunk("A1", 1),
        "a2": _chunk("A2", 2),
        "a3": _chunk("A3", 3),
        "a4": _chunk("A4", 4),
        "b1": _chunk("B1", 1),
        "b2": _chunk("B2", 2),
        "c1": _chunk("C1", 1),
    }
    source_mapping = {chunk_id: f"source-{chunk_id[0]}" for chunk_id in chunks}

    sections = build_sections(
        baseline_hits,
        chunks=chunks,
        source_mapping=source_mapping,
        titles={
            "source-a": "Alpha",
            "source-b": "Beta",
            "source-c": "Gamma",
        },
    )

    assert [section["source_id"] for section in sections] == [
        "source-a",
        "source-b",
        "source-c",
    ]
    assert sections[0] == {
        "section_id": 0,
        "source_id": "source-a",
        "title": "Alpha",
        "content": "A1\n\nA2\n\nA3",
        "chunk_ids": ["a1", "a2", "a3"],
    }
    assert sections[1]["chunk_ids"] == ["b1", "b2"]
    assert sections[1]["content"] == "B1\n\nB2"


def test_build_sections_rejects_revision_conflicts_even_after_the_first_three_children() -> None:
    baseline_hits = [
        _hit("a1", "source-a", 0.9),
        _hit("a2", "source-a", 0.8),
        _hit("a3", "source-a", 0.7),
        _hit("a4", "source-a", 0.6),
    ]
    chunks = {
        "a1": _chunk("A1", 1),
        "a2": _chunk("A2", 2),
        "a3": _chunk("A3", 3),
        "a4": _chunk("A4", 4, "revision-b"),
    }

    with pytest.raises(ValueError, match="conflicting revisions"):
        build_sections(
            baseline_hits,
            chunks=chunks,
            source_mapping={chunk_id: "source-a" for chunk_id in chunks},
            titles={"source-a": "Alpha"},
        )


@pytest.mark.parametrize(
    ("mapping_change", "message"),
    [
        ({"a1": "source-b"}, "disagrees with baseline"),
        ({"a1": None}, "no authorized source mapping"),
    ],
)
def test_build_sections_rejects_unauthorized_or_disagreeing_child_mapping(
    mapping_change: dict[str, str],
    message: str,
) -> None:
    baseline_hits = [_hit("a1", "source-a", 0.9)]
    source_mapping = {"a1": "source-a"}
    if mapping_change == {"a1": None}:
        source_mapping = {}
    else:
        source_mapping.update(mapping_change)

    with pytest.raises(ValueError, match=message):
        build_sections(
            baseline_hits,
            chunks={"a1": _chunk("A1", 1)},
            source_mapping=source_mapping,
            titles={"source-a": "Alpha", "source-b": "Beta", "missing": "Missing"},
        )


def test_parse_selection_accepts_empty_ordered_and_maximum_valid_lists() -> None:
    assert parse_selection('{"section_ids": []}', count=0) == ()
    assert parse_selection('{"section_ids": [2, 0]}', count=3) == (2, 0)
    assert parse_selection(
        json.dumps({"section_ids": list(range(10))}),
        count=10,
    ) == tuple(range(10))


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[]",
        '{"section_ids": [], "source_id": 0}',
        '{"section_ids": true}',
        '{"section_ids": [true]}',
        '{"section_ids": [0.0]}',
        '{"section_ids": ["0"]}',
        '{"section_ids": [-1]}',
        '{"section_ids": [11]}',
        '{"section_ids": [1, 1]}',
        '{"section_ids": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]}',
        '{"section_ids": [0], "section_ids": [1]}',
    ],
)
def test_parse_selection_rejects_schema_type_range_duplicate_and_key_violations(
    text: str,
) -> None:
    with pytest.raises(ValueError):
        parse_selection(text, count=11)


def test_promote_selection_orders_selected_sources_then_remaining_baseline_children() -> None:
    baseline_hits = [
        _hit("a1", "source-a", 0.9),
        _hit("b1", "source-b", 0.8),
        _hit("a2", "source-a", 0.7),
        _hit("c1", "source-c", 0.6),
        _hit("b2", "source-b", 0.5),
        _hit("a3", "source-a", 0.4),
        _hit("c2", "source-c", 0.3),
    ]
    chunks = {
        chunk_id: _chunk(chunk_id, int(chunk_id[1]))
        for chunk_id in ("a1", "a2", "a3", "b1", "b2", "c1", "c2")
    }
    sections = build_sections(
        baseline_hits,
        chunks=chunks,
        source_mapping={chunk_id: f"source-{chunk_id[0]}" for chunk_id in chunks},
        titles={"source-a": "Alpha", "source-b": "Beta", "source-c": "Gamma"},
    )

    promoted = promote_selection(baseline_hits, sections, (2, 0))

    assert [hit["chunk_id"] for hit in promoted] == [
        "c1",
        "c2",
        "a1",
        "a2",
        "a3",
        "b1",
        "b2",
    ]
    assert promoted == [
        _hit("c1", "source-c", 0.6),
        _hit("c2", "source-c", 0.3),
        _hit("a1", "source-a", 0.9),
        _hit("a2", "source-a", 0.7),
        _hit("a3", "source-a", 0.4),
        _hit("b1", "source-b", 0.8),
        _hit("b2", "source-b", 0.5),
    ]


def test_promote_selection_empty_list_is_an_exact_baseline_copy() -> None:
    baseline_hits = [
        _hit("a1", "source-a", 0.9),
        _hit("b1", "source-b", 0.8),
        _hit("a2", "source-a", 0.7),
        _hit("c1", "source-c", 0.6),
    ]
    chunks = {
        "a1": _chunk("A1", 1),
        "a2": _chunk("A2", 2),
        "b1": _chunk("B1", 1),
        "c1": _chunk("C1", 1),
    }
    sections = build_sections(
        baseline_hits,
        chunks=chunks,
        source_mapping={chunk_id: f"source-{chunk_id[0]}" for chunk_id in chunks},
        titles={"source-a": "Alpha", "source-b": "Beta", "source-c": "Gamma"},
    )

    promoted = promote_selection(baseline_hits, sections, ())

    assert promoted == baseline_hits
    assert promoted is not baseline_hits
    assert promoted[0] is not baseline_hits[0]


class _ExplodingBaseline(Sequence[dict]):
    def __len__(self) -> int:
        return 0

    def __getitem__(self, index: int) -> dict:
        raise AssertionError("baseline must not be inspected before selection validation")

    def __iter__(self) -> Iterator[dict]:
        raise AssertionError("baseline must not be inspected before selection validation")


@pytest.mark.parametrize("selected_ids", [(1,), (True,)])
def test_promote_selection_validates_ids_before_touching_the_baseline(
    selected_ids: Sequence[int],
) -> None:
    sections = [
        {
            "section_id": 0,
            "source_id": "source-a",
            "title": "Alpha",
            "content": "A",
            "chunk_ids": ["a1"],
        }
    ]

    with pytest.raises(ValueError):
        promote_selection(_ExplodingBaseline(), sections, selected_ids)


def test_model_sections_strips_source_and_child_bookkeeping() -> None:
    sections = [
        {
            "section_id": 0,
            "source_id": "source-a",
            "title": "Alpha",
            "content": "Evidence",
            "chunk_ids": ["a1"],
        }
    ]

    assert model_sections(sections) == [{"section_id": 0, "title": "Alpha", "content": "Evidence"}]
    assert sections[0]["source_id"] == "source-a"
    assert sections[0]["chunk_ids"] == ["a1"]


def test_system_prompt_requires_relevance_context_untrusted_text_and_strict_json() -> None:
    prompt = SYSTEM_PROMPT.lower()

    assert "supporting" in prompt
    assert "contradict" in prompt
    assert "untrusted" in prompt
    assert "ignore instructions" in prompt
    assert "at most 10" in prompt
    assert '{"section_ids": [0, 2]}' in prompt
    assert "qrel" not in prompt
    assert "answer" in prompt
