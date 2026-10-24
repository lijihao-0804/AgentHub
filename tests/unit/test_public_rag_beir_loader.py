from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.public_rag.data import SourceDocument, load_beir, load_scifact


@pytest.fixture
def beir_root(tmp_path: Path) -> Path:
    root = tmp_path / "beir"
    root.mkdir()
    _write_jsonl(
        root / "corpus.jsonl",
        [
            {"_id": "doc-a", "title": "Alpha", "text": "First passage."},
            {"_id": "doc-b", "title": "Beta", "text": "Second passage."},
            {"_id": "doc-unused", "title": "Unused", "text": "Unused passage."},
        ],
    )
    _write_jsonl(
        root / "queries.jsonl",
        [
            {"_id": "q-1", "text": "First query?"},
            {"_id": "q-2", "text": "Second query?"},
            {"_id": "q-unused", "text": "Query outside every split?"},
        ],
    )
    qrels_dir = root / "qrels"
    qrels_dir.mkdir()
    _write_qrels(qrels_dir / "train.tsv", [("q-1", "doc-a", "0")])
    _write_qrels(
        qrels_dir / "dev.tsv",
        [
            ("q-1", "doc-a", "0"),
            ("q-1", "doc-b", "1"),
            ("q-2", "doc-b", "2"),
            ("q-2", "doc-a", "3"),
        ],
    )
    _write_qrels(qrels_dir / "test.tsv", [("q-2", "doc-unused", "1")])
    return root


def test_load_beir_loads_dev_and_preserves_upstream_ids_and_grades(
    beir_root: Path,
) -> None:
    data = load_beir(beir_root, "dev")

    assert data.corpus["doc-a"] == SourceDocument("doc-a", "Alpha", "First passage.")
    assert set(data.corpus) == {"doc-a", "doc-b", "doc-unused"}
    assert data.queries == {"q-1": "First query?", "q-2": "Second query?"}
    assert data.qrels == {
        "q-1": {"doc-a": 0, "doc-b": 1},
        "q-2": {"doc-b": 2, "doc-a": 3},
    }


@pytest.mark.parametrize("split", ["train", "dev", "test"])
def test_load_beir_accepts_standard_splits(beir_root: Path, split: str) -> None:
    data = load_beir(beir_root, split)

    assert set(data.queries) == set(data.qrels)
    assert "q-unused" not in data.queries


@pytest.mark.parametrize(
    ("qrels", "message"),
    [
        ([("missing-query", "doc-a", "1")], "missing queries"),
        ([("q-1", "missing-document", "1")], "missing relevant corpus documents"),
    ],
)
def test_load_beir_rejects_missing_qrel_references(
    beir_root: Path,
    qrels: list[tuple[str, str, str]],
    message: str,
) -> None:
    _write_qrels(beir_root / "qrels" / "dev.tsv", qrels)

    with pytest.raises(ValueError, match=message):
        load_beir(beir_root, "dev")


def test_load_scifact_rejects_dev_with_legacy_error_while_load_beir_accepts_it(
    beir_root: Path,
) -> None:
    with pytest.raises(ValueError) as exc_info:
        load_scifact(beir_root, "dev")

    assert str(exc_info.value) == "split must be either 'train' or 'test'"
    assert load_beir(beir_root, "dev").qrels["q-2"] == {"doc-b": 2, "doc-a": 3}


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
