from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import patch

from benchmarks.public_rag import entity_delivery_data

_FIXTURE = Path(__file__).resolve().parent / ".entity-delivery-fixture"


def setup_module() -> None:
    if _FIXTURE.exists():
        shutil.rmtree(_FIXTURE)
    _FIXTURE.mkdir()
    papers = {}
    for index in range(18):
        paper_id = f"paper-{index:02d}"
        paragraphs = [
            f"Abstract sentence for {paper_id}.",
            f"Training data paragraph for {paper_id}.",
            f"Evaluation corpus paragraph for {paper_id}.",
            f"Unrelated background for {paper_id}.",
        ]
        papers[paper_id] = {
            "title": f"Title {paper_id}",
            "abstract": paragraphs[0],
            "full_text": [{"section_name": "Experiments", "paragraphs": paragraphs[1:]}],
            "figures_and_tables": [],
            "qas": [
                {
                    "question_id": f"{paper_id}-rejected",
                    "question": "How many samples are in the dataset used for training?",
                    "answers": [_answer(paragraphs[1], "large sentence answer")],
                },
                {
                    "question_id": f"{paper_id}-selected",
                    "question": "Which datasets are used for training and evaluation?",
                    "answers": [
                        _answer(paragraphs[1], "one answer"),
                        _answer(paragraphs[2], "another answer"),
                    ],
                },
            ],
        }
    (_FIXTURE / "qasper-train-v0.3.json").write_text(
        json.dumps(papers),
        encoding="utf-8",
    )
    (_FIXTURE / "qasper-dev-v0.3.json").write_text("invalid", encoding="utf-8")
    (_FIXTURE / "qasper-test-v0.3.json").write_text("invalid", encoding="utf-8")


def teardown_module() -> None:
    if _FIXTURE.exists():
        shutil.rmtree(_FIXTURE)


def test_selection_is_question_only_ordered_and_excludes_frozen_papers() -> None:
    excluded = {f"paper-{index:02d}" for index in range(4)}
    with patch.object(entity_delivery_data, "_exclusion_ids", return_value=excluded):
        baseline = entity_delivery_data.prepare(_FIXTURE)

    papers, _, _ = _fixture_rows()
    for paper in papers.values():
        for question in paper["qas"]:
            question["answers"][0]["answer"]["evidence"] = ["Changed human reference."]
    (_FIXTURE / "qasper-train-v0.3.json").write_text(
        json.dumps(papers),
        encoding="utf-8",
    )
    with patch.object(entity_delivery_data, "_exclusion_ids", return_value=excluded):
        changed = entity_delivery_data.prepare(_FIXTURE)

    selected_ids = [case["query_id"] for case in baseline["cases"]]
    assert selected_ids == [case["query_id"] for case in changed["cases"]]
    assert selected_ids == [f"paper-{index:02d}-selected" for index in range(4, 18)]
    assert {case["paper_id"] for case in baseline["cases"]}.isdisjoint(excluded)
    assert len(selected_ids) == len(set(selected_ids)) == 14
    assert [
        (item["query_id"], item["paper_id"], item["question"]) for item in baseline["ledger"]
    ] == [(item["query_id"], item["paper_id"], item["question"]) for item in changed["ledger"]]
    assert baseline["selection"]["proposals"] == [
        {
            "query_id": item["query_id"],
            "paper_id": item["paper_id"],
            "question": item["question"],
        }
        for item in baseline["ledger"]
    ]
    assert baseline["selection"]["dev_test_accessed"] is False
    assert baseline["selection"]["gold_labels_or_old_scores_used"] is False


def test_ambiguous_evidence_is_retained_with_pending_notes() -> None:
    excluded = {f"paper-{index:02d}" for index in range(4)}
    papers, _, _ = _fixture_rows()
    for paper in papers.values():
        for question in paper["qas"]:
            question["answers"][0]["answer"]["evidence"] = ["Not an original paragraph."]
    (_FIXTURE / "qasper-train-v0.3.json").write_text(
        json.dumps(papers),
        encoding="utf-8",
    )
    with patch.object(entity_delivery_data, "_exclusion_ids", return_value=excluded):
        dataset = entity_delivery_data.prepare(_FIXTURE)

    case = dataset["cases"][0]
    assert len(case["annotations"]) == 2
    assert case["review_status"] == "PENDING"
    assert "HUMAN_EVIDENCE_UNALIGNED:annotation=0:evidence=0" in case["notes"]
    assert case["answer_references"] == [
        "Not an original paragraph.",
        "Evaluation corpus paragraph for paper-04.",
    ]
    assert len(case["human_evidence_source_ids"]) == 1
    assert "NO_ALIGNED_HUMAN_EVIDENCE" not in case["notes"]
    assert any("Evaluation corpus paragraph" in record["text"] for record in case["records"])
    assert all(
        set(record) == {"source_id", "paper_id", "source_revision", "source_ranges", "text"}
        for record in case["records"]
    )


def test_source_slice_validator_checks_exact_corpus_membership() -> None:
    excluded = {f"paper-{index:02d}" for index in range(4)}
    with patch.object(entity_delivery_data, "_exclusion_ids", return_value=excluded):
        dataset = entity_delivery_data.prepare(_FIXTURE)
    summary = entity_delivery_data.validate_source_slices(dataset, dataset["corpus"])
    assert summary["cases_checked"] == 14
    assert summary["records_checked"] == sum(len(case["records"]) for case in dataset["cases"])

    broken = json.loads(json.dumps(dataset))
    broken["cases"][0]["records"][0]["text"] += " changed"
    try:
        entity_delivery_data.validate_source_slices(broken, dataset["corpus"])
    except ValueError as error:
        assert "does not match its source" in str(error)
    else:
        raise AssertionError("validator accepted altered source text")


def _fixture_rows():
    papers = json.loads((_FIXTURE / "qasper-train-v0.3.json").read_text(encoding="utf-8"))
    return papers, None, None


def _answer(evidence: str, text: str) -> dict[str, object]:
    return {
        "answer": {
            "unanswerable": False,
            "extractive_spans": [text],
            "free_form_answer": "",
            "yes_no": None,
            "evidence": [evidence],
        }
    }
