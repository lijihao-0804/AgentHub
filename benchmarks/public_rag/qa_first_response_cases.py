"""Freeze a QASPER TRAIN development sample and recover its official source passages."""

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PUBLIC = ROOT / "benchmarks/public_rag/results/published"
OUT = PUBLIC / "qa-first-response-20261114/live"
OLD = PUBLIC / "article-recall-20261114/live"
TRAIN = ROOT / "data/external/rag-public-20261021/qasper/extracted/qasper-train-v0.3.json"


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def read(path):
    return json.loads(path.read_bytes())


def paper_text(paper):
    body = [s for sec in paper["full_text"] for s in [sec["section_name"], *sec["paragraphs"]] if s]
    return "\n\n".join([paper["title"], paper["abstract"], *body])


def project(protocol=None):
    definition = read(protocol or OUT / "protocol.json")
    if hashlib.sha256(TRAIN.read_bytes()).hexdigest() != definition["train_sha256"]:
        raise ValueError("QASPER TRAIN differs from the frozen source")
    raw = read(TRAIN)
    cases = definition["cases"]
    docs = {
        c["gold_documents"][0]: paper_text(raw[c["gold_documents"][0]])
        for c in cases
        if c["scope"] != "full"
    }
    # The historical VG and negative-control retrieval universe had seven papers.
    for c in read(OLD / "protocol-public.json")["cases"]:
        if c["scope"] == "qasper":
            key = c["gold_documents"][0]
            docs[key] = paper_text(raw[key])
    for case in cases:
        if case["scope"] == "full":
            continue
        paper = raw[case["gold_documents"][0]]
        question = next(item for item in paper["qas"] if item["question_id"] == case["id"])
        official = question["answers"][0]["answer"]
        if not (
            case["query"] == f"In the paper “{paper['title']}”, {question['question']}"
            and case["answers"] == official["extractive_spans"]
            and case["evidence"] == official["evidence"]
            and case["negative"] == official["unanswerable"]
        ):
            raise ValueError(f"Frozen case differs from official QASPER: {case['id']}")
    return cases, docs


def select(raw, previous_papers, previous_questions, salt, positives, negatives):
    choices = []
    for key, paper in raw.items():
        if key in previous_papers:
            continue
        body = paper_text(paper)
        if len(body) > 30000:
            continue
        for qa in paper["qas"]:
            uid = qa["question_id"]
            if uid in previous_questions or len(qa["answers"]) != 1:
                continue
            a = qa["answers"][0]["answer"]
            negative = a["unanswerable"]
            spans, evidence = a["extractive_spans"], a["evidence"]
            if not negative and not (
                len(spans) == len(evidence) == 1
                and 2 <= len(spans[0]) <= 100
                and spans[0].lower() in evidence[0].lower()
                and evidence[0] in body
                and len(evidence[0]) <= 1200
            ):
                continue
            choices.append((sha(salt + uid), key, qa, a))
    cases, seen = [], set()
    for _, key, qa, a in sorted(choices):
        neg = a["unanswerable"]
        if key in seen or sum(c["negative"] == neg for c in cases) >= (
            negatives if neg else positives
        ):
            continue
        seen.add(key)
        cases.append(
            dict(
                id=qa["question_id"],
                scope="independent",
                query=f"In the paper “{raw[key]['title']}”, {qa['question']}",
                gold_documents=[key],
                negative=neg,
                answers=a["extractive_spans"],
                evidence=a["evidence"],
            )
        )
    assert len(cases) == positives + negatives
    return cases


def main():
    raw = read(TRAIN)
    # Inspect published result files (excluding this study), including per-query traces.
    paper_ids = re.compile(rb"(?<!\d)\d{4}\.\d{4,5}(?!\d)")
    question_ids = re.compile(rb"\b[0-9a-f]{40}\b")
    previous_papers, previous_questions = set(), set()
    for f in PUBLIC.rglob("*"):
        if OUT.parent in f.parents or f.suffix not in {".json", ".jsonl", ".md", ".csv", ".tsv"}:
            continue
        data = f.read_bytes()
        previous_papers.update(x.decode() for x in paper_ids.findall(data))
        previous_questions.update(x.decode() for x in question_ids.findall(data))
    cases = select(raw, previous_papers, previous_questions, "qa-first-response-v1|", 8, 4)
    old = read(OLD / "protocol-public.json")["cases"]
    for c in old:
        if c["scope"] == "qasper":
            key = c["gold_documents"][0]
            qa = next(x for x in raw[key]["qas"] if x["question_id"] == c["id"])
            cases.append(dict(c, evidence=qa["answers"][0]["answer"]["evidence"]))
        elif c["id"] in {"620", "791", "838-reproductive-functions", "missing-tenant-canary"}:
            cases.append(c)
    protocol = dict(
        baseline="f8e7878d72471a778b238ee0ff1fbefb0e80c18f",
        endpoint="POST /api/v1/knowledge/query",
        order=(
            "baseline then candidate, independent then historical QASPER then full SciFact; "
            "identical snapshots within each cohort"
        ),
        selection=(
            "TRAIN only, deterministic salted SHA256 of qid, one paper per question, exclude "
            "paper and question IDs found in prior published machine-readable results; "
            "single annotation; 8 extractive answerable, 4 official unanswerables"
        ),
        train_sha256=hashlib.sha256(TRAIN.read_bytes()).hexdigest(),
        cases=cases,
    )
    path = OUT / "protocol.json"
    with path.open("x", encoding="utf8", newline="\n") as f:
        json.dump(protocol, f, ensure_ascii=False, indent=2)
        f.write("\n")


if __name__ == "__main__":
    main()
