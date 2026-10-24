"""Convert one QASPER split into JSON-friendly retrieval records."""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path
from typing import Any

_SPLITS = frozenset({"train", "dev", "test"})


def load_qasper(root: Path, split: str) -> dict[str, list[dict[str, Any]]]:
    """Load the requested QASPER v0.3 split without reading other splits."""

    if split not in _SPLITS:
        raise ValueError("split must be one of 'train', 'dev', or 'test'")
    path = Path(root) / f"qasper-{split}-v0.3.json"
    with path.open("r", encoding="utf-8") as stream:
        papers = json.load(stream)
    if not isinstance(papers, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return _convert(papers)


def _convert(papers: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    corpus: list[dict[str, Any]] = []
    queries: list[dict[str, Any]] = []
    query_ids: set[str] = set()
    for paper_id, paper in papers.items():
        if not isinstance(paper_id, str) or not isinstance(paper, dict):
            raise ValueError("each paper must have a string ID and object value")
        title = _string(paper, "title", paper_id)
        units = _paper_units(paper_id, title, paper)
        corpus.extend(unit[0] for unit in units)
        normalized = [unit[1] for unit in units]
        document_ids = [unit[0]["document_id"] for unit in units]
        ranges = _ranges(normalized, document_ids)
        for qa in _list(paper, "qas", paper_id):
            query = _query(paper_id, qa, normalized, ranges)
            if query["query_id"] in query_ids:
                raise ValueError(f"duplicate query ID: {query['query_id']}")
            query_ids.add(query["query_id"])
            queries.append(query)
    return {"corpus": corpus, "queries": queries}


def _paper_units(
    paper_id: str, title: str, paper: dict[str, Any]
) -> list[tuple[dict[str, Any], str]]:
    sources: list[tuple[str, str, str]] = [
        ("abstract", "Abstract", _string(paper, "abstract", paper_id))
    ]
    for section in _list(paper, "full_text", paper_id):
        if not isinstance(section, dict):
            raise ValueError(f"paper {paper_id} has a non-object section")
        heading = section.get("section_name")
        if heading is None:
            heading = ""
        if not isinstance(heading, str):
            raise ValueError(f"paper {paper_id} has invalid section_name")
        for paragraph in _list(section, "paragraphs", paper_id):
            if not isinstance(paragraph, str):
                raise ValueError(f"paper {paper_id} has a non-string paragraph")
            sources.append(("full_text", heading, paragraph))
    for figure in _list(paper, "figures_and_tables", paper_id):
        if not isinstance(figure, dict):
            raise ValueError(f"paper {paper_id} has a non-object caption")
        sources.append(("caption", "Captions", _string(figure, "caption", paper_id)))

    units: list[tuple[dict[str, Any], str]] = []
    for ordinal, (kind, section, text) in enumerate(sources):
        document_id = (
            f"{paper_id}:abstract"
            if kind == "abstract"
            else f"{paper_id}:{kind}:{ordinal}"
        )
        record = {
            "document_id": document_id,
            "paper_id": paper_id,
            "title": title,
            "section": section,
            "text": text,
            "ordinal": ordinal,
        }
        units.append((record, _normalize(text)))
    return units


def _query(
    paper_id: str,
    qa: Any,
    normalized: list[str],
    ranges: list[tuple[int, int, str]],
) -> dict[str, Any]:
    if not isinstance(qa, dict):
        raise ValueError(f"paper {paper_id} has a non-object question")
    query_id = _string(qa, "question_id", paper_id)
    question = _string(qa, "question", paper_id)
    answers: list[str] = []
    evidence_sets: list[list[str]] = []
    unaligned: list[str] = []
    answerable = False
    alignment_complete = True
    for annotation in _list(qa, "answers", query_id):
        if not isinstance(annotation, dict) or not isinstance(annotation.get("answer"), dict):
            raise ValueError(f"question {query_id} has an invalid annotation")
        answer = annotation["answer"]
        if answer.get("unanswerable") is True:
            answers.append("Unanswerable")
            evidence_sets.append([])
            continue
        answerable = True
        answers.append(_answer_text(answer, query_id))
        evidence = _list(answer, "evidence", query_id)
        aligned, missed = _align(evidence, normalized, ranges)
        evidence_sets.append(aligned)
        unaligned.extend(missed)
        alignment_complete = alignment_complete and not missed and bool(evidence)
    if not answers:
        raise ValueError(f"question {query_id} has no annotations")
    return {
        "query_id": query_id,
        "paper_id": paper_id,
        "query": question,
        "answerable": answerable,
        "evidence_alignment_complete": alignment_complete,
        "evidence_sets": evidence_sets,
        "answers": answers,
        "unaligned_evidence": unaligned,
    }


def _answer_text(answer: dict[str, Any], query_id: str) -> str:
    spans = answer.get("extractive_spans")
    if isinstance(spans, list) and spans:
        return ", ".join(str(span) for span in spans)
    free_form = answer.get("free_form_answer")
    if isinstance(free_form, str) and free_form:
        return free_form
    if answer.get("yes_no") is True:
        return "Yes"
    if answer.get("yes_no") is False:
        return "No"
    raise ValueError(f"annotation for question {query_id} has no answer")


def _align(
    evidence: list[Any],
    normalized: list[str],
    ranges: list[tuple[int, int, str]],
) -> tuple[list[str], list[str]]:
    aligned: list[str] = []
    unaligned: list[str] = []
    joined = " ".join(normalized)
    for value in evidence:
        if not isinstance(value, str) or not _normalize(value):
            unaligned.append(str(value))
            continue
        # QASPER's UI marks a selected figure/table caption with this literal
        # annotation prefix. Remove the UI marker, never invent missing text.
        needle = _normalize(value.removeprefix("FLOAT SELECTED:"))
        sequences: set[tuple[str, ...]] = set()
        start = joined.find(needle)
        while start >= 0:
            end = start + len(needle)
            document_ids = tuple(
                document_id
                for left, right, document_id in ranges
                if left < end and right > start
            )
            sequences.add(document_ids)
            start = joined.find(needle, start + 1)
        if len(sequences) != 1:
            unaligned.append(value)
            continue
        for document_id in next(iter(sequences)):
            if document_id not in aligned:
                aligned.append(document_id)
    return aligned, unaligned


def _ranges(
    normalized: list[str], document_ids: list[str]
) -> list[tuple[int, int, str]]:
    ranges: list[tuple[int, int, str]] = []
    cursor = 0
    for text, document_id in zip(normalized, document_ids, strict=True):
        ranges.append((cursor, cursor + len(text), document_id))
        cursor += len(text) + 1
    return ranges


def _normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split())


def _string(record: dict[str, Any], field: str, owner: str) -> str:
    value = record.get(field)
    if not isinstance(value, str):
        raise ValueError(f"{owner} has invalid {field}")
    return value


def _list(record: dict[str, Any], field: str, owner: str) -> list[Any]:
    value = record.get(field)
    if not isinstance(value, list):
        raise ValueError(f"{owner} has invalid {field}")
    return value


__all__ = ["load_qasper"]
