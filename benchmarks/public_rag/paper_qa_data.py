"""Prepare paper-local QASPER question answering data from the official train split."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any, Final

_TRAIN_NAME: Final = "qasper-train-v0.3.json"
_ANSWER_TYPES: Final = frozenset({"extractive", "free_form", "yes_no", "unanswerable"})


def prepare(
    root: Path,
    *,
    seed: int = 20261031,
    max_papers: int = 12,
    max_questions: int = 60,
) -> dict[str, object]:
    """Select paper-local QASPER train cases without using answer labels for sampling.

    Papers are shuffled from a sorted ID list with a fixed seed. Questions then
    rotate in their original QASPER order, one paper per round, until the question
    cap is reached. Every annotation remains an independent human label.
    """

    if not 8 <= max_papers <= 12:
        raise ValueError("max_papers must be between 8 and 12")
    if not 1 <= max_questions <= 60:
        raise ValueError("max_questions must be between 1 and 60")

    path = _train_path(Path(root))
    papers = _load_json_object(path)
    paper_ids = sorted(papers)
    shuffled_paper_ids = list(paper_ids)
    random.Random(seed).shuffle(shuffled_paper_ids)
    selected_paper_ids = shuffled_paper_ids[:max_papers]

    # Build only the ID ledger before touching annotations. Question order is the
    # source order, and no answer type or gold evidence participates in selection.
    ledger: list[dict[str, object]] = []
    question_ids_by_paper: dict[str, list[str]] = {}
    for paper_id in selected_paper_ids:
        paper = _paper_for(papers, paper_id, path)
        questions = _list(paper, "qas", paper_id)
        paper_question_ids: list[str] = []
        for qa in questions:
            question = _object(qa, f"question in paper {paper_id}")
            paper_question_ids.append(_question_id(question, paper_id))
        if len(paper_question_ids) != len(set(paper_question_ids)):
            raise ValueError(f"paper {paper_id} contains duplicate question IDs")
        question_ids_by_paper[paper_id] = paper_question_ids
        ledger.append(
            {
                "paper_id": paper_id,
                "question_ids": paper_question_ids,
                "question_count": len(paper_question_ids),
            }
        )

    selected_qids = _round_robin_questions(
        selected_paper_ids,
        question_ids_by_paper,
        max_questions=max_questions,
    )
    qid_index = {
        (paper_id, _question_id(_object(qa, f"question in paper {paper_id}"), paper_id)): (
            paper_id,
            qa,
        )
        for paper_id in selected_paper_ids
        for qa in _list(_paper_for(papers, paper_id, path), "qas", paper_id)
    }
    cases = [
        _case(paper_id, qid_index[(paper_id, query_id)][1]) for paper_id, query_id in selected_qids
    ]
    corpus = [
        _corpus_paper(paper_id, _paper_for(papers, paper_id, path))
        for paper_id in selected_paper_ids
    ]

    selected_paper_question_count = sum(
        len(question_ids_by_paper[paper_id]) for paper_id in selected_paper_ids
    )
    return {
        "corpus": corpus,
        "cases": cases,
        "sampling": {
            "seed": seed,
            "max_papers": max_papers,
            "max_questions": max_questions,
            "selected_paper_ids": selected_paper_ids,
            "selected_paper_count": len(selected_paper_ids),
            "selected_paper_question_count": selected_paper_question_count,
            "selected_question_count": len(cases),
            "question_ledger": ledger,
            "paper_order": ("sorted official train paper IDs, then random.Random(seed).shuffle"),
            "question_order": (
                "official QASPER question order; one question per selected paper per round"
            ),
            "label_blind_sampling": True,
            "all_questions_retained_for_selected_papers": len(cases)
            == selected_paper_question_count,
            "all_selected_paper_question_ids_in_ledger": True,
        },
        "source": {
            "dataset": "qasper",
            "version": "0.3",
            "split": "train",
            "file": path.name,
            "paper_total": len(papers),
            "question_total": sum(
                len(_list(_paper_for(papers, paper_id, path), "qas", paper_id))
                for paper_id in paper_ids
            ),
            "annotation_origin": "QASPER human annotations",
            "model_generated_annotations": False,
            "dev_test_accessed": False,
            "scope": "exposed_train",
            "scope_note": (
                "QASPER train is a previously exposed measurement scenario; "
                "this sample is not an independent confirmation or holdout."
            ),
        },
    }


def _train_path(root: Path) -> Path:
    candidates = (root / _TRAIN_NAME, root / "extracted" / _TRAIN_NAME)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    searched = ", ".join(str(candidate) for candidate in candidates)
    raise FileNotFoundError(f"QASPER train split not found; searched {searched}")


def _load_json_object(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    if not value:
        raise ValueError(f"{path.name} contains no papers")
    return value


def _round_robin_questions(
    paper_ids: list[str],
    question_ids_by_paper: dict[str, list[str]],
    *,
    max_questions: int,
) -> list[tuple[str, str]]:
    selected: list[tuple[str, str]] = []
    offsets = {paper_id: 0 for paper_id in paper_ids}
    while len(selected) < max_questions:
        progressed = False
        for paper_id in paper_ids:
            offset = offsets[paper_id]
            question_ids = question_ids_by_paper[paper_id]
            if offset >= len(question_ids):
                continue
            selected.append((paper_id, question_ids[offset]))
            offsets[paper_id] = offset + 1
            progressed = True
            if len(selected) == max_questions:
                break
        if not progressed:
            break
    return selected


def _corpus_paper(paper_id: str, paper: dict[str, Any]) -> dict[str, object]:
    title = _string(paper, "title", paper_id)
    abstract = _string(paper, "abstract", paper_id)
    sections: list[dict[str, object]] = []
    for index, raw_section in enumerate(_list(paper, "full_text", paper_id)):
        section = _object(raw_section, f"section {index} in paper {paper_id}")
        heading = section.get("section_name")
        if heading is None:
            heading = ""
        if not isinstance(heading, str):
            raise ValueError(f"paper {paper_id} has an invalid section_name")
        paragraphs = _string_list(
            section.get("paragraphs"),
            owner=f"paper {paper_id} section {index}",
            field="paragraphs",
        )
        sections.append(
            {
                "heading": heading,
                "paragraphs": [paragraph for paragraph in paragraphs if paragraph != ""],
            }
        )

    captions: list[str] = []
    for index, raw_caption in enumerate(_list(paper, "figures_and_tables", paper_id)):
        caption = _object(
            raw_caption,
            f"figure/table {index} in paper {paper_id}",
        )
        text = _string(caption, "caption", f"figure/table {index} in paper {paper_id}")
        if text != "":
            captions.append(text)
    if captions:
        sections.append({"heading": "Figures and tables", "paragraphs": captions})

    return {
        "paper_id": paper_id,
        "title": title,
        "abstract": abstract,
        "sections": sections,
    }


def _case(paper_id: str, qa: Any) -> dict[str, object]:
    question = _object(qa, f"question in paper {paper_id}")
    query_id = _question_id(question, paper_id)
    question_text = _string(question, "question", query_id)
    annotations = [
        _annotation(raw_annotation, query_id)
        for raw_annotation in _list(question, "answers", query_id)
    ]
    if not annotations:
        raise ValueError(f"question {query_id} has no human annotations")
    answerable_count = sum(
        annotation["answer_type"] != "unanswerable" for annotation in annotations
    )
    if answerable_count == len(annotations):
        consensus = "answerable"
    elif answerable_count == 0:
        consensus = "unanswerable"
    else:
        consensus = "mixed"
    return {
        "query_id": query_id,
        "paper_id": paper_id,
        "question": question_text,
        "annotations": annotations,
        "consensus": consensus,
        "scorable": consensus != "mixed",
    }


def _annotation(raw_annotation: Any, query_id: str) -> dict[str, object]:
    wrapper = _object(raw_annotation, f"annotation for question {query_id}")
    answer = _object(wrapper.get("answer"), f"answer for question {query_id}")
    unanswerable = answer.get("unanswerable")
    if not isinstance(unanswerable, bool):
        raise ValueError(f"question {query_id} has an invalid unanswerable flag")

    spans = answer.get("extractive_spans")
    spans = _string_list(
        spans,
        owner=f"answer for question {query_id}",
        field="extractive_spans",
    )
    nonempty_spans = [span for span in spans if span != ""]
    free_form = answer.get("free_form_answer")
    if not isinstance(free_form, str):
        raise ValueError(f"question {query_id} has an invalid free_form_answer")
    yes_no = answer.get("yes_no")
    if yes_no is not None and not isinstance(yes_no, bool):
        raise ValueError(f"question {query_id} has an invalid yes_no value")

    if unanswerable:
        if nonempty_spans or free_form != "" or yes_no is not None:
            raise ValueError(f"unanswerable annotation for {query_id} also has an answer")
        answer_type = "unanswerable"
        answer_text = ""
    else:
        answer_fields = [
            bool(nonempty_spans),
            free_form != "",
            isinstance(yes_no, bool),
        ]
        if sum(answer_fields) != 1:
            raise ValueError(f"question {query_id} annotation has an ambiguous answer type")
        if nonempty_spans:
            answer_type = "extractive"
            answer_text = ", ".join(nonempty_spans)
        elif free_form != "":
            answer_type = "free_form"
            answer_text = free_form
        else:
            answer_type = "yes_no"
            answer_text = "Yes" if yes_no else "No"

    if answer_type not in _ANSWER_TYPES:
        raise ValueError(f"question {query_id} has an unsupported answer type")
    evidence = _string_list(
        answer.get("evidence"),
        owner=f"answer for question {query_id}",
        field="evidence",
    )
    result: dict[str, object] = {
        "answer_type": answer_type,
        "answer": answer_text,
        "evidence": list(evidence),
    }
    if "highlighted_evidence" in answer:
        result["highlighted_evidence"] = list(
            _string_list(
                answer["highlighted_evidence"],
                owner=f"answer for question {query_id}",
                field="highlighted_evidence",
            )
        )
    return result


def _paper_for(
    papers: dict[str, Any],
    paper_id: str,
    path: Path,
) -> dict[str, Any]:
    return _object(papers.get(paper_id), f"paper {paper_id} in {path.name}")


def _question_id(question: dict[str, Any], paper_id: str) -> str:
    value = question.get("question_id")
    if not isinstance(value, str) or not value:
        raise ValueError(f"paper {paper_id} has an invalid question_id")
    return value


def _object(value: Any, owner: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{owner} must be a JSON object")
    return value


def _string(record: dict[str, Any], field: str, owner: str) -> str:
    value = record.get(field)
    if not isinstance(value, str):
        raise ValueError(f"{owner} has an invalid {field}")
    return value


def _list(record: dict[str, Any], field: str, owner: str) -> list[Any]:
    value = record.get(field)
    if not isinstance(value, list):
        raise ValueError(f"{owner} has an invalid {field}")
    return value


def _string_list(value: Any, *, owner: str, field: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{owner} has an invalid {field}")
    return value


__all__ = ["prepare"]
