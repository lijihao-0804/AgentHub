"""Audit local historical result IDs against official QASPER TRAIN. No inference or network."""

import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path


OUT = Path(__file__).resolve().parent
BASE = OUT.parents[2]
ROOT = BASE.parents[2]
TRAIN = ROOT / "data/external/rag-public-20261021/qasper/extracted/qasper-train-v0.3.json"
CURRENT = OUT.parent / "live/protocol.json"
ID = re.compile(rb"\b[a-f0-9]{40}\b")
CORPUS = json.loads(TRAIN.read_bytes())
PAPER = {q["question_id"]: pid for pid, paper in CORPUS.items() for q in paper["qas"]}


def lines(path):
    return (json.loads(s) for s in path.read_bytes().splitlines() if s.strip())


def main():
    sources = {}
    by_id = defaultdict(lambda: defaultdict(list))

    def add(kind, path, ids):
        ids = set(ids) & PAPER.keys()
        if ids:
            name = path.relative_to(BASE).as_posix()
            sources[name] = hashlib.sha256(path.read_bytes()).hexdigest()
            for qid in ids:
                by_id[qid][kind].append(name)

    for pattern in ("paper-qa-live-*/live-run-*/arms.jsonl", "paragraph-qa-*/live-run-*/arms.jsonl",
                    "answer-path-*/*/answers.jsonl", "entity-delivery-*/*/answers.jsonl",
                    "rag-root-cause-*/reader-*/**/answers.jsonl"):
        for path in sorted(BASE.glob(pattern)):
            ids = []
            for row in lines(path):
                if row["result"].get("physical_outcomes"):
                    qid = row.get("query_id") or next((v for v in row.get("key", []) if v in PAPER), None)
                    ids.append(qid)
            add("answer_generation_dispatch", path, ids)
    for path in sorted((BASE / "published/article-recall-20261114/live").glob("qasper-*.json")):
        obj = json.loads(path.read_bytes())
        if obj.get("endpoint") == "POST /api/v1/knowledge/query":
            add("answer_generation_dispatch", path, (r["id"] for r in obj["rows"] if r["state"] == "returned"))
    for path in sorted(BASE.glob("scenario-rag-*/*qasper*/run.jsonl")):
        if json.loads((path.parent / "meta.json").read_bytes())["split"] == "train":
            add("retrieval_run", path, (r["query_id"] for r in lines(path)))
            events = path.parent / "request-events.jsonl"
            if events.exists():
                add("selector_model_dispatch", events, (r["id"] for r in lines(events) if r["event"] == "RESPONSE_CAPTURED"))
    for path in sorted(BASE.glob("rag-root-cause-*/paragraph-*/*/*")):
        if path.name in ("views.jsonl", "retrievals.jsonl"):
            add("retrieval_run", path, (v.decode() for v in ID.findall(path.read_bytes())))
    for path in sorted(BASE.glob("rag-root-cause-*/development-proposal*.json")):
        add("reserved_not_evaluated", path, json.loads(path.read_bytes())["reservation"]["query_ids"])
    # Scan historical receipts for other appearances, including proposals and reviews.
    # Never open current-run answers even during the exhaustive ID-only scan.
    for path in sorted(BASE.rglob("*")):
        if OUT.parent in path.parents or path.suffix not in (".json", ".jsonl"):
            continue
        add("historical_id_mention", path, (v.decode() for v in ID.findall(path.read_bytes())))

    groups = {}
    for kind in ("answer_generation_dispatch", "selector_model_dispatch", "retrieval_run",
                 "reserved_not_evaluated", "historical_id_mention"):
        ids = sorted(q for q, events in by_id.items() if kind in events)
        groups[kind] = {"questions": ids, "papers": sorted({PAPER[q] for q in ids})}
    independent = {r["id"] for r in json.loads(CURRENT.read_bytes())["cases"] if r["scope"] == "independent"}
    overlap = {
        kind: {"question_intersection": sorted(independent & set(values["questions"])),
               "paper_intersection": sorted({PAPER[q] for q in independent} & set(values["papers"]))}
        for kind, values in groups.items()
    }
    ledger = {"train_sha256": hashlib.sha256(TRAIN.read_bytes()).hexdigest(),
              "current_protocol_sha256": hashlib.sha256(CURRENT.read_bytes()).hexdigest(),
              "groups": groups, "frozen_12_overlap": overlap, "source_sha256": sources,
              "receipts_by_question": {q: {"paper_id": PAPER[q], **events} for q, events in sorted(by_id.items())}}
    (OUT / "qasper-train.json").write_text(json.dumps(ledger, indent=2) + "\n", encoding="utf8")
    for kind, values in groups.items():
        print(kind, len(values["questions"]), len(values["papers"]), overlap[kind])


if __name__ == "__main__":
    main()
