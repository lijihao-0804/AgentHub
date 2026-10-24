"""Prepare label-blind QASPER TRAIN paragraphs for the next RAG development run."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any, Final

from benchmarks.public_rag import paper_qa_data

_TRAIN_NAME: Final = "qasper-train-v0.3.json"
_ANSWER_TYPES: Final = frozenset({"extractive", "free_form", "yes_no", "unanswerable"})
_DEFAULT_SEED: Final = 20261101
_DEFAULT_MAX_PAPERS: Final = 24
_DEFAULT_MAX_QUESTIONS: Final = 100
_KNOWN_EXPOSURE_LEDGERS: Final = (
    "benchmarks/public_rag/results/paper-qa-live-20261023/frozen-dataset.json",
    "benchmarks/public_rag/results/scenario-rag-20261023/qasper-dev-1/run.jsonl",
)


def prepare(
    root: Path = Path("data/external/rag-public-20261021/qasper/extracted"),
    *,
    seed: int = _DEFAULT_SEED,
    max_papers: int = _DEFAULT_MAX_PAPERS,
    max_questions: int = _DEFAULT_MAX_QUESTIONS,
) -> dict[str, object]:
    """Return a seeded, previously unexposed QASPER TRAIN development sample.

    Paper IDs are filtered before shuffled selection. The existing paper-QA
    adapter's validated conversion helpers build every corpus paper and case.
    """

    if seed < 0:
        raise ValueError("seed must be non-negative")
    if max_papers < 1:
        raise ValueError("max_papers must be positive")
    if max_questions < 1:
        raise ValueError("max_questions must be positive")

    root = Path(root)
    train_path = _train_path(root)
    papers = _load_json_object(train_path)
    known_exposure = _known_train_exposure()
    missing_exposure_ids = sorted(known_exposure - set(papers))
    if missing_exposure_ids:
        raise ValueError(
            "known exposure ledgers contain unknown TRAIN paper IDs: "
            + ", ".join(missing_exposure_ids)
        )

    eligible_paper_ids = sorted(set(papers) - known_exposure)
    if len(eligible_paper_ids) < max_papers:
        raise ValueError(
            f"requested {max_papers} papers but only {len(eligible_paper_ids)} are unexposed"
        )
    shuffled_paper_ids = list(eligible_paper_ids)
    random.Random(seed).shuffle(shuffled_paper_ids)
    selected_paper_ids = shuffled_paper_ids[:max_papers]
    selected_id_set = set(selected_paper_ids)
    ledger = _question_ledger(selected_paper_ids, papers)
    question_ids_by_paper = {entry["paper_id"]: entry["question_ids"] for entry in ledger}
    selected_qids = paper_qa_data._round_robin_questions(
        selected_paper_ids,
        question_ids_by_paper,
        max_questions=max_questions,
    )
    qid_index = {
        (paper_id, paper_qa_data._question_id(question, paper_id)): (paper_id, question)
        for paper_id in selected_paper_ids
        for question in _list(_paper_for(papers, paper_id), "qas")
        if isinstance(question, dict)
    }
    cases = [
        paper_qa_data._case(paper_id, qid_index[(paper_id, query_id)][1])
        for paper_id, query_id in selected_qids
    ]
    corpus = [
        paper_qa_data._corpus_paper(paper_id, _paper_for(papers, paper_id))
        for paper_id in selected_paper_ids
    ]

    if any(item.get("paper_id") not in selected_id_set for item in corpus + cases):
        raise ValueError("paper-QA adapter returned a case outside the selected papers")
    _validate_corpus(corpus)
    _validate_cases(cases)
    selected_question_ids = [item["query_id"] for item in cases]
    ledger_question_ids = [question_id for entry in ledger for question_id in entry["question_ids"]]
    if not set(selected_question_ids).issubset(ledger_question_ids):
        raise ValueError("selected cases contain a question absent from the ledger")

    return {
        "corpus": corpus,
        "cases": cases,
        "sampling": {
            "seed": seed,
            "max_papers": max_papers,
            "max_questions": max_questions,
            "selected_paper_ids": selected_paper_ids,
            "selected_paper_count": len(selected_paper_ids),
            "selected_paper_question_count": len(ledger_question_ids),
            "selected_question_count": len(cases),
            "question_ledger": ledger,
            "paper_order": (
                "sorted eligible official TRAIN paper IDs, then random.Random(seed).shuffle"
            ),
            "question_order": (
                "official QASPER question order; one question per selected paper per round"
            ),
            "label_blind_sampling": True,
            "label_use": (
                "Paper IDs were selected before annotation parsing; labels are retained "
                "only after selection for cases and the audit."
            ),
            "all_questions_retained_for_selected_papers": len(cases) == len(ledger_question_ids),
            "all_selected_paper_question_ids_in_ledger": True,
            "selected_question_ids": selected_question_ids,
            "question_cap_shortfall": max(0, max_questions - len(cases)),
            "question_cap_fill_policy": (
                "Use only the selected papers; never add a next paper to fill the cap."
            ),
            "known_exposure": {
                "paper_count": len(known_exposure),
                "sources": list(_KNOWN_EXPOSURE_LEDGERS),
                "selected_paper_overlap": sorted(selected_id_set & known_exposure),
            },
        },
        "source": {
            "dataset": "qasper",
            "version": "0.3",
            "split": "train",
            "file": _TRAIN_NAME,
            "paper_total": len(papers),
            "question_total": sum(
                len(_list(_paper_for(papers, paper_id), "qas")) for paper_id in papers
            ),
            "annotation_origin": "QASPER human annotations",
            "model_generated_annotations": False,
            "dev_test_accessed": False,
            "scope": "train_development",
            "scope_note": (
                "QASPER TRAIN development sample isolated by known paper-ID exposure; "
                "not a new formal holdout or independent confirmation."
            ),
            "sibling_splits_accessed": False,
        },
    }


def _known_train_exposure() -> set[str]:
    """Read only paper IDs from completed QASPER TRAIN exposure ledgers."""

    repository = Path(__file__).resolve().parents[2]
    exposed: set[str] = set()
    for relative in _KNOWN_EXPOSURE_LEDGERS:
        path = repository / relative
        if relative.endswith(".jsonl"):
            with path.open("r", encoding="utf-8") as stream:
                for line_number, line in enumerate(stream, start=1):
                    if not line.strip():
                        continue
                    row = _object(json.loads(line), f"{relative}:{line_number}")
                    exposed.add(_paper_id(row.get("group_id"), f"{relative}:{line_number}"))
        else:
            dataset = _object(json.loads(path.read_text(encoding="utf-8")), relative)
            sampling = _object(dataset.get("sampling"), f"{relative} sampling")
            ids = _string_list(
                sampling.get("selected_paper_ids"),
                owner=f"{relative} sampling",
                field="selected_paper_ids",
            )
            if not ids:
                raise ValueError(f"{relative} contains no exposed paper IDs")
            exposed.update(_paper_id(value, relative) for value in ids)
    return exposed


def _train_path(root: Path) -> Path:
    candidates = (root / _TRAIN_NAME, root / "extracted" / _TRAIN_NAME)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    searched = ", ".join(str(candidate) for candidate in candidates)
    raise FileNotFoundError(f"QASPER TRAIN split not found; searched {searched}")


def _load_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    if not value:
        raise ValueError(f"{path.name} contains no papers")
    return value


def _question_ledger(
    paper_ids: list[str],
    papers: dict[str, Any],
) -> list[dict[str, object]]:
    ledger: list[dict[str, object]] = []
    for paper_id in paper_ids:
        question_ids = [
            _question_id(_object(qa, f"question in paper {paper_id}"), paper_id)
            for qa in _list(_paper_for(papers, paper_id), "qas")
        ]
        if len(question_ids) != len(set(question_ids)):
            raise ValueError(f"paper {paper_id} contains duplicate question IDs")
        ledger.append(
            {
                "paper_id": paper_id,
                "question_ids": question_ids,
                "question_count": len(question_ids),
            }
        )
    return ledger


def _validate_corpus(corpus: list[dict[str, Any]]) -> None:
    for paper in corpus:
        if set(paper) != {"paper_id", "title", "abstract", "sections"}:
            raise ValueError(f"paper {paper.get('paper_id')} has an unexpected corpus schema")
        for section in _object_list(paper.get("sections"), "corpus sections"):
            if set(section) != {"heading", "paragraphs"}:
                raise ValueError("corpus section must contain heading and paragraphs")
            _string(section.get("heading"), "corpus section heading")
            _string_list(section.get("paragraphs"), owner="corpus section", field="paragraphs")


def _validate_cases(cases: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    for case in cases:
        if set(case) != {
            "query_id",
            "paper_id",
            "question",
            "annotations",
            "consensus",
            "scorable",
        }:
            raise ValueError(f"case {case.get('query_id')} has an unexpected schema")
        query_id = _string(case.get("query_id"), "case query_id")
        if query_id in seen:
            raise ValueError(f"duplicate case query_id: {query_id}")
        seen.add(query_id)
        annotations = _object_list(case.get("annotations"), f"case {query_id} annotations")
        if not annotations:
            raise ValueError(f"case {query_id} has no annotations")
        for annotation in annotations:
            allowed = {"answer_type", "answer", "evidence", "highlighted_evidence"}
            if not set(annotation).issubset(allowed):
                raise ValueError(f"case {query_id} has an unexpected annotation schema")
            if annotation.get("answer_type") not in _ANSWER_TYPES:
                raise ValueError(f"case {query_id} has an unsupported answer type")
            if "highlighted_evidence" not in annotation:
                raise ValueError(f"case {query_id} lost highlighted_evidence")
        answerable_count = sum(
            annotation["answer_type"] != "unanswerable" for annotation in annotations
        )
        expected = (
            "answerable"
            if answerable_count == len(annotations)
            else "unanswerable"
            if answerable_count == 0
            else "mixed"
        )
        if case.get("consensus") != expected or case.get("scorable") is not (expected != "mixed"):
            raise ValueError(f"case {query_id} has inconsistent consensus or scorable")


def _paper_for(papers: dict[str, Any], paper_id: str) -> dict[str, Any]:
    return _object(papers.get(paper_id), f"paper {paper_id}")


def _paper_id(value: Any, owner: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{owner} has an invalid paper ID")
    return value


def _question_id(question: dict[str, Any], paper_id: str) -> str:
    value = question.get("question_id")
    if not isinstance(value, str) or not value:
        raise ValueError(f"paper {paper_id} has an invalid question_id")
    return value


def _object(value: Any, owner: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{owner} must be a JSON object")
    return value


def _object_list(value: Any, owner: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"{owner} must be a list of JSON objects")
    return value


def _string(value: Any, owner: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{owner} must be a string")
    return value


def _string_list(value: Any, *, owner: str, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{owner} has an invalid {field}")
    return value


def _list(record: dict[str, Any], field: str) -> list[Any]:
    value = record.get(field)
    if not isinstance(value, list):
        raise ValueError(f"record has an invalid {field}")
    return value


__all__ = ["prepare"]
