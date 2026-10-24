"""Load MuSiQue records as question-local retrieval cases."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

_SPLITS: Final = frozenset({"train", "dev", "test"})
_VARIANTS: Final = frozenset({"ans", "full"})
_HOP_PREFIX: Final = re.compile(r"^(?P<hops>[1-9]\d*)hop", re.IGNORECASE)


def load_musique(root: Path, split: str, variant: str = "ans") -> dict[str, object]:
    """Load one MuSiQue split with every question-local paragraph retained.

    Unlabelled splits use ``answerable=None`` and empty answer/evidence labels.
    ``question_decomposition`` contributes only the hop count and never retrieval text.
    """

    if split not in _SPLITS:
        raise ValueError("split must be one of 'train', 'dev', or 'test'")
    if variant not in _VARIANTS:
        raise ValueError("variant must be either 'ans' or 'full'")

    path = Path(root) / f"musique_{variant}_v1.0_{split}.jsonl"
    documents: list[dict[str, object]] = []
    queries: list[dict[str, object]] = []
    seen_case_ids: set[str] = set()
    occurrence_by_query_id: dict[str, int] = {}

    for line_number, record in _iter_jsonl(path):
        query_id = _required_text(record, "id", path=path, line_number=line_number)
        occurrence = occurrence_by_query_id.get(query_id, 0)
        occurrence_by_query_id[query_id] = occurrence + 1
        case_id = query_id if occurrence == 0 else f"{query_id}::{occurrence + 1}"
        if case_id in seen_case_ids:
            raise ValueError(f"{path.name} line {line_number} has duplicate case id {case_id!r}")
        seen_case_ids.add(case_id)

        question = _required_text(record, "question", path=path, line_number=line_number)
        paragraphs = _required_list(record, "paragraphs", path=path, line_number=line_number)
        supporting_ids: list[str] = []
        paragraph_ids: set[str] = set()

        for paragraph in paragraphs:
            if not isinstance(paragraph, dict):
                raise ValueError(f"{path.name} line {line_number} has a non-object paragraph")
            ordinal = paragraph.get("idx")
            if type(ordinal) is not int or ordinal < 0:
                raise ValueError(f"{path.name} line {line_number} has an invalid paragraph idx")
            title = _required_string(paragraph, "title", path=path, line_number=line_number)
            text = _required_string(paragraph, "paragraph_text", path=path, line_number=line_number)
            is_supporting = paragraph.get("is_supporting", False)
            if type(is_supporting) is not bool:
                raise ValueError(f"{path.name} line {line_number} has invalid is_supporting")

            document_id = f"{case_id}::paragraph::{ordinal}"
            if document_id in paragraph_ids:
                raise ValueError(f"{path.name} line {line_number} has duplicate paragraph idx")
            paragraph_ids.add(document_id)
            documents.append(
                {
                    "document_id": document_id,
                    "case_id": case_id,
                    "title": title,
                    "text": text,
                    "ordinal": ordinal,
                }
            )
            if is_supporting:
                supporting_ids.append(document_id)

        answerable = record.get("answerable")
        answer = record.get("answer")
        labelled = answerable is not None and answer is not None
        unlabelled = not labelled
        if labelled:
            if type(answerable) is not bool:
                raise ValueError(f"{path.name} line {line_number} has invalid answerable")
            if not isinstance(answer, str):
                raise ValueError(f"{path.name} line {line_number} has invalid answer")
            aliases = record.get("answer_aliases", [])
            if not isinstance(aliases, list) or any(not isinstance(item, str) for item in aliases):
                raise ValueError(f"{path.name} line {line_number} has invalid answer_aliases")
            answers = [answer, *aliases]
        else:
            answerable = None
            answers = []

        decomposition = record.get("question_decomposition")
        if decomposition is None:
            hops = _hops_from_id(query_id, path=path, line_number=line_number)
        else:
            if not isinstance(decomposition, list) or not decomposition:
                raise ValueError(
                    f"{path.name} line {line_number} has invalid question_decomposition"
                )
            hops = len(decomposition)

        queries.append(
            {
                "query_id": query_id,
                "case_id": case_id,
                "query": question,
                "answerable": answerable,
                "unlabelled": unlabelled,
                "evidence_sets": [supporting_ids] if supporting_ids else [],
                "answers": answers,
                "hops": hops,
            }
        )

    return {"documents": documents, "queries": queries}


def _hops_from_id(query_id: str, *, path: Path, line_number: int) -> int:
    match = _HOP_PREFIX.match(query_id)
    if match is None:
        raise ValueError(
            f"{path.name} line {line_number} cannot determine hops without decomposition"
        )
    return int(match.group("hops"))


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


def _required_text(
    record: dict[str, Any],
    field: str,
    *,
    path: Path,
    line_number: int,
) -> str:
    value = _required_string(record, field, path=path, line_number=line_number)
    if not value.strip():
        raise ValueError(f"{path.name} line {line_number} has an empty {field}")
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


def _required_list(
    record: dict[str, Any],
    field: str,
    *,
    path: Path,
    line_number: int,
) -> list[Any]:
    value = record.get(field)
    if not isinstance(value, list):
        raise ValueError(f"{path.name} line {line_number} has invalid {field}")
    return value


__all__ = ["load_musique"]
