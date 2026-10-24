"""Freeze paper-isolated development proposals and an unopened reservation.

Selection uses identifiers only, after a conservative local exposure scan.
Public annotations are retained for offline review, not certified sufficient.
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from benchmarks.public_rag.paper_qa_data import (
    _case,
    _corpus_paper,
    _question_id,
    _round_robin_questions,
)

ROOT = Path("benchmarks/public_rag/results")
SEED = 2026102410


def source_keys(paper):
    """Conservative exact-content overlap checks; no answer labels or hashes."""

    def normal(text):
        return " ".join(text.casefold().split())

    keys = {("title", normal(paper["title"]))}
    keys.update(("question", normal(qa["question"])) for qa in paper["qas"])
    keys.update(
        ("paragraph", normal(text))
        for section in paper["full_text"]
        for text in section["paragraphs"]
        if len(normal(text)) >= 200
    )
    return keys


def paper_ids(value, train_ids):
    found = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"paper_id", "paper", "document_id", "group_id"} and isinstance(child, str):
                if child in train_ids:
                    found.add(child)
            elif key in {"selected_paper_ids", "paper_ids", "group_ids"} and isinstance(
                child, list
            ):
                found.update(v for v in child if isinstance(v, str) and v in train_ids)
            # Source maps can use paper IDs as their object keys.
            if key in train_ids:
                found.add(key)
            found.update(paper_ids(child, train_ids))
    elif isinstance(value, list):
        for child in value:
            found.update(paper_ids(child, train_ids))
    return found


def scan_exposure(train_ids, results_root=ROOT):
    excluded, ledger, unresolved = set(), [], []
    for group in sorted(results_root.iterdir()):
        if not group.is_dir() or group.name in {"published", "rag-root-cause-20261024"}:
            continue
        for path in sorted(group.rglob("*")):
            if not path.is_file() or path.suffix not in {".json", ".jsonl"}:
                continue
            try:
                if path.suffix == ".jsonl":
                    ids = set()
                    with path.open(encoding="utf-8-sig") as stream:
                        for line in stream:
                            if line.strip():
                                ids.update(paper_ids(json.loads(line), train_ids))
                else:
                    ids = paper_ids(json.loads(path.read_text(encoding="utf-8-sig")), train_ids)
                if ids:
                    excluded.update(ids)
                    ledger.append({"artifact": path.as_posix(), "paper_ids": sorted(ids)})
            except (OSError, ValueError, RecursionError) as error:
                unresolved.append({"artifact": path.as_posix(), "error_type": type(error).__name__})
    return excluded, ledger, unresolved


def prepare(train_path, output):
    if output.exists():
        raise FileExistsError("refusing to overwrite frozen sampling")
    papers = json.loads(train_path.read_text(encoding="utf-8"))
    excluded, ledger, unresolved = scan_exposure(set(papers))
    available = sorted(set(papers) - excluded)
    random.Random(SEED).shuffle(available)
    seen = set().union(*(source_keys(papers[pid]) for pid in excluded)) if excluded else set()
    selected_papers, overlap_exclusions = [], []
    for pid in available:
        keys = source_keys(papers[pid])
        if keys & seen:
            overlap_exclusions.append(pid)
            continue
        selected_papers.append(pid)
        seen.update(keys)
        if len(selected_papers) == 30:
            break
    if len(selected_papers) < 30:
        raise ValueError("fewer than 30 source-isolated papers; preserve exposed diagnostic scope")
    dev, reserved = selected_papers[:20], selected_papers[20:30]
    questions = {
        pid: [_question_id(qa, pid) for qa in papers[pid]["qas"]] for pid in [*dev, *reserved]
    }
    selected = _round_robin_questions(dev, questions, max_questions=60)
    reserved_questions = _round_robin_questions(reserved, questions, max_questions=30)
    selected_ids = set(selected)
    cases = [
        _case(pid, qa)
        for pid in dev
        for qa in papers[pid]["qas"]
        if (pid, _question_id(qa, pid)) in selected_ids
    ]
    by_id = {(c["paper_id"], c["query_id"]): c for c in cases}
    cases = [by_id[key] for key in selected]
    # Copy only development content. Reserved labels/paragraphs are not exported.
    data = {
        "state": "FROZEN_DEVELOPMENT_PROPOSAL_NOT_SOURCE_REVIEWED",
        "source": {
            "dataset": "QASPER",
            "version": "0.3",
            "split": "train",
            "file": train_path.as_posix(),
            "seed": SEED,
        },
        "selection": {
            "development_papers": dev,
            "excluded_papers": sorted(excluded),
            "development_queries": len(cases),
            "available_papers": len(available),
            "label_blind": True,
            "answers_used_for_selection": False,
            "additional_overlap_exclusions": overlap_exclusions,
            "overlap_rule": "Exact normalized title/question or paragraph >=200 chars; "
            "checked against known exposed papers and across development/reserved papers. "
            "Paraphrased or latent external pretraining overlap remains UNKNOWN.",
        },
        "review": {
            "status": "PENDING_SOURCE_REVIEW",
            "annotation_origin": "QASPER public human annotations",
            "expert_review": False,
            "semantic_sufficiency": "UNKNOWN",
            "question_requirements": "PENDING_MACHINE_ASSISTED_REVIEW",
            "not_a_ready_semantic_gold_dataset": True,
        },
        "corpus": [_corpus_paper(pid, papers[pid]) for pid in dev],
        "cases": cases,
        "reservation": {
            "paper_ids": reserved,
            "query_ids": [qid for _, qid in reserved_questions],
            "state": "RESERVED_NOT_EVALUATED",
            "labels_exported": False,
            "purpose": "future source-isolated confirmation only after frozen method selection",
        },
        "exposure_audit": {
            "sources": ledger,
            "unresolved": unresolved,
            "known_paper_overlap": sorted(set(dev + reserved) & excluded),
            "guarantee": "No overlap with machine-readable local JSON/JSONL identities found. "
            "Archive-only, absent or identity-free prior exposure remains UNKNOWN.",
        },
        "planned_new_model_requests": 0,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(
        json.dumps(
            {
                "development_cases": len(cases),
                "development_papers": len(dev),
                "reserved_queries": len(reserved_questions),
                "reserved_papers": len(reserved),
                "excluded_papers": len(excluded),
                "unresolved_ledger_files": len(unresolved),
                "new_model_calls": 0,
            }
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--train-path",
        type=Path,
        default=Path("data/external/rag-public-20261021/qasper/extracted/qasper-train-v0.3.json"),
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.train_path, args.output)
