from __future__ import annotations

import json
from pathlib import Path

from benchmarks.public_rag.musique_data import load_musique


def test_load_musique_keeps_distractors_and_builds_complete_multihop_evidence(
    musique_root: Path,
) -> None:
    data = load_musique(musique_root, "train")
    query = data["queries"][0]
    documents = data["documents"][:3]

    assert set(data) == {"documents", "queries"}
    assert query == {
        "query_id": "2hop__alpha",
        "case_id": "2hop__alpha",
        "query": "Which institute owns the newspaper?",
        "answerable": True,
        "unlabelled": False,
        "evidence_sets": [["2hop__alpha::paragraph::0", "2hop__alpha::paragraph::2"]],
        "answers": ["Houston Baptist University", "HBU"],
        "hops": 2,
    }
    assert documents == [
        {
            "document_id": "2hop__alpha::paragraph::0",
            "case_id": "2hop__alpha",
            "title": "The Collegian",
            "text": "The Collegian is the student newspaper.",
            "ordinal": 0,
        },
        {
            "document_id": "2hop__alpha::paragraph::1",
            "case_id": "2hop__alpha",
            "title": "Unrelated newspaper",
            "text": "A distractor about another newspaper.",
            "ordinal": 1,
        },
        {
            "document_id": "2hop__alpha::paragraph::2",
            "case_id": "2hop__alpha",
            "title": "University",
            "text": "The university was founded in 1960.",
            "ordinal": 2,
        },
    ]
    assert "is_supporting" not in documents[0]
    assert "decomposition-secret" not in query["query"]
    assert "answer-secret" not in query["query"]
    assert all(
        "answer-secret" not in document["title"] + document["text"] for document in documents
    )


def test_load_musique_preserves_unanswerable_label(musique_root: Path) -> None:
    query = load_musique(musique_root, "train")["queries"][1]

    assert query["answerable"] is False
    assert query["unlabelled"] is False
    assert query["answers"] == ["not present in the supplied paragraphs"]
    assert query["evidence_sets"] == []
    assert query["hops"] == 2


def test_load_musique_keeps_unlabelled_test_case_without_fake_labels(
    musique_root: Path,
) -> None:
    data = load_musique(musique_root, "test")
    query = data["queries"][0]

    assert query == {
        "query_id": "2hop__unlabelled",
        "case_id": "2hop__unlabelled",
        "query": "Unlabelled test question?",
        "answerable": None,
        "unlabelled": True,
        "evidence_sets": [],
        "answers": [],
        "hops": 2,
    }
    assert "score" not in query
    assert len(data["documents"]) == 1


def test_load_musique_full_keeps_paired_records_and_traceable_source_ids(
    musique_root: Path,
) -> None:
    _write_jsonl(
        musique_root / "musique_full_v1.0_train.jsonl",
        [
            {
                "id": "2hop__paired",
                "paragraphs": [_paragraph(0, "Answerable", "Support.", supporting=True)],
                "question": "Paired question?",
                "question_decomposition": [{"question": "one"}, {"question": "two"}],
                "answer": "yes",
                "answer_aliases": [],
                "answerable": True,
            },
            {
                "id": "2hop__paired",
                "paragraphs": [_paragraph(0, "Unanswerable", "No support.")],
                "question": "Paired question?",
                "question_decomposition": [{"question": "one"}, {"question": "two"}],
                "answer": "no",
                "answer_aliases": [],
                "answerable": False,
            },
        ],
    )

    data = load_musique(musique_root, "train", variant="full")

    assert [query["query_id"] for query in data["queries"]] == [
        "2hop__paired",
        "2hop__paired",
    ]
    assert [query["case_id"] for query in data["queries"]] == [
        "2hop__paired",
        "2hop__paired::2",
    ]
    assert [document["document_id"] for document in data["documents"]] == [
        "2hop__paired::paragraph::0",
        "2hop__paired::2::paragraph::0",
    ]
    assert data["queries"][0]["evidence_sets"] == [["2hop__paired::paragraph::0"]]
    assert data["queries"][1]["evidence_sets"] == []


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def _paragraph(
    idx: int,
    title: str,
    text: str,
    *,
    supporting: bool = False,
) -> dict[str, object]:
    return {
        "idx": idx,
        "title": title,
        "paragraph_text": text,
        "is_supporting": supporting,
    }


def musique_root(tmp_path: Path) -> Path:
    root = tmp_path / "musique"
    root.mkdir()
    _write_jsonl(
        root / "musique_ans_v1.0_train.jsonl",
        [
            {
                "id": "2hop__alpha",
                "paragraphs": [
                    _paragraph(
                        0,
                        "The Collegian",
                        "The Collegian is the student newspaper.",
                        supporting=True,
                    ),
                    _paragraph(1, "Unrelated newspaper", "A distractor about another newspaper."),
                    _paragraph(
                        2,
                        "University",
                        "The university was founded in 1960.",
                        supporting=True,
                    ),
                ],
                "question": "Which institute owns the newspaper?",
                "question_decomposition": [
                    {"question": "decomposition-secret", "answer": "answer-secret"},
                    {"question": "second step", "answer": "second-secret"},
                ],
                "answer": "Houston Baptist University",
                "answer_aliases": ["HBU"],
                "answerable": True,
            },
            {
                "id": "2hop__missing",
                "paragraphs": [
                    _paragraph(0, "Distractor", "This paragraph does not answer the question.")
                ],
                "question": "Unanswerable question?",
                "question_decomposition": [
                    {"question": "first step"},
                    {"question": "second step"},
                ],
                "answer": "not present in the supplied paragraphs",
                "answer_aliases": [],
                "answerable": False,
            },
        ],
    )
    _write_jsonl(
        root / "musique_ans_v1.0_test.jsonl",
        [
            {
                "id": "2hop__unlabelled",
                "paragraphs": [_paragraph(0, "Test title", "Test candidate text.")],
                "question": "Unlabelled test question?",
            }
        ],
    )
    (root / "musique_ans_v1.0_dev.jsonl").write_text("{invalid sibling split\n", encoding="utf-8")
    return root


musique_root = __import__("pytest").fixture(musique_root)
