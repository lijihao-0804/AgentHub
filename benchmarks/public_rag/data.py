"""Lightweight BEIR dataset loading with upstream identifiers preserved."""

from __future__ import annotations

import csv
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

_BEIR_SPLITS: Final = frozenset({"train", "dev", "test"})
_SCIFACT_SPLITS: Final = frozenset({"train", "test"})
_QREL_FIELDS: Final = ("query-id", "corpus-id", "score")


@dataclass(frozen=True, slots=True)
class SourceDocument:
    document_id: str
    title: str
    text: str

    @property
    def retrieval_text(self) -> str:
        return self.title.strip() + "\n\n" + self.text.strip()


@dataclass(frozen=True, slots=True)
class SciFactData:
    corpus: dict[str, SourceDocument]
    queries: dict[str, str]
    qrels: dict[str, dict[str, int]]


def load_scifact(root: Path, split: str) -> SciFactData:
    """Load one SciFact train/test split without changing upstream IDs or grades."""

    if split not in _SCIFACT_SPLITS:
        raise ValueError("split must be either 'train' or 'test'")
    return load_beir(root, split)


def load_beir(root: Path, split: str) -> SciFactData:
    """Load one BEIR train/dev/test split without changing IDs or grades."""

    if split not in _BEIR_SPLITS:
        raise ValueError("split must be one of 'train', 'dev', or 'test'")
    root = Path(root)
    qrels = _load_qrels(root / "qrels" / f"{split}.tsv")
    corpus = _load_corpus(root / "corpus.jsonl")
    all_queries = _load_queries(root / "queries.jsonl")

    missing_queries = sorted(set(qrels).difference(all_queries))
    if missing_queries:
        raise ValueError(f"qrels reference missing queries: {missing_queries}")

    positive_document_ids = {
        document_id
        for grades in qrels.values()
        for document_id, grade in grades.items()
        if grade > 0
    }
    missing_documents = sorted(positive_document_ids.difference(corpus))
    if missing_documents:
        raise ValueError(f"qrels reference missing relevant corpus documents: {missing_documents}")

    selected_queries = {
        query_id: text for query_id, text in all_queries.items() if query_id in qrels
    }
    return SciFactData(corpus=corpus, queries=selected_queries, qrels=qrels)


def query_groups(qrels: dict[str, dict[str, int]]) -> dict[str, str]:
    """Map each query to a deterministic component joined by positive gold documents."""

    query_ids = sorted(qrels)
    for query_id in query_ids:
        _validate_identifier(query_id, field="query ID")

    parent = {query_id: query_id for query_id in query_ids}

    def find(query_id: str) -> str:
        while parent[query_id] != query_id:
            parent[query_id] = parent[parent[query_id]]
            query_id = parent[query_id]
        return query_id

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return
        first, second = sorted((left_root, right_root))
        parent[second] = first

    first_query_by_document: dict[str, str] = {}
    for query_id in query_ids:
        for document_id, grade in qrels[query_id].items():
            _validate_identifier(document_id, field="document ID")
            _validate_grade(grade, query_id=query_id, document_id=document_id)
            if grade <= 0:
                continue
            first_query = first_query_by_document.setdefault(document_id, query_id)
            union(first_query, query_id)

    components: dict[str, list[str]] = {}
    for query_id in query_ids:
        components.setdefault(find(query_id), []).append(query_id)

    ordered_components = sorted(
        (sorted(component) for component in components.values()),
        key=lambda component: tuple(component),
    )
    return {
        query_id: f"group-{group_index:04d}"
        for group_index, component in enumerate(ordered_components)
        for query_id in component
    }


def _load_corpus(path: Path) -> dict[str, SourceDocument]:
    corpus: dict[str, SourceDocument] = {}
    for line_number, record in _iter_jsonl(path):
        document_id = _required_identifier(record, "_id", path=path, line_number=line_number)
        title = _required_string(record, "title", path=path, line_number=line_number)
        text = _required_string(record, "text", path=path, line_number=line_number)
        if document_id in corpus:
            raise ValueError(f"duplicate corpus document ID: {document_id}")
        corpus[document_id] = SourceDocument(document_id=document_id, title=title, text=text)
    return corpus


