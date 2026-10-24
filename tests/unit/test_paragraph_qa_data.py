from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from benchmarks.public_rag import paragraph_qa_data


@pytest.fixture(scope="module")
def qasper_root() -> Path:
    root = Path(__file__).resolve().parent / ".paragraph-qa-fixture"
    if root.exists():
        shutil.rmtree(root)
    _write_fixture(root)
    try:
        yield root
    finally:
        shutil.rmtree(root)


def test_prepare_excludes_known_exposure_before_seeded_selection(
    qasper_root: Path,
) -> None:
    exposure = {"paper-00", "paper-01", "paper-02"}
    with patch.object(paragraph_qa_data, "_known_train_exposure", return_value=exposure):
        data = paragraph_qa_data.prepare(
            qasper_root,
            seed=20261101,
            max_papers=3,
            max_questions=100,
        )

    selected = data["sampling"]["selected_paper_ids"]
    assert len(selected) == 3
    assert not set(selected) & exposure
    assert data["sampling"]["known_exposure"]["selected_paper_overlap"] == []
    assert data["source"]["split"] == "train"
    assert data["source"]["sibling_splits_accessed"] is False


def test_prepare_retains_every_annotation_and_evidence_field(
    qasper_root: Path,
) -> None:
    with patch.object(paragraph_qa_data, "_known_train_exposure", return_value=set()):
        data = paragraph_qa_data.prepare(
            qasper_root,
            seed=20261101,
            max_papers=10,
            max_questions=100,
        )

    case = next(item for item in data["cases"] if item["query_id"] == "paper-00-question-1")
    assert [item["answer_type"] for item in case["annotations"]] == [
        "extractive",
        "free_form",
        "yes_no",
        "unanswerable",
    ]
    assert [item["answer"] for item in case["annotations"]] == [
        "span A, span B",
        "Written answer.",
        "No",
        "",
    ]
    assert case["consensus"] == "mixed"
    assert case["scorable"] is False
    assert [item["evidence"] for item in case["annotations"]] == [
        ["Gold only."],
        ["Other gold only."],
        ["Yes/no gold."],
        ["No answer is stated."],
    ]
    assert case["annotations"][0]["highlighted_evidence"] == ["Visible highlight."]
    assert case["annotations"][3]["highlighted_evidence"] == ["No support."]

    corpus_text = json.dumps(data["corpus"], ensure_ascii=False)
    assert "Gold only." not in corpus_text
    assert "Visible paragraph." in corpus_text
    assert "Figure 1: Caption." in corpus_text
    assert '"qas"' not in corpus_text


def test_selection_is_invariant_to_answer_and_evidence_values(
    qasper_root: Path,
) -> None:
    with patch.object(paragraph_qa_data, "_known_train_exposure", return_value=set()):
        baseline = paragraph_qa_data.prepare(
            qasper_root,
            seed=20261101,
            max_papers=4,
            max_questions=6,
        )

    changed_root = qasper_root.parent / ".paragraph-qa-changed-labels"
    if changed_root.exists():
        shutil.rmtree(changed_root)
    shutil.copytree(qasper_root, changed_root)
    try:
        path = changed_root / "qasper-train-v0.3.json"
        papers = json.loads(path.read_text(encoding="utf-8"))
        for paper in papers.values():
            for question in paper["qas"]:
                for wrapper in question["answers"]:
                    answer = wrapper["answer"]
                    if answer["unanswerable"]:
                        answer["evidence"] = ["Changed unanswerable evidence."]
                        answer["highlighted_evidence"] = ["Changed no-answer highlight."]
                    elif answer["extractive_spans"]:
                        answer["extractive_spans"] = ["Changed extractive answer."]
                        answer["evidence"] = ["Changed extractive evidence."]
                    elif answer["free_form_answer"]:
                        answer["free_form_answer"] = "Changed free-form answer."
                        answer["evidence"] = ["Changed free-form evidence."]
                    else:
                        answer["yes_no"] = not answer["yes_no"]
                        answer["evidence"] = ["Changed yes/no evidence."]
        path.write_text(json.dumps(papers, ensure_ascii=False), encoding="utf-8")
        with patch.object(paragraph_qa_data, "_known_train_exposure", return_value=set()):
            changed = paragraph_qa_data.prepare(
                changed_root,
                seed=20261101,
                max_papers=4,
                max_questions=6,
            )
    finally:
        shutil.rmtree(changed_root)

    assert changed["sampling"]["selected_paper_ids"] == baseline["sampling"]["selected_paper_ids"]
    assert (
        changed["sampling"]["selected_question_ids"]
        == baseline["sampling"]["selected_question_ids"]
    )
    assert changed["sampling"]["question_ledger"] == baseline["sampling"]["question_ledger"]


def test_prepare_classifies_unanswerable_only_from_annotation_type(
    qasper_root: Path,
) -> None:
    with patch.object(paragraph_qa_data, "_known_train_exposure", return_value=set()):
        cases = paragraph_qa_data.prepare(
            qasper_root,
            seed=20261101,
            max_papers=10,
            max_questions=100,
        )["cases"]

    empty_answer = next(item for item in cases if item["query_id"] == "paper-00-question-3")
    unanswerable = next(item for item in cases if item["query_id"] == "paper-01-question-1")
    assert empty_answer["annotations"][0]["answer_type"] == "free_form"
    assert empty_answer["annotations"][0]["answer"] == " "
    assert unanswerable["annotations"][0]["answer_type"] == "unanswerable"
    assert unanswerable["annotations"][0]["answer"] == ""


