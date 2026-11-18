"""Validate the source bindings, gold annotations, historical exposure and evaluator."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

from .dataset import (
    HERE,
    PUBLIC,
    ROOT,
    SOURCES,
    canonical_digest,
    digest,
    load_cases,
    load_documents,
)
from .scoring import evidence_covers, norm, score_case

HISTORY = ROOT / "benchmarks/public_rag/results"
MUSIQUE_ID = re.compile(rb"\b[2-4]hop\d*__\d+(?:_\d+)+\b")
PAPER_ID = re.compile(rb"(?<![\w.])\d{4}\.\d{4,5}(?![\w.])")
QUESTION_ID = re.compile(rb"\b[a-f0-9]{40}\b")
FROZEN_FILES = ("cases.json", "dataset.py", "scoring.py", "verify.py", "rubric.md")


def artifact_bindings() -> dict[str, str]:
    """Handle text checkouts with Windows or Unix newlines identically."""
    return {
        name: digest((HERE / name).read_bytes().replace(b"\r\n", b"\n")) for name in FROZEN_FILES
    }


def _json_lines(path: Path):
    with path.open(encoding="utf8") as src:
        for line in src:
            yield json.loads(line)


def audit_history() -> dict:
    """Audit old receipts by ID. Compare DEV/TEST article titles and text hashes only."""
    cases = load_cases()
    docs = load_documents()
    old_musique, old_papers, old_questions = set(), set(), set()
    files = {}
    for path in sorted(HISTORY.rglob("*")):
        if path.suffix not in {".json", ".jsonl"} or not path.is_file():
            continue
        data = path.read_bytes()
        files[path.relative_to(HISTORY).as_posix()] = digest(data)
        old_musique.update(x.decode() for x in MUSIQUE_ID.findall(data))
        old_papers.update(x.decode() for x in PAPER_ID.findall(data))
        old_questions.update(x.decode() for x in QUESTION_ID.findall(data))
    qasper = json.loads(SOURCES["qasper"][0].read_bytes())
    old_papers.intersection_update(qasper)
    old_papers.update(
        pid
        for pid, doc in qasper.items()
        if any(q["question_id"] in old_questions for q in doc["qas"])
    )
    upstream = {norm(q["question"]) for doc in qasper.values() for q in doc["qas"]}
    old_titles = set()
    old_texts = set()
    music_dir = PUBLIC / "musique/extracted/data"
    for variant, split in (("ans", "dev"), ("ans", "test"), ("ans", "train"), ("full", "train")):
        for row in _json_lines(music_dir / f"musique_{variant}_v1.0_{split}.jsonl"):
            if split == "train" and variant == "ans":
                upstream.add(norm(row["question"]))
                upstream.update(norm(q["question"]) for q in row.get("question_decomposition", []))
            if split != "train" or row["id"] in old_musique:
                old_titles.update(p["title"].casefold() for p in row["paragraphs"])
                old_texts.update(digest(p["paragraph_text"].encode()) for p in row["paragraphs"])
    findings = {
        "old_qasper_paper_overlaps": [],
        "old_musique_record_overlaps": [],
        "old_musique_title_overlaps": [],
        "old_musique_paragraph_overlaps": [],
        "upstream_question_text_overlaps": [],
    }
    for case in cases:
        loc = case["locator"]
        doc = docs[case["doc_id"]]
        if norm(case["question"]) in upstream:
            findings["upstream_question_text_overlaps"].append(case["id"])
        if case["source_kind"] == "qasper":
            if loc["paper"] in old_papers:
                findings["old_qasper_paper_overlaps"].append(case["id"])
        else:
            if loc["record"] in old_musique:
                findings["old_musique_record_overlaps"].append(case["id"])
            if doc["title"].casefold() in old_titles:
                findings["old_musique_title_overlaps"].append(case["id"])
            body = doc["text"].removeprefix(doc["title"] + "\n\n").removesuffix("\n")
            if digest(body.encode()) in old_texts:
                findings["old_musique_paragraph_overlaps"].append(case["id"])
    return {
        "old_result_files": len(files),
        "old_result_tree_sha256": canonical_digest(files),
        "old_musique_ids": len(old_musique),
        "old_qasper_train_papers": len(old_papers),
        "excluded_musique_titles": len(old_titles),
        "upstream_train_question_texts": len(upstream),
        **findings,
    }


def verify() -> dict:
    cases = load_cases()
    docs = load_documents()
    assert len(cases) == len(docs) == len({c["doc_id"] for c in cases}) == 100
    assert len({d["title"].casefold() for d in docs.values()}) == 100
    assert (
        len({d["sha256"] for d in docs.values()})
        == len({norm(c["question"]) for c in cases})
        == 100
    )
    assert [c["id"] for c in cases] == [f"R{n:03d}" for n in range(1, 101)]
    counts = Counter((c["source_kind"], c["answerable"]) for c in cases)
    assert counts == {
        ("qasper", True): 20,
        ("qasper", False): 20,
        ("musique", True): 30,
        ("musique", False): 30,
    }
    for case in cases:
        doc = docs[case["doc_id"]]
        citation = {"id": 1, "doc_id": doc["id"], "text": doc["text"]}
        citation_list = [citation]
        assert case["question"].endswith("?") and len(case["target"]) > 2
        assert bool(case["facets"]) == case["answerable"]
        for facet in case["facets"]:
            assert facet["aliases"]
            for start, end in facet["support"]:
                assert 0 <= start < end <= len(doc["text"])
        assert evidence_covers(case, citation_list) == case["answerable"]
        refusal = score_case(case, state="refusal", answer="Insufficient evidence.", citations=[])
        assert refusal["joint_correct"] == (not case["answerable"])
        assert refusal["wrong_refusal"] == case["answerable"]
        assert not refusal["needs_review"]
        for state in ("invalid", "error"):
            s = score_case(case, state=state, answer="Insufficient evidence.", citations=[])
            assert not s["valid"] and not s["joint_correct"]
        if case["answerable"]:
            gold = case["target"] + " [1]"
            correct = score_case(case, state="answer", answer=gold, citations=citation_list)
            assert correct["joint_correct"], (case["id"], case["target"], correct)
            assert correct["needs_review"]
            for bad_ref in (
                {**citation, "doc_id": "unrelated"},
                {**citation, "text": "Unrelated words, an invented source."},
                {**citation, "text": doc["title"]},
            ):
                wrong = score_case(case, state="answer", answer=gold, citations=[bad_ref])
                assert not wrong["citation_correct"] and not wrong["joint_correct"]
            for bad_answer in ("Nothing about the requested fact [1]", "Different [2]"):
                wrong = score_case(case, state="answer", answer=bad_answer, citations=citation_list)
                assert not wrong["joint_correct"]
            extra = score_case(
                case,
                state="answer",
                answer=gold + "[2]",
                citations=[citation, {**citation, "id": 2, "text": doc["title"]}],
            )
            assert not extra["joint_correct"]
            for fabricated in (
                gold + ". It was tested on Neptune [1].",
                gold + ". They went to Mars.",
            ):
                wrong = score_case(case, state="answer", answer=fabricated, citations=citation_list)
                assert not wrong["joint_correct"] and wrong["needs_review"]
        else:
            wrong = score_case(case, state="answer", answer="A guess [1].", citations=citation_list)
            assert wrong["unsafe_answer"] and not wrong["joint_correct"]
            disguised = score_case(
                case, state="refusal", answer="The value was ninety.", citations=[]
            )
            assert not disguised["joint_correct"] and disguised["needs_review"]
    pair = next(c for c in cases if c["family"] == "paired_values")
    doc = docs[pair["doc_id"]]
    passages = []
    for i, facet in enumerate(pair["facets"], 1):
        a, b = facet["support"][0]
        passages.append({"id": i, "doc_id": doc["id"], "text": doc["text"][a:b]})
    pair_answer = "Emotion gained 5 points [1]. Sentiment gained 3.2 points [2]."
    assert score_case(pair, state="answer", answer=pair_answer, citations=passages)["joint_correct"]
    passages[0]["text"], passages[1]["text"] = passages[1]["text"], passages[0]["text"]
    assert not score_case(pair, state="answer", answer=pair_answer, citations=passages)[
        "joint_correct"
    ]
    return {
        "cases": len(cases),
        "distinct_documents": len(docs),
        "max_document_bytes": max(len(d["text"].encode()) for d in docs.values()),
        "cases_sha256": canonical_digest(cases),
        "documents_sha256": canonical_digest({k: d["sha256"] for k, d in docs.items()}),
        "source_sha256": {kind: pin for kind, (_, pin) in SOURCES.items()},
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true")
    args = parser.parse_args()
    report = verify()
    freeze_path = HERE / "source_freeze.json"
    if freeze_path.exists():
        freeze = json.loads(freeze_path.read_bytes())
        if report != freeze["dataset"] or artifact_bindings() != freeze["artifacts_lf_sha256"]:
            raise SystemExit("A frozen input or evaluator changed")
    if args.history:
        report["exposure"] = audit_history()
    print(json.dumps(report, indent=2))
    if any(v for k, v in report.get("exposure", {}).items() if k.endswith("_overlaps")):
        raise SystemExit("historical exposure overlap")