def _load_queries(path: Path) -> dict[str, str]:
    queries: dict[str, str] = {}
    for line_number, record in _iter_jsonl(path):
        query_id = _required_identifier(record, "_id", path=path, line_number=line_number)
        text = _required_string(record, "text", path=path, line_number=line_number)
        if not text.strip():
            raise ValueError(f"{path.name} line {line_number} has an empty query text")
        if query_id in queries:
            raise ValueError(f"duplicate query ID: {query_id}")
        queries[query_id] = text
    return queries


def _load_qrels(path: Path) -> dict[str, dict[str, int]]:
    qrels: dict[str, dict[str, int]] = {}
    with path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream, delimiter="\t")
        fieldnames = reader.fieldnames or ()
        if len(fieldnames) != len(set(fieldnames)):
            raise ValueError(f"{path.name} has duplicate qrel fields")
        missing_fields = set(_QREL_FIELDS).difference(fieldnames)
        if missing_fields:
            raise ValueError(f"{path.name} is missing qrel fields: {sorted(missing_fields)}")

        for line_number, row in enumerate(reader, start=2):
            if None in row or any(row.get(field) is None for field in _QREL_FIELDS):
                raise ValueError(f"{path.name} line {line_number} has malformed fields")
            query_id = row["query-id"]
            document_id = row["corpus-id"]
            _validate_identifier(
                query_id, field="qrel query ID", path=path, line_number=line_number
            )
            _validate_identifier(
                document_id,
                field="qrel document ID",
                path=path,
                line_number=line_number,
            )
            try:
                grade = int(row["score"].strip())
            except (AttributeError, ValueError) as exc:
                raise ValueError(
                    f"{path.name} line {line_number} has a non-integer relevance score"
                ) from exc
            _validate_grade(grade, query_id=query_id, document_id=document_id)

            query_qrels = qrels.setdefault(query_id, {})
            prior_grade = query_qrels.get(document_id)
            if prior_grade is not None and prior_grade != grade:
                raise ValueError(
                    f"conflicting qrels for query {query_id} and document {document_id}"
                )
            query_qrels[document_id] = grade
    return qrels


def _iter_jsonl(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                raise ValueError(f"{path.name} line {line_number} is blank")
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path.name} line {line_number} is invalid JSON") from exc
            if not isinstance(record, dict):
                raise ValueError(f"{path.name} line {line_number} must contain a JSON object")
            yield line_number, record


def _required_identifier(
    record: dict[str, Any],
    field: str,
    *,
    path: Path,
    line_number: int,
) -> str:
    value = _required_string(record, field, path=path, line_number=line_number)
    _validate_identifier(value, field=field, path=path, line_number=line_number)
    return value


def _required_string(
    record: dict[str, Any],
    field: str,
    *,
    path: Path,
    line_number: int,
) -> str:
    value = record.get(field)
    if not isinstance(value, str):
        raise ValueError(f"{path.name} line {line_number} has invalid {field}")
    return value


def _validate_identifier(
    value: object,
    *,
    field: str,
    path: Path | None = None,
    line_number: int | None = None,
) -> None:
    if not isinstance(value, str) or not value.strip():
        location = f"{path.name} line {line_number}: " if path is not None else ""
        raise ValueError(f"{location}{field} must be a non-empty string")


def _validate_grade(grade: object, *, query_id: str, document_id: str) -> None:
    if isinstance(grade, bool) or not isinstance(grade, int):
        raise TypeError(f"relevance score for {query_id}/{document_id} must be an integer")
    if grade < 0:
        raise ValueError(f"relevance score for {query_id}/{document_id} must be non-negative")


__all__ = ["SciFactData", "SourceDocument", "load_beir", "load_scifact", "query_groups"]
