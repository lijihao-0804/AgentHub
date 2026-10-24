from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.public_rag.data import SourceDocument, load_scifact, query_groups


@pytest.fixture
def scifact_root(tmp_path: Path) -> Path:
    root = tmp_path / "scifact"
    root.mkdir()
    _write_jsonl(
        root / "corpus.jsonl",
        [
            {
                "_id": "31715818",
                "title": "  Alpha title  \n",
                "text": "\nAlpha body.  ",
            },
            {
                "_id": "14717500",
                "title": "Beta title",
                "text": "Beta body.",
            },
            {
                "_id": "unused-document",
                "title": "Unused title",
                "text": "Unused body.",
            },
        ],
    )
    _write_jsonl(
        root / "queries.jsonl",
        [
            {"_id": "1", "text": "First upstream question?"},
            {"_id": "2", "text": "Second upstream question?"},
            {"_id": "4", "text": "Question outside this split?"},
        ],
    )
    (root / "qrels").mkdir()
    _write_qrels(
        root / "qrels" / "train.tsv",
        [
            ("1", "31715818", "1"),
            ("2", "14717500", "2"),
        ],
    )
    _write_qrels(root / "qrels" / "test.tsv", [("2", "14717500", "1")])
    return root


def test_load_scifact_selects_only_queries_in_requested_split(
    scifact_root: Path,
) -> None:
    train = load_scifact(scifact_root, "train")
    test = load_scifact(scifact_root, "test")

    assert set(train.queries) == {"1", "2"}
    assert set(test.queries) == {"2"}
    assert set(train.corpus) == {"31715818", "14717500", "unused-document"}
    assert train.qrels == {
        "1": {"31715818": 1},
        "2": {"14717500": 2},
    }
    assert test.qrels == {"2": {"14717500": 1}}


def test_source_document_preserves_fields_and_builds_retrieval_text(
    scifact_root: Path,
) -> None:
    data = load_scifact(scifact_root, "train")
    document = data.corpus["31715818"]

    assert data.queries["1"] == "First upstream question?"
    assert document.document_id == "31715818"
    assert document.title == "  Alpha title  \n"
    assert document.text == "\nAlpha body.  "
    assert document.retrieval_text == "Alpha title\n\nAlpha body."
    assert SourceDocument("doc", " title ", " text ").retrieval_text == "title\n\ntext"


def test_load_scifact_rejects_duplicate_corpus_ids(scifact_root: Path) -> None:
    _write_jsonl(
        scifact_root / "corpus.jsonl",
        [
            {"_id": "31715818", "title": "First", "text": "Text"},
            {"_id": "31715818", "title": "Duplicate", "text": "Text"},
        ],
    )
    with pytest.raises(ValueError, match="duplicate corpus document ID"):
        load_scifact(scifact_root, "train")


def test_load_scifact_rejects_duplicate_query_ids(scifact_root: Path) -> None:
    _write_jsonl(
        scifact_root / "queries.jsonl",
        [
            {"_id": "1", "text": "First"},
            {"_id": "1", "text": "Duplicate"},
        ],
    )
    with pytest.raises(ValueError, match="duplicate query ID"):
        load_scifact(scifact_root, "train")


def test_load_scifact_rejects_conflicting_qrels(scifact_root: Path) -> None:
    _write_qrels(
        scifact_root / "qrels" / "train.tsv",
        [
            ("1", "31715818", "1"),
            ("1", "31715818", "2"),
        ],
    )

    with pytest.raises(ValueError, match="conflicting qrels"):
        load_scifact(scifact_root, "train")


@pytest.mark.parametrize(
    ("qrels", "message", "replace_query"),
    [
        ([("999", "31715818", "1")], "missing queries", False),
        ([("1", "99999999", "1")], "missing relevant corpus documents", True),
    ],
)
def test_load_scifact_rejects_missing_qrel_references(
    scifact_root: Path,
    qrels: list[tuple[str, str, str]],
    message: str,
    replace_query: bool,
) -> None:
    if replace_query:
        _write_jsonl(
            scifact_root / "queries.jsonl",
            [{"_id": "1", "text": "First upstream question?"}],
        )
    _write_qrels(scifact_root / "qrels" / "train.tsv", qrels)

    with pytest.raises(ValueError, match=message):
        load_scifact(scifact_root, "train")


def test_load_scifact_rejects_unsupported_split(scifact_root: Path) -> None:
    with pytest.raises(ValueError, match="train.*test"):
        load_scifact(scifact_root, "dev")


def test_query_groups_follow_transitive_positive_gold_links() -> None:
    qrels = {
        "q1": {"doc-a": 1},
        "q2": {"doc-a": 1, "doc-b": 2},
        "q3": {"doc-b": 1},
        "q4": {"doc-a": 0},
        "q5": {"doc-c": 1},
    }

    groups = query_groups(qrels)

    assert groups["q1"] == groups["q2"] == groups["q3"] == "group-0000"
    assert groups["q4"] == "group-0001"
    assert groups["q5"] == "group-0002"
    assert query_groups(dict(reversed(list(qrels.items())))) == groups


def _write_jsonl(path: Path, records: list[dict[str, str]]) -> None:
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def _write_qrels(path: Path, rows: list[tuple[str, str, str]]) -> None:
    path.write_text(
        "query-id\tcorpus-id\tscore\n" + "".join("\t".join(row) + "\n" for row in rows),
        encoding="utf-8",
    )