def test_prepare_round_robins_questions_without_adding_unselected_papers(
    qasper_root: Path,
) -> None:
    with patch.object(paragraph_qa_data, "_known_train_exposure", return_value=set()):
        data = paragraph_qa_data.prepare(
            qasper_root,
            seed=20261101,
            max_papers=3,
            max_questions=5,
        )

    selected = data["sampling"]["selected_paper_ids"]
    assert [item["paper_id"] for item in data["cases"]] == [
        selected[0],
        selected[1],
        selected[2],
        selected[0],
        selected[1],
    ]
    assert data["sampling"]["selected_question_count"] == 5
    assert data["sampling"]["selected_paper_question_count"] == 9
    assert data["sampling"]["question_cap_shortfall"] == 0
    assert {item["paper_id"] for item in data["cases"]} == set(selected)


def test_prepare_does_not_fill_cap_with_an_extra_paper(
    qasper_root: Path,
) -> None:
    with patch.object(paragraph_qa_data, "_known_train_exposure", return_value=set()):
        data = paragraph_qa_data.prepare(
            qasper_root,
            seed=20261101,
            max_papers=2,
            max_questions=100,
        )

    assert data["sampling"]["selected_paper_count"] == 2
    assert data["sampling"]["selected_question_count"] == 6
    assert {item["paper_id"] for item in data["cases"]} == set(
        data["sampling"]["selected_paper_ids"]
    )
    assert data["sampling"]["all_questions_retained_for_selected_papers"] is True


def test_prepare_rejects_an_exposure_id_absent_from_train(qasper_root: Path) -> None:
    with patch.object(
        paragraph_qa_data,
        "_known_train_exposure",
        return_value={"paper-00", "not-in-train"},
    ):
        try:
            paragraph_qa_data.prepare(qasper_root)
        except ValueError as error:
            assert "not-in-train" in str(error)
        else:
            raise AssertionError("unknown exposure ID was accepted")


def _write_fixture(root: Path) -> None:
    root.mkdir()
    papers: dict[str, object] = {}
    for index in range(10):
        paper_id = f"paper-{index:02d}"
        papers[paper_id] = {
            "title": f"Title {index}",
            "abstract": f"Abstract {index}.",
            "full_text": [
                {
                    "section_name": "1 Introduction",
                    "paragraphs": ["Visible paragraph.", ""],
                }
            ],
            "figures_and_tables": [{"caption": "Figure 1: Caption.", "file": "f.png"}],
            "qas": [_question(paper_id, number) for number in range(1, 4)],
        }
    papers["paper-00"]["qas"][0]["answers"] = [
        _answer(
            extractive_spans=["span A", "span B"],
            evidence=["Gold only."],
            highlighted_evidence=["Visible highlight."],
        ),
        _answer(
            free_form_answer="Written answer.",
            evidence=["Other gold only."],
            highlighted_evidence=["Another highlight."],
        ),
        _answer(yes_no=False, evidence=["Yes/no gold."], highlighted_evidence=["Yes/no."]),
        _answer(
            unanswerable=True,
            evidence=["No answer is stated."],
            highlighted_evidence=["No support."],
        ),
    ]
    papers["paper-00"]["qas"][2]["answers"] = [
        _answer(
            free_form_answer=" ",
            evidence=[],
            highlighted_evidence=[],
        )
    ]
    papers["paper-01"]["qas"][0]["answers"] = [
        _answer(
            unanswerable=True,
            evidence=[],
            highlighted_evidence=[],
        )
    ]
    (root / "qasper-train-v0.3.json").write_text(
        json.dumps(papers, ensure_ascii=False),
        encoding="utf-8",
    )
    # A TRAIN-only adapter must not read siblings even when they are invalid.
    (root / "qasper-dev-v0.3.json").write_text("not valid JSON", encoding="utf-8")
    (root / "qasper-test-v0.3.json").write_text("not valid JSON", encoding="utf-8")


def _question(paper_id: str, number: int) -> dict[str, object]:
    return {
        "question_id": f"{paper_id}-question-{number}",
        "question": f"Question {number} for {paper_id}?",
        "answers": [
            _answer(
                extractive_spans=["Visible answer."],
                evidence=["Visible paragraph."],
                highlighted_evidence=["Visible evidence."],
            )
        ],
    }


def _answer(
    *,
    unanswerable: bool = False,
    extractive_spans: list[str] | None = None,
    free_form_answer: str = "",
    yes_no: bool | None = None,
    evidence: list[str],
    highlighted_evidence: list[str],
) -> dict[str, object]:
    return {
        "annotation_id": f"annotation-{unanswerable}-{free_form_answer}-{yes_no}",
        "answer": {
            "unanswerable": unanswerable,
            "extractive_spans": extractive_spans or [],
            "free_form_answer": free_form_answer,
            "yes_no": yes_no,
            "evidence": evidence,
            "highlighted_evidence": highlighted_evidence,
        },
    }
