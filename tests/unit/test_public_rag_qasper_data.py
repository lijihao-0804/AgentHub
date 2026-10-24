from __future__ import annotations

import json
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from benchmarks.public_rag.qasper_data import load_qasper

_SCRATCH = Path(__file__).resolve().parents[2] / ".scratch"


@pytest.fixture
def qasper_root() -> Iterator[Path]:
    _SCRATCH.mkdir(exist_ok=True)
    root = _SCRATCH / f"qasper-fixture-{uuid.uuid4().hex}"
    root.mkdir()
    paper = {
        "title": "A Paper",
        "abstract": "Abstract text.",
        "full_text": [
            {
                "section_name": "1 Introduction",
                "paragraphs": ["Repeated paragraph.", "First span.", "Second span."],
            },
            {
                "section_name": "2 Method",
                "paragraphs": ["Repeated paragraph.", "Unique method detail."],
            },
            {
                "section_name": None,
                "paragraphs": ["Unnamed section text."],
            },
        ],
        "figures_and_tables": [{"caption": "Figure 1: Overview.", "file": "x.png"}],
        "qas": [
            {
                "question_id": "q-multiple",
                "question": "What are the alternatives?",
                "answers": [
                    {
                        "answer": {
                            "unanswerable": False,
                            "extractive_spans": ["one"],
                            "free_form_answer": "",
                            "yes_no": None,
                            "evidence": ["Repeated paragraph."],
                        }
                    },
                    {
                        "answer": {
                            "unanswerable": False,
                            "extractive_spans": [],
                            "free_form_answer": "two",
                            "yes_no": None,
                            "evidence": ["First span. Second span."],
                        }
                    },
                    {
                        "answer": {
                            "unanswerable": False,
                            "extractive_spans": [],
                            "free_form_answer": "",
                            "yes_no": False,
                            "evidence": ["Unique method detail.", "GOLD ONLY UNALIGNED"],
                        }
                    },
                ],
            },
            {
                "question_id": "q-none",
                "question": "Is it answered?",
                "answers": [
                    {
                        "answer": {
                            "unanswerable": True,
                            "extractive_spans": [],
                            "free_form_answer": "",
                            "yes_no": None,
                            "evidence": [],
                        }
                    }
                ],
            },
        ],
    }
    (root / "qasper-train-v0.3.json").write_text(
        json.dumps({"paper-1": paper}), encoding="utf-8"
    )
    yield root
    shutil.rmtree(root)


def test_load_qasper_preserves_order_sections_and_annotation_alternatives(
    qasper_root: Path,
) -> None:
    data = load_qasper(qasper_root, "train")

    assert [
        (item["document_id"], item["section"], item["text"], item["ordinal"])
        for item in data["corpus"]
    ] == [
        ("paper-1:abstract", "Abstract", "Abstract text.", 0),
        ("paper-1:full_text:1", "1 Introduction", "Repeated paragraph.", 1),
        ("paper-1:full_text:2", "1 Introduction", "First span.", 2),
        ("paper-1:full_text:3", "1 Introduction", "Second span.", 3),
        ("paper-1:full_text:4", "2 Method", "Repeated paragraph.", 4),
        ("paper-1:full_text:5", "2 Method", "Unique method detail.", 5),
        ("paper-1:full_text:6", "", "Unnamed section text.", 6),
        ("paper-1:caption:7", "Captions", "Figure 1: Overview.", 7),
    ]
    query = data["queries"][0]
    assert query["answers"] == ["one", "two", "No"]
    assert query["evidence_alignment_complete"] is False
    assert query["evidence_sets"] == [
        [],
        ["paper-1:full_text:2", "paper-1:full_text:3"],
        ["paper-1:full_text:5"],
    ]
    assert query["unaligned_evidence"] == [
        "Repeated paragraph.",
        "GOLD ONLY UNALIGNED",
    ]
    assert "GOLD ONLY UNALIGNED" not in {
        item["text"] for item in data["corpus"]
    }
    assert query["query"] == "What are the alternatives?"


def test_load_qasper_marks_unanswerable_questions(qasper_root: Path) -> None:
    data = load_qasper(qasper_root, "train")

    assert data["queries"][1] == {
        "query_id": "q-none",
        "paper_id": "paper-1",
        "query": "Is it answered?",
        "answerable": False,
        "evidence_alignment_complete": True,
        "evidence_sets": [[]],
        "answers": ["Unanswerable"],
        "unaligned_evidence": [],
    }


def test_load_qasper_uses_only_the_requested_split(qasper_root: Path) -> None:
    (qasper_root / "qasper-dev-v0.3.json").write_text("not json", encoding="utf-8")

    assert len(load_qasper(qasper_root, "train")["queries"]) == 2


def test_float_marker_maps_only_an_existing_exact_caption():
    from benchmarks.public_rag.qasper_data import _align

    aligned, missing = _align(
        ["FLOAT SELECTED: Table 1: Results.", "FLOAT SELECTED: Table 2: Missing."],
        ["Table 1: Results."],
        [(0, 17, "caption-1")],
    )
    assert aligned == ["caption-1"]
    assert missing == ["FLOAT SELECTED: Table 2: Missing."]
