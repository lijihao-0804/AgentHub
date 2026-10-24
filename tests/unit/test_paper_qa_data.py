from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from benchmarks.public_rag.paper_qa_data import prepare

_PAPER_IDS = [f"paper-{index:02d}" for index in range(1, 9)]


@pytest.fixture
def qasper_root(tmp_path: Path) -> Path:
    papers: dict[str, object] = {}
    for index, paper_id in enumerate(_PAPER_IDS, start=1):
        paper = _paper(index)
        if index == 1:
            paper["qas"][0]["answers"] = [
                _answer(
                    extractive_spans=["First span", "Second span"],
                    evidence=["GOLD-ONLY EVIDENCE"],
                    highlighted_evidence=["Supporting sentence."],
                ),
                _answer(
                    unanswerable=True,
                    evidence=[],
                ),
            ]
        elif index == 2:
            paper["qas"][0]["answers"] = [
                _answer(unanswerable=True, evidence=["No answer is stated."])
            ]
        elif index == 3:
            paper["qas"][0]["answers"] = [
                _answer(
                    free_form_answer="A written human answer.",
                    evidence=["Free-form evidence."],
                )
            ]
        elif index == 4:
            paper["qas"][0]["answers"] = [_answer(yes_no=False, evidence=["Yes/no evidence."])]
        papers[paper_id] = paper

    root = tmp_path / "qasper"
    root.mkdir()
    (root / "qasper-train-v0.3.json").write_text(
        json.dumps(papers, ensure_ascii=False),
        encoding="utf-8",
    )
    # A train-only loader must succeed even when sibling splits are unreadable.
    (root / "qasper-dev-v0.3.json").write_text("not json", encoding="utf-8")
    (root / "qasper-test-v0.3.json").write_text("not json", encoding="utf-8")
    return root


def test_prepare_loads_full_paper_material_and_keeps_gold_out_of_corpus(
    qasper_root: Path,
) -> None:
    data = prepare(qasper_root, max_papers=8, max_questions=8)
    paper = data["corpus"][0]

    assert set(paper) == {"paper_id", "title", "abstract", "sections"}
    assert paper["sections"][0] == {
        "heading": "1 Introduction",
        "paragraphs": ["Visible passage."],
    }
    assert paper["sections"][1] == {
        "heading": "",
        "paragraphs": ["Unnamed section passage."],
    }
    assert paper["sections"][2] == {
        "heading": "Figures and tables",
        "paragraphs": ["Figure 1: Exact original caption."],
    }
    serialized_corpus = json.dumps(data["corpus"], ensure_ascii=False)
    assert "GOLD-ONLY EVIDENCE" not in serialized_corpus
    assert '"qas"' not in serialized_corpus


def test_prepare_preserves_each_annotation_and_marks_mixed_unscorable(
    qasper_root: Path,
) -> None:
    cases = prepare(qasper_root, max_papers=8, max_questions=8)["cases"]
    mixed = next(case for case in cases if case["query_id"] == "paper-01-question-1")

    assert [annotation["answer_type"] for annotation in mixed["annotations"]] == [
        "extractive",
        "unanswerable",
    ]
    assert mixed["annotations"][0] == {
        "answer_type": "extractive",
        "answer": "First span, Second span",
        "evidence": ["GOLD-ONLY EVIDENCE"],
        "highlighted_evidence": ["Supporting sentence."],
    }
    assert mixed["annotations"][1] == {
        "answer_type": "unanswerable",
        "answer": "",
        "evidence": [],
    }
    assert mixed["consensus"] == "mixed"
    assert mixed["scorable"] is False


def test_prepare_represents_all_answer_types_without_fake_unanswerable_answers(
    qasper_root: Path,
) -> None:
    cases = prepare(qasper_root, max_papers=8, max_questions=8)["cases"]
    annotations = [
        case["annotations"][0]
        for case in cases
        if case["paper_id"] in {"paper-02", "paper-03", "paper-04", "paper-05"}
    ]
    by_type = {annotation["answer_type"]: annotation for annotation in annotations}

    assert by_type["free_form"]["answer"] == "A written human answer."
    assert by_type["yes_no"]["answer"] == "No"
    assert by_type["extractive"]["answer"] == "Extractive answer."
    assert by_type["unanswerable"]["answer"] == ""
    assert by_type["unanswerable"]["evidence"] == ["No answer is stated."]
    unanswerable_case = next(case for case in cases if case["query_id"] == "paper-02-question-1")
    assert unanswerable_case["consensus"] == "unanswerable"
    assert unanswerable_case["scorable"] is True


