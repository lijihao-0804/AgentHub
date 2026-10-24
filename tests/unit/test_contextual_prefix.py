"""P-A1 prefix generator unit tests: strict schema and fail-open semantics."""

from __future__ import annotations

import json

import pytest

from benchmarks.public_rag.contextual_prefix import (
    parse_prefix,
    parse_summary,
    prefixed_text,
    situate_messages,
    summary_messages,
)


def test_parse_prefix_accepts_valid_context() -> None:
    text = json.dumps({"prefix": "This passage states the refund window for premium members."})
    assert parse_prefix(text).startswith("This passage states")


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        json.dumps({"context": "x" * 40}),
        json.dumps({"prefix": "x" * 40, "extra": 1}),
        json.dumps({"prefix": "short"}),
        json.dumps({"prefix": "x" * 601}),
        json.dumps({"prefix": "   "}),
        json.dumps({"prefix": 17}),
    ],
)
def test_parse_prefix_rejects_every_deviation(payload: str) -> None:
    with pytest.raises(ValueError):
        parse_prefix(payload)


def test_parse_summary_bounds() -> None:
    assert parse_summary(json.dumps({"summary": "x" * 200})) == "x" * 200
    with pytest.raises(ValueError):
        parse_summary(json.dumps({"summary": "x" * 1_201}))


def test_situate_messages_include_summary_only_when_given() -> None:
    with_summary = situate_messages("chunk body", document_summary="doc about refunds")
    without = situate_messages("chunk body", document_summary=None)
    assert with_summary[1]["content"].startswith("Document summary:")
    assert "Document summary" not in without[1]["content"]
    with pytest.raises(ValueError):
        situate_messages("   ", document_summary=None)


def test_prefixed_text_prepends_and_never_mutates_chunk() -> None:
    chunk = "original passage"
    combined = prefixed_text("A refund policy passage.", chunk)
    assert combined == "A refund policy passage.\n\noriginal passage"
    assert chunk == "original passage"
    with pytest.raises(ValueError):
        prefixed_text("  ", chunk)


def test_summary_messages_require_document_text() -> None:
    assert summary_messages("doc body")[1]["content"] == "doc body"
    with pytest.raises(ValueError):
        summary_messages("")
