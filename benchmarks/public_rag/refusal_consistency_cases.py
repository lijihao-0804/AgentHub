"""Freeze and reconstruct a disjoint QASPER train evaluation for refusal consistency."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime

from benchmarks.public_rag import evidence_retention_cases as prior
from benchmarks.public_rag.qa_first_response_cases import ROOT, TRAIN, paper_text, read, select

OUT = prior.PUBLIC / "refusal-consistency-20261114"
SCRATCH = ROOT / ".scratch/refusal-consistency-20261114"
BASELINE = "46362e9245c7a708d015f1bd2a462d850cb0e118"
SAMPLE = "qasper-refusal-consistency-20261114/sample-v1|"
ORDER = "qasper-refusal-consistency-20261114/order-v1|"
RUBRIC = prior.PUBLIC / "qa-first-response-20261114/rubric.md"
VIETNAM = "891c4af5bb77d6b8635ec4109572de3401b60631"


def lf_sha(path):
    return prior.digest(path.read_bytes().replace(b"\r\n", b"\n"))


def exposure(raw):
    # The previous study's pattern files contain a deliberately complete TRAIN
    # dictionary. Its published results, scripts and all question reservations
    # remain eligible witnesses. The prior study's inaccessible tempfile is
    # outside the local scan; its known records are covered by the published ledger.
    globs = (
        "!**/refusal-consistency*",
        "!**/*refusal_consistency*",
        "!**/evidence-retention-20261114/exposure-*",
        "!**/evidence-retention-20261114/tmp6hggowme",
        *prior.SKIP[2:],
    )
    ledger = prior.historical_exposure(
        raw, scratch_root=SCRATCH, excluded_globs=globs, excluded_study=OUT
    )
    previous = read(prior.OUT / "exposure.json")
    last = read(prior.OUT / "protocol.json")
    papers = set(ledger["excluded_papers"])
    questions = set(ledger["excluded_questions"])
    if not (
        set(previous["excluded_questions"]) <= questions
        and set(previous["excluded_papers"]) <= papers
        and all(set(ids) <= papers for ids in last["corpora"].values())
        and all(c["id"] in questions for c in last["cases"])
    ):
        raise ValueError("Earlier witnesses, previously evaluated cohorts or reservations missing")

    # Split-word layout and shared wordings between two papers must not turn an
    # old question into a new one just because it has a distinct official ID.
    def wording(qa):
        return " ".join(qa["question"].casefold().split())

    old_wordings = {
        wording(q) for p in raw.values() for q in p["qas"] if q["question_id"] in questions
    }
    copies = sorted(
        (q["question_id"], key)
        for key, p in raw.items()
        for q in p["qas"]
        if wording(q) in old_wordings and q["question_id"] not in questions
    )
    questions.update(q for q, _ in copies)
    papers.update(p for _, p in copies)
    ledger.update(
        prior_ledger_sha256_lf=lf_sha(prior.OUT / "exposure.json"),
        prior_protocol_sha256_lf=lf_sha(prior.OUT / "protocol.json"),
        normalized_duplicate_question_parents=dict(copies),
        excluded_papers=sorted(papers),
        excluded_questions=sorted(questions),
    )
    return ledger


def build(raw, ledger):
    independent = select(
        raw,
        set(ledger["excluded_papers"]),
        set(ledger["excluded_questions"]),
        SAMPLE,
        30,
        20,
        unique_wording=True,
    )
    previous = read(prior.OUT / "protocol.json")
    old = {c["id"]: c for c in previous["cases"]}
    corpora = {
        "independent": sorted(c["gold_documents"][0] for c in independent),
        "historical": previous["corpora"]["historical"],
        "historical_vietnam": previous["corpora"]["independent"],
    }
    positive = sorted((c for c in independent if not c["negative"]), key=order_key)
    negative = sorted((c for c in independent if c["negative"]), key=order_key)
    for group in (positive, negative):
        for i, case in enumerate(group):
            case["arm_order"] = ["candidate", "baseline"] if i % 2 else ["baseline", "candidate"]
    controls = []
    for i, cid in enumerate((*prior.CONTROLS, VIETNAM)):
        controls.append(
            {
                **old[cid],
                "scope": "historical_vietnam" if cid == VIETNAM else "historical",
                "arm_order": ["candidate", "baseline"] if i % 2 else ["baseline", "candidate"],
            }
        )
    return sorted(independent, key=order_key) + controls, corpora


def order_key(case):
    return prior.digest((ORDER + case["id"]).encode())


def source_text_hashes(raw, corpora):
    return {
        pid: prior.digest(paper_text(raw[pid]).encode())
        for pid in sorted({pid for keys in corpora.values() for pid in keys})
    }


def freeze():
    if prior.digest(TRAIN.read_bytes()) != prior.EXPECTED_TRAIN:
        raise ValueError("Official QASPER v0.3 TRAIN hash differs")
    OUT.mkdir(exist_ok=True)
    ledger_path, protocol_path = OUT / "exposure.json", OUT / "protocol.json"
    if ledger_path.exists() or protocol_path.exists():
        raise FileExistsError("The cohort has already been frozen")
    raw = read(TRAIN)
    ledger = exposure(raw)
    cases, corpora = build(raw, ledger)
    ledger["independent_overlap"] = {
        "papers": sorted(set(corpora["independent"]) & set(ledger["excluded_papers"])),
        "questions": sorted(
            {c["id"] for c in cases if c["scope"] == "independent"}
            & set(ledger["excluded_questions"])
        ),
    }
    if any(ledger["independent_overlap"].values()):
        raise ValueError("Independent cohort overlaps previously used or reserved material")
    prior.save_new(ledger_path, ledger)
    prior.save_new(
        protocol_path,
        {
            "frozen_at_utc": datetime.now(UTC).isoformat(),
            "source_baseline_commit": BASELINE,
            "endpoint": "POST /api/v1/knowledge/query",
            "dataset": "QASPER v0.3 official TRAIN only; paired development evaluation",
            "train_sha256": prior.EXPECTED_TRAIN,
            "exposure_sha256_lf": lf_sha(ledger_path),
            "acceptance_sha256_lf": lf_sha(OUT / "acceptance.md"),
            "semantic_rubric_sha256_lf": lf_sha(RUBRIC),
            "whole_answer_rubric_sha256_lf": lf_sha(OUT / "rubric.md"),
            "sample_salt": SAMPLE,
            "order_salt": ORDER,
            "selection": (
                "Ascending SHA-256(sample salt + official question ID). Exclude historical or "
                "reserved papers and questions; require one distinct normalized question wording "
                "and one question per paper, paper text <=30,000 characters, and exactly one "
                "annotation. A positive has one 2–100-character extractive answer in one "
                "<=1,200-character verbatim evidence paragraph. "
                "A negative has official unanswerable=true. Stop at 30 positives and 20 negatives."
            ),
            "schedule": (
                "Independent questions sorted by SHA-256(order salt + official question ID). "
                "Alternate A/B first separately within each answerability stratum (15/15 "
                "answerable, 10/10 negative). One first HTTP dispatch per question per arm. Two "
                "arms share the 50-paper snapshot; retain every failure and capture internal "
                "provider calls separately."
            ),
            "historical_controls": (
                "The 1,001-response and Python controls use the former 16-paper library; the "
                "Vietnamese platform control uses the former 48-paper library. Six additional "
                "first HTTP requests."
            ),
            "answer_scoring": (
                "Two independently recorded arm-blind judgments and masked dispute resolution. "
                "Official negative labels are retained despite evidence conflicts. Score full "
                "correctness, positive false refusals, correct negative refusals, whole-answer "
                "claim support, and each returned citation. Use the frozen rubric and acceptance "
                "document."
            ),
            "corpora": corpora,
            "rendered_paper_utf8_sha256": source_text_hashes(raw, corpora),
            "cases": cases,
        },
    )


def verify(directory=OUT):
    protocol_path = directory / "protocol.json"
    protocol, ledger, raw = read(protocol_path), read(directory / "exposure.json"), read(TRAIN)
    cases, corpora = build(raw, ledger)
    fresh = [c for c in cases if c["scope"] == "independent"]
    if not (
        protocol["source_baseline_commit"] == BASELINE
        and prior.digest(TRAIN.read_bytes()) == protocol["train_sha256"] == prior.EXPECTED_TRAIN
        and lf_sha(directory / "exposure.json") == protocol["exposure_sha256_lf"]
        and lf_sha(directory / "acceptance.md") == protocol["acceptance_sha256_lf"]
        and lf_sha(RUBRIC) == protocol["semantic_rubric_sha256_lf"]
        and lf_sha(directory / "rubric.md") == protocol["whole_answer_rubric_sha256_lf"]
        and lf_sha(prior.OUT / "exposure.json") == ledger["prior_ledger_sha256_lf"]
        and lf_sha(prior.OUT / "protocol.json") == ledger["prior_protocol_sha256_lf"]
        and protocol["cases"] == cases
        and protocol["corpora"] == corpora
        and source_text_hashes(raw, corpora) == protocol["rendered_paper_utf8_sha256"]
        and len(fresh) == len(set(corpora["independent"])) == 50
        and len(cases) == 53
        and len({c["id"] for c in cases}) == 53
        and len(corpora["historical"]) == 16
        and len(corpora["historical_vietnam"]) == 48
        and not (set(corpora["independent"]) & set(ledger["excluded_papers"]))
        and not ({c["id"] for c in fresh} & set(ledger["excluded_questions"]))
        and ledger["independent_overlap"] == {"papers": [], "questions": []}
    ):
        raise ValueError("Source, history, sample, snapshot or scoring freeze differs")
    for case in cases:
        paper = raw[case["gold_documents"][0]]
        qa = next(q for q in paper["qas"] if q["question_id"] == case["id"])
        (annotation,) = qa["answers"]
        a = annotation["answer"]
        if not (
            case["query"] == f"In the paper “{paper['title']}”, {qa['question']}"
            and case["negative"] == a["unanswerable"]
            and case["answers"] == a["extractive_spans"]
            and case["evidence"] == a["evidence"]
            and all(s in paper_text(paper) for s in case["evidence"])
        ):
            raise ValueError(f"Official gold mismatch: {case['id']}")
    for negative, count in ((False, 30), (True, 20)):
        group = [c for c in fresh if c["negative"] is negative]
        if len(group) != count or sum(c["arm_order"][0] == "baseline" for c in group) != count // 2:
            raise ValueError("Paired order or stratum size differs")
    return {
        "protocol_sha256_lf": lf_sha(protocol_path),
        "excluded_papers": len(ledger["excluded_papers"]),
        "excluded_questions": len(ledger["excluded_questions"]),
        "independent_distinct_papers": len(set(corpora["independent"])),
        "independent_first_http_dispatches": 2 * len(fresh),
        "historical_first_http_dispatches": 2 * (len(cases) - len(fresh)),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze", action="store_true")
    if parser.parse_args().freeze:
        freeze()
    print(json.dumps(verify()))