def test_prepare_reads_only_the_official_train_split(qasper_root: Path) -> None:
    data = prepare(qasper_root, max_papers=8, max_questions=60)

    assert data["source"]["split"] == "train"
    assert data["source"]["dev_test_accessed"] is False
    assert data["sampling"]["selected_paper_count"] == 8
    assert {case["paper_id"] for case in data["cases"]} == set(_PAPER_IDS)


def test_prepare_sampling_is_seeded_and_round_robins_source_questions(
    qasper_root: Path,
) -> None:
    first = prepare(qasper_root, seed=17, max_papers=8, max_questions=11)
    second = prepare(qasper_root, seed=17, max_papers=8, max_questions=11)

    assert first == second
    expected_papers = list(_PAPER_IDS)
    random.Random(17).shuffle(expected_papers)
    assert first["sampling"]["selected_paper_ids"] == expected_papers

    expected_cases = []
    for round_number in range(2):
        for paper_id in expected_papers:
            expected_cases.append(f"{paper_id}-question-{round_number + 1}")
    assert [case["query_id"] for case in first["cases"]] == expected_cases[:11]
    assert first["sampling"]["selected_paper_question_count"] == 16
    assert sum(len(item["question_ids"]) for item in first["sampling"]["question_ledger"]) == 16


def test_prepare_round_robin_reaches_the_requested_cap(
    qasper_root: Path,
) -> None:
    data = prepare(qasper_root, seed=23, max_papers=8, max_questions=15)

    expected_papers = list(_PAPER_IDS)
    random.Random(23).shuffle(expected_papers)
    expected_qids = [
        f"{paper_id}-question-{question_number}"
        for question_number in (1,)
        for paper_id in expected_papers
    ] + [f"{paper_id}-question-2" for paper_id in expected_papers[:7]]
    assert [case["query_id"] for case in data["cases"]] == expected_qids
    assert data["sampling"]["selected_question_count"] == 15


def _paper(index: int) -> dict[str, object]:
    paper_id = _PAPER_IDS[index - 1]
    return {
        "title": f"Title {index}",
        "abstract": f"Abstract {index}.",
        "full_text": [
            {
                "section_name": "1 Introduction",
                "paragraphs": ["Visible passage.", ""],
            },
            {
                "section_name": None,
                "paragraphs": ["Unnamed section passage."],
            },
        ],
        "figures_and_tables": [
            {
                "caption": "Figure 1: Exact original caption.",
                "file": "figure-1.png",
            }
        ],
        "qas": [
            {
                "question_id": f"{paper_id}-question-1",
                "question": f"Question one for {paper_id}?",
                "answers": [
                    _answer(
                        extractive_spans=["Extractive answer."],
                        evidence=["Source paragraph."],
                    )
                ],
            },
            {
                "question_id": f"{paper_id}-question-2",
                "question": f"Question two for {paper_id}?",
                "answers": [
                    _answer(
                        free_form_answer="Second annotation.",
                        evidence=["Second source paragraph."],
                    )
                ],
            },
        ],
    }


def _answer(
    *,
    unanswerable: bool = False,
    extractive_spans: list[str] | None = None,
    free_form_answer: str = "",
    yes_no: bool | None = None,
    evidence: list[str],
    highlighted_evidence: list[str] | None = None,
) -> dict[str, object]:
    answer: dict[str, object] = {
        "unanswerable": unanswerable,
        "extractive_spans": extractive_spans or [],
        "free_form_answer": free_form_answer,
        "yes_no": yes_no,
        "evidence": evidence,
    }
    if highlighted_evidence is not None:
        answer["highlighted_evidence"] = highlighted_evidence
    return {"annotation_id": "human-annotation", "answer": answer}
