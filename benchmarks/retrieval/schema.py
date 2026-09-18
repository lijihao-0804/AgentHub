"""Dataset schema and quality validation for the M3 retrieval baseline."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packages.core.canonical.json_hash import canonical_json_hash

_LOCATOR_TYPES = frozenset({"page", "section", "text_range"})
_SPLITS = frozenset({"dev", "holdout"})


@dataclass(frozen=True, slots=True)
class CorpusSection:
    section_key: str
    text: str


@dataclass(frozen=True, slots=True)
class CorpusDocument:
    document_key: str
    revision_key: str
    title: str
    sections: tuple[CorpusSection, ...]


@dataclass(frozen=True, slots=True)
class GroundTruth:
    document_key: str
    revision_key: str
    locator: dict[str, Any]
    relevant_text: str


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    case_id: str
    split: str
    query: str
    ground_truth: tuple[GroundTruth, ...]


@dataclass(frozen=True, slots=True)
class RetrievalDataset:
    dataset_version: str
    corpus: tuple[CorpusDocument, ...]
    cases: tuple[BenchmarkCase, ...]
    dataset_hash: str


def _non_empty(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _validate_locator(locator: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(locator, dict):
        raise ValueError(f"{field} must be an object")
    locator_type = _non_empty(locator.get("type"), f"{field}.type")
    if locator_type not in _LOCATOR_TYPES:
        raise ValueError(f"{field}.type is unsupported")
    if locator_type == "section":
        _non_empty(locator.get("section_key"), f"{field}.section_key")
    elif locator_type == "page":
        page = locator.get("page")
        if not isinstance(page, int) or page < 1:
            raise ValueError(f"{field}.page must be a positive integer")
    else:
        start = locator.get("char_start")
        end = locator.get("char_end")
        if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end:
            raise ValueError(f"{field} must contain a valid char range")
    return dict(locator)


def _parse_document(raw: Any, index: int) -> CorpusDocument:
    if not isinstance(raw, dict):
        raise ValueError(f"corpus[{index}] must be an object")
    sections_raw = raw.get("sections")
    if not isinstance(sections_raw, list) or not sections_raw:
        raise ValueError(f"corpus[{index}].sections must be non-empty")
    sections: list[CorpusSection] = []
    section_keys: set[str] = set()
    for section_index, section in enumerate(sections_raw):
        if not isinstance(section, dict):
            raise ValueError(f"corpus[{index}].sections[{section_index}] must be an object")
        section_key = _non_empty(
            section.get("section_key"), f"corpus[{index}].sections[{section_index}].section_key"
        )
        if section_key in section_keys:
            raise ValueError(f"duplicate section key: {section_key}")
        section_keys.add(section_key)
        sections.append(
            CorpusSection(
                section_key=section_key,
                text=_non_empty(
                    section.get("text"), f"corpus[{index}].sections[{section_index}].text"
                ),
            )
        )
    return CorpusDocument(
        document_key=_non_empty(raw.get("document_key"), f"corpus[{index}].document_key"),
        revision_key=_non_empty(raw.get("revision_key"), f"corpus[{index}].revision_key"),
        title=_non_empty(raw.get("title"), f"corpus[{index}].title"),
        sections=tuple(sections),
    )


def _parse_case(raw: Any, index: int) -> BenchmarkCase:
    if not isinstance(raw, dict):
        raise ValueError(f"cases[{index}] must be an object")
    ground_truth_raw = raw.get("ground_truth")
    if not isinstance(ground_truth_raw, list) or not ground_truth_raw:
        raise ValueError(f"cases[{index}].ground_truth must be non-empty")
    ground_truth: list[GroundTruth] = []
    for gt_index, item in enumerate(ground_truth_raw):
        if not isinstance(item, dict):
            raise ValueError(f"cases[{index}].ground_truth[{gt_index}] must be an object")
        ground_truth.append(
            GroundTruth(
                document_key=_non_empty(
                    item.get("document_key"),
                    f"cases[{index}].ground_truth[{gt_index}].document_key",
                ),
                revision_key=_non_empty(
                    item.get("revision_key"),
                    f"cases[{index}].ground_truth[{gt_index}].revision_key",
                ),
                locator=_validate_locator(
                    item.get("locator"),
                    field=f"cases[{index}].ground_truth[{gt_index}].locator",
                ),
                relevant_text=_non_empty(
                    item.get("relevant_text"),
                    f"cases[{index}].ground_truth[{gt_index}].relevant_text",
                ),
            )
        )
    return BenchmarkCase(
        case_id=_non_empty(raw.get("id"), f"cases[{index}].id"),
        split=_non_empty(raw.get("split"), f"cases[{index}].split"),
        query=_non_empty(raw.get("query"), f"cases[{index}].query"),
        ground_truth=tuple(ground_truth),
    )


def dataset_from_payload(
    payload: dict[str, Any],
    *,
    expected_case_count: int | None = 30,
) -> RetrievalDataset:
    if not isinstance(payload, dict):
        raise ValueError("dataset must be an object")
    version = _non_empty(payload.get("dataset_version"), "dataset_version")
    corpus_raw = payload.get("corpus")
    cases_raw = payload.get("cases")
    if not isinstance(corpus_raw, list) or not corpus_raw:
        raise ValueError("corpus must be non-empty")
    if not isinstance(cases_raw, list) or not cases_raw:
        raise ValueError("cases must be non-empty")
    corpus = tuple(_parse_document(item, index) for index, item in enumerate(corpus_raw))
    cases = tuple(_parse_case(item, index) for index, item in enumerate(cases_raw))
    dataset = RetrievalDataset(
        dataset_version=version,
        corpus=corpus,
        cases=cases,
        dataset_hash=canonical_json_hash(payload),
    )
    validate_dataset(dataset, expected_case_count=expected_case_count)
    return dataset


def validate_dataset(
    dataset: RetrievalDataset,
    *,
    expected_case_count: int | None = 30,
) -> None:
    if expected_case_count is not None and len(dataset.cases) != expected_case_count:
        raise ValueError(f"dataset must contain exactly {expected_case_count} cases")
    if not 8 <= len(dataset.corpus) <= 12:
        raise ValueError("corpus must contain between 8 and 12 documents")
    document_keys = {document.document_key for document in dataset.corpus}
    revision_keys = {document.revision_key for document in dataset.corpus}
    if len(document_keys) != len(dataset.corpus) or len(revision_keys) != len(dataset.corpus):
        raise ValueError("document_key and revision_key must be unique")
    case_ids: set[str] = set()
    queries: set[str] = set()
    split_counts = {split: 0 for split in _SPLITS}
    for case in dataset.cases:
        if case.case_id in case_ids:
            raise ValueError(f"duplicate case id: {case.case_id}")
        case_ids.add(case.case_id)
        if case.query in queries:
            raise ValueError(f"duplicate query: {case.query}")
        queries.add(case.query)
        if case.split not in _SPLITS:
            raise ValueError(f"unsupported split: {case.split}")
        split_counts[case.split] += 1
        for ground_truth in case.ground_truth:
            if ground_truth.document_key not in document_keys:
                raise ValueError(f"unknown ground-truth document: {ground_truth.document_key}")
            if ground_truth.revision_key not in revision_keys:
                raise ValueError(f"unknown ground-truth revision: {ground_truth.revision_key}")
    if not split_counts["dev"] or not split_counts["holdout"]:
        raise ValueError("both dev and holdout splits must be present")


def load_dataset(path: Path) -> RetrievalDataset:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return dataset_from_payload(payload)


__all__ = [
    "BenchmarkCase",
    "CorpusDocument",
    "CorpusSection",
    "GroundTruth",
    "RetrievalDataset",
    "dataset_from_payload",
    "load_dataset",
    "validate_dataset",
]
