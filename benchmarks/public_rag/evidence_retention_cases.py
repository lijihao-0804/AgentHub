"""Freeze and verify the two QASPER TRAIN cohorts without model or provider access."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

from benchmarks.public_rag.qa_first_response_cases import ROOT, TRAIN, paper_text, read, select

PUBLIC = ROOT / "benchmarks/public_rag/results/published"
OUT = PUBLIC / "evidence-retention-20261114"
OLD = PUBLIC / "qa-first-response-20261114/reader/protocol3.json"
BASELINE = "8f385b5ca105a252da9408369e9b77731d33a3c7"
EXPECTED_TRAIN = "9458bfe76074a8fa8d1685af02bcc73537aa6d338ad20591dfaff1946bc88bf4"
SALT = "qasper-evidence-retention-20261114/sample-v1|"
ORDER = "qasper-evidence-retention-20261114/order-v1|"
CONTROLS = ("c78f18606524539e4c573481e5bf1e0a242cc33c", "891cab2e41d6ba962778bda297592c916b432226")
ROOTS = ("benchmarks", "docs", "plan", "scripts", "tests", ".scratch", "output", ".worktrees")
SUFFIXES = (
    "json",
    "jsonl",
    "ndjson",
    "md",
    "csv",
    "tsv",
    "txt",
    "py",
    "yaml",
    "yml",
    "log",
    "patch",
)
SKIP = (
    "!**/evidence-retention*/**",
    "!**/*evidence_retention*",
    "!**/{.git,node_modules,.venv,__pycache__,.next,upstream}/**",
    "!**/data/external/**",
    "!**/qasper-{train,dev,test}-v*.json",
    "!**/.env*",
    "!**/{identities,private,credentials,secrets}.json",
)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def save_new(path, obj):
    with path.open("x", encoding="utf8", newline="\n") as stream:
        json.dump(obj, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def historical_exposure(raw, *, scratch_root=None, excluded_globs=SKIP, excluded_study=OUT):
    # Include paper-only identifiers, title-only mentions, questions with no IDs,
    # and questions' official parent papers as well as explicit reservations.
    labels = defaultdict(set)

    def register(kind, key, text):
        if not text.strip():
            return
        for variant in (text, json.dumps(text)[1:-1], json.dumps(text, ensure_ascii=False)[1:-1]):
            if "\n" not in variant and "\r" not in variant:
                labels[variant.lower()].add((kind, key))

    parents = {}
    for pid, p in raw.items():
        register("paper_id", pid, pid)
        register("paper_title", pid, p["title"])
        for q in p["qas"]:
            qid = q["question_id"]
            parents[qid] = pid
            register("question_id", qid, qid)
            register("question_text", qid, q["question"])

    witnesses = defaultdict(dict)
    scan_counts = defaultdict(set)

    def record(source, matched):
        for kind, key in labels[matched.decode("utf8").lower()]:
            first = witnesses[kind].get(key)
            if first is None or source < first:
                witnesses[kind][key] = source
            scan_counts[kind].add(source)

    def scan():
        # Keep scratch inputs for the local exposure audit. Starting rg in the
        # archive directory permits the historical source scan's self-exclusion.
        archive_root = scratch_root or ROOT / ".scratch/evidence-retention-20261114"
        tmp = archive_root / f"exposure-{uuid4().hex}"
        tmp.mkdir(parents=True)
        patterns = tmp / "patterns.txt"
        patterns.write_text("\n".join(sorted(labels)), encoding="utf8")
        # Old benchmark source archives include frozen manifests and one JSONL result.
        # Preserve virtual paths for their per-ID evidence.
        archives = tmp / "archives"
        archives.mkdir()
        for path in sorted((ROOT / "benchmarks/public_rag/results").rglob("*.zip")):
            if excluded_study in path.parents:
                continue
            with ZipFile(path) as zipped:
                for entry in zipped.infolist():
                    member = Path(entry.filename)
                    if member.suffix.lower().lstrip(".") not in SUFFIXES:
                        continue
                    if member.is_absolute() or ".." in member.parts or ":" in entry.filename:
                        raise ValueError("Unexpected archival member path")
                    dest = archives / path.relative_to(ROOT) / member
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(zipped.read(entry))
        command = [
            "rg",
            "--no-config",
            "-uu",
            "-F",
            "-i",
            "--only-matching",
            "--no-line-number",
            "--no-heading",
            "--with-filename",
            "--null",
            "--path-separator",
            "/",
            "--color",
            "never",
            "-f",
            str(patterns),
            "-g",
            "*.{" + ",".join(SUFFIXES) + "}",
        ]
        for pattern in excluded_globs:
            command.extend(("-g", pattern))
        scan_roots = [k for k in ROOTS if (ROOT / k).exists()]
        for location, targets in ((ROOT, scan_roots), (archives, ["."])):
            proc = subprocess.run(
                [*command, *targets], cwd=location, capture_output=True, check=False
            )
            if proc.returncode not in (0, 1):
                raise RuntimeError(proc.stderr.decode("utf8", "replace"))
            for line in proc.stdout.splitlines():
                name, value = line.split(b"\x00", 1)
                source = name.decode("utf8").replace("\\", "/").removeprefix("./")
                record(source, value)
        return scan_roots

    scan_roots = scan()
    qids = set(witnesses["question_id"]) | set(witnesses["question_text"])
    pids = set(witnesses["paper_id"]) | set(witnesses["paper_title"]) | {parents[q] for q in qids}
    reservations = read(PUBLIC / "qa-first-response-20261114/exposure/qasper-train.json")["groups"][
        "reserved_not_evaluated"
    ]
    if not set(reservations["papers"]) <= pids or not set(reservations["questions"]) <= qids:
        raise ValueError("Missing previously recorded reservations")
    return {
        "scan_roots": scan_roots,
        "extensions": SUFFIXES,
        "excluded_globs": excluded_globs,
        "archives": "Text members of local benchmarks/public_rag/results/**/*.zip",
        "matching": (
            "Case-insensitive literal official TRAIN paper ID, question ID, paper title or "
            "question wording, raw and JSON-escaped. Question matches exclude their parent "
            "papers. Shared wording excludes all associated questions and papers."
        ),
        "history_boundary": (
            "Locally available historical result, preparation, proposal, reservation, audit, "
            "source and scratch text, including unpublished benchmark records. Dataset mirrors, "
            "upstream source, secrets and this study are excluded. External or deleted records, "
            "unrecognizable paraphrases and opaque binary records are outside the scan."
        ),
        "historical_sources_with_matches": {k: len(v) for k, v in sorted(scan_counts.items())},
        "excluded_questions": sorted(qids),
        "excluded_papers": sorted(pids),
        "first_witness_by_kind": {k: dict(sorted(v.items())) for k, v in sorted(witnesses.items())},
        "prior_reservation_counts": {k: len(v) for k, v in reservations.items()},
    }


def build(raw, history):
    cases = select(
        raw, set(history["excluded_papers"]), set(history["excluded_questions"]), SALT, 30, 18
    )
    historical = [c for c in read(OLD)["cases"] if c["scope"] == "reserve16"]
    old = {c["id"]: c for c in historical}
    corpora = {
        "independent": [c["gold_documents"][0] for c in cases],
        "historical": [c["gold_documents"][0] for c in historical],
    }
    cases.extend({**old[key], "scope": "historical"} for key in CONTROLS)
    for scope in corpora:
        cohort = [c for c in cases if c["scope"] == scope]
        cohort.sort(key=lambda c: digest((ORDER + c["id"]).encode()))
        for n, case in enumerate(cohort):
            case["arm_order"] = ["candidate", "baseline"] if n % 2 else ["baseline", "candidate"]
    return cases, corpora


def freeze():
    if digest(TRAIN.read_bytes()) != EXPECTED_TRAIN:
        raise ValueError("Official TRAIN v0.3 hash")
    raw = read(TRAIN)
    history = historical_exposure(raw)
    cases, corpora = build(raw, history)
    fresh = [c for c in cases if c["scope"] == "independent"]
    history["independent_overlap"] = {
        "papers": sorted(set(corpora["independent"]) & set(history["excluded_papers"])),
        "questions": sorted({c["id"] for c in fresh} & set(history["excluded_questions"])),
    }
    if any(history["independent_overlap"].values()):
        raise ValueError("Independent cohort overlaps exposure records")
    OUT.mkdir(exist_ok=True)
    ledger_path = OUT / "exposure.json"
    protocol_path = OUT / "protocol.json"
    if ledger_path.exists() or protocol_path.exists():
        raise FileExistsError("Frozen cohort already exists")
    save_new(ledger_path, history)
    save_new(
        protocol_path,
        {
            "frozen_at_utc": datetime.now(UTC).isoformat(),
            "source_baseline_commit": BASELINE,
            "endpoint": "POST /api/v1/knowledge/query",
            "dataset": "QASPER v0.3 official TRAIN only; development evaluation",
            "train_sha256": EXPECTED_TRAIN,
            "exposure_sha256": digest(ledger_path.read_bytes()),
            "acceptance_sha256_lf": digest(
                (OUT / "acceptance.md").read_text(encoding="utf8").encode()
            ),
            "exclusion": "Explicit paper/QA historical exposure or reservations; see exposure.json",
            "sample": (
                "Ascending SHA256(sample salt + question ID), one question per unseen paper, "
                "one official annotation, rendered paper <=30,000 characters. Positives require "
                "one answer span of 2–100 characters in one verbatim paper evidence paragraph "
                "<=1,200 characters. Negatives require official unanswerable=true. Select until "
                "30 positives and 18 negatives; retain every selected question in its denominator."
            ),
            "sample_salt": SALT,
            "historical": (
                "Two exposed diagnostics reconstructed in the previous 16-paper library. The "
                "1,001-response question previously lost its evidence at reader cutoff. The "
                "Python-language question previously received full supported answers in all three "
                "arms. Controls are separate from every independent denominator."
            ),
            "historical_protocol_sha256_lf": digest(OLD.read_text(encoding="utf8").encode()),
            "order_salt": ORDER,
            "schedule": (
                "One first HTTP dispatch per arm in listed case order; arm_order alternates after "
                "sorting by SHA256(order salt + id) within scope for 24/24 and 1/1 first. Same "
                "retrieval/reader budget and generative settings. Keep failures and every internal "
                "provider call. Exact source/snapshot/wire metadata belong to execution receipts."
            ),
            "primary_evidence": (
                "At least one complete annotated evidence passage occurs case-insensitively in a "
                "single original target-paper chunk, at candidate@20 and actually sent reader@6. "
                "Denominator 30 for both; also report reader hits among candidate hits. Official "
                "negatives have no evidence target. Secondary metrics are original target-paper "
                "presence and exact official answer-span presence (no semantic expansion)."
            ),
            "answer_scoring": (
                "Arm-blind semantic rubric in qa-first-response-20261114/rubric.md: full, partial, "
                "wrong, structured refusal or failure, plus citation-level support. Preserve all "
                "official negative labels and describe source conflicts. Numerators: fully "
                "supported positive/30, correct structured negative refusal/18, sum/48, individual "
                "supporting "
                "citations/all citations. Report paired gains, losses, and retained previously "
                "correct cases, absolute percentage-point changes, and error rows."
            ),
            "corpora": corpora,
            "cases": cases,
        },
    )


def verify(directory=OUT):
    path = directory / "protocol.json"
    protocol = read(path)
    ledger_path = directory / "exposure.json"
    history = read(ledger_path)
    if not (
        digest(TRAIN.read_bytes()) == protocol["train_sha256"] == EXPECTED_TRAIN
        and digest(ledger_path.read_bytes().replace(b"\r\n", b"\n")) == protocol["exposure_sha256"]
        and digest(OLD.read_text(encoding="utf8").encode())
        == protocol["historical_protocol_sha256_lf"]
        and digest((directory / "acceptance.md").read_text(encoding="utf8").encode())
        == protocol["acceptance_sha256_lf"]
    ):
        raise ValueError("Frozen input differs")
    raw = read(TRAIN)
    cases, corpora = build(raw, history)
    if cases != protocol["cases"] or corpora != protocol["corpora"]:
        raise ValueError("Sample/order differs from deterministic reconstruction")
    fresh = [c for c in cases if c["scope"] == "independent"]
    if (
        len(fresh) != 48
        or sum(not c["negative"] for c in fresh) != 30
        or len(set(corpora["independent"])) != 48
        or len(set(corpora["historical"])) != 16
        or set(corpora["independent"]) & set(corpora["historical"])
        or len({c["id"] for c in cases}) != len(cases)
        or len({c["query"].casefold().strip() for c in fresh}) != 48
    ):
        raise ValueError("Cohort sizes or unique papers/questions differ")
    overlap = {
        "papers": sorted(set(corpora["independent"]) & set(history["excluded_papers"])),
        "questions": sorted({c["id"] for c in fresh} & set(history["excluded_questions"])),
    }
    if overlap != history["independent_overlap"] or any(overlap.values()):
        raise ValueError("Unexpected exposure")
    for case in cases:
        pid = case["gold_documents"][0]
        qa = next(q for q in raw[pid]["qas"] if q["question_id"] == case["id"])
        (annotation,) = qa["answers"]
        a = annotation["answer"]
        if not (
            case["query"] == f"In the paper “{raw[pid]['title']}”, {qa['question']}"
            and case["negative"] == a["unanswerable"]
            and case["answers"] == a["extractive_spans"]
            and case["evidence"] == a["evidence"]
            and all(text in paper_text(raw[pid]) for text in case["evidence"])
        ):
            raise ValueError(f"Official annotation mismatch: {case['id']}")
    return {
        "protocol_sha256_lf": digest(path.read_text(encoding="utf8").encode()),
        "excluded_questions": len(history["excluded_questions"]),
        "excluded_papers": len(history["excluded_papers"]),
        "independent_positives": 30,
        "independent_negatives": 18,
        "historical_diagnostics": len(CONTROLS),
        "primary_http_dispatches": len(cases) * 2,
    }


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--freeze", action="store_true", help="Create new exposure and protocol files")
    if cli.parse_args().freeze:
        freeze()
    print(json.dumps(verify(), ensure_ascii=False))
