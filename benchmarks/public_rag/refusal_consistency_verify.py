"""Independently verify the paired first-HTTP receipts against the original paper text."""

from __future__ import annotations

import argparse
import hashlib
from collections import Counter
from datetime import datetime
from pathlib import Path

from benchmarks.public_rag.evidence_retention_verify import check_receipt, retention
from benchmarks.public_rag.qa_first_response_cases import TRAIN, paper_text
from benchmarks.public_rag.qa_first_response_review import index, load, save
from benchmarks.public_rag.qa_first_response_verify import canon, events_of, require, sha, summarize
from benchmarks.public_rag.refusal_consistency_live_results import PRIOR, historical_evidence
from packages.knowledge.citation_qa import _CitationQaModelOutput, _validate_model_output
from packages.knowledge.refusal_consistency import explicit_abstention

RESULTS = Path("benchmarks/public_rag/results/published/refusal-consistency-20261114")


def apply_both_to_terminal(case, row, arm):
    """Attribute a public result to the upstream generation or the deterministic rule."""
    (variant,) = events_of(row, "qa_variant")
    require(
        variant["arm"] == arm and variant["verify_refusal_consistency"] is (arm == "B"),
        "Request-local reader variant",
    )
    outputs = events_of(row, "model_output")
    if row["http"] != 200:
        return {"http": row["http"], "terminal_replay": False}
    require(outputs and outputs[-1]["json_status"] == "schema_valid", "No accepted provider output")
    output = _CitationQaModelOutput.model_validate(outputs[-1]["output"], strict=True)
    ids = list(_validate_model_output(output, evidence_count=6))
    normalized = {
        "answer": output.answer,
        "citation_ids": ids,
        "insufficient_evidence": output.insufficient_evidence,
    }
    abstention = explicit_abstention(case["query"], output.answer) if ids else None
    corrected = (
        normalized
        if abstention is None
        else {"answer": abstention, "citation_ids": [], "insufficient_evidence": True}
    )
    observed = {
        "answer": row["answer"],
        "citation_ids": [c["number"] for c in row["citations"]],
        "insufficient_evidence": row["insufficient"],
    }
    require(observed == (normalized if arm == "A" else corrected), "Replay and real HTTP disagree")
    return {
        "http": row["http"],
        "terminal_replay": True,
        "model_flag": output.insufficient_evidence,
        "public_flag": row["insufficient"],
        "rule_changes_same_output": normalized != corrected,
        "baseline_terminal_sha256": sha(canon(normalized)),
        "fixed_terminal_sha256": sha(canon(corrected)),
    }


def collected_receipts(live):
    rows = {"A": {}, "B": {}}
    for path in sorted(live.glob("*.json")):
        value = load(path)
        if value.get("endpoint") != "POST /api/v1/knowledge/query":
            continue
        arm = rows[value["arm"]]
        for cid, row in index(value["rows"]).items():
            require(cid not in arm, f"Duplicate receipt for {cid} in {path}")
            arm[cid] = ({k: v for k, v in value.items() if k != "rows"}, row)
    return rows


def historical_parity(cases, receipts):
    controls = {}
    for cid, case in cases.items():
        if case["scope"] == "independent":
            continue
        result = historical_evidence(case, receipts["B"][cid][1], PRIOR)
        controls[cid] = {name: value for name, value in result.items() if name.startswith("same_")}
    return controls


def audit(root, train):
    protocol_path = root / "protocol.json"
    protocol = load(protocol_path)
    freeze = load(root / "execution-freeze.json")
    dataset_sha = hashlib.sha256(train.read_bytes()).hexdigest()
    require(dataset_sha == protocol["train_sha256"], "Training corpus identity")
    protocol_sha = sha(protocol_path.read_text("utf-8"))
    require(freeze["protocol_sha256"] == protocol_sha, "Execution freeze protocol mismatch")
    docs = {key: paper_text(paper) for key, paper in load(train).items()}
    cases = index(protocol["cases"])
    snapshots = {s["snapshot"]: s for f in (root / "snapshots").glob("*.json") if (s := load(f))}
    for snapshot in snapshots.values():
        for paper_id, doc in snapshot["documents"].items():
            require(
                doc["paper_text_sha256"] == sha(docs[paper_id]), "Snapshot differs from train paper"
            )
    receipts = collected_receipts(root / "live")
    require(all(set(arm) == cases.keys() for arm in receipts.values()), "Paired roster")
    fresh = [cid for cid, c in cases.items() if c["scope"] == "independent"]
    require(len(fresh) == 50 and len(cases) == 53, "Original 50 plus three controls")
    require(
        len({cases[cid]["gold_documents"][0] for cid in fresh}) == 50, "Paper-level independence"
    )
    require(sum(cases[cid]["negative"] for cid in fresh) == 20, "Official-negative denominator")

    details = {}
    counts = Counter()
    request_ids = set()
    for cid, case in cases.items():
        pair = {}
        meta_a, row_a = receipts["A"][cid]
        meta_b, row_b = receipts["B"][cid]
        for field in (
            "model",
            "profile",
            "workspace",
            "knowledge_base",
            "snapshot",
            "snapshot_hash",
            "documents",
            "chunks",
            "retrieval_profile",
            "output_mode",
            "request_mode",
        ):
            require(meta_a[field] == meta_b[field], f"{cid}: paired {field} changed")
        require(
            meta_a["output_mode"] == "PROMPT_JSON" and meta_a["request_mode"] == "provider_default",
            "Reader output or request mode",
        )
        snapshot = snapshots[meta_a["snapshot"]]
        require(
            set(snapshot["documents"]) == set(protocol["corpora"][case["scope"]]),
            f"{cid}: retrieval universe differs from frozen scope",
        )
        for arm, (meta, row) in (("A", (meta_a, row_a)), ("B", (meta_b, row_b))):
            (variant,) = events_of(row, "qa_variant")
            require(
                variant["qa_source_sha256_lf"] == freeze["qa_source_sha256_lf"][arm]
                and meta["source"] == freeze["source"]
                and meta["snapshot"] == freeze["scope_snapshots"][case["scope"]]["snapshot"]
                and meta["profile"] == freeze["profile"]
                and meta["model"] == freeze["model"],
                f"{cid}/{arm}: execution freeze identity",
            )
            checks = check_receipt(
                {**meta, "rows": [row]},
                {cid: case},
                arm,
                protocol_sha,
                docs,
                False,
                snapshot,
                reranker_arm="B",
            )[cid]
            require(
                row["request_id"] and row["request_id"] not in request_ids, "Duplicate request ID"
            )
            request_ids.add(row["request_id"])
            first_wire, *corrections = events_of(row, "http_wire")
            model_outputs = events_of(row, "model_output")
            require(len(model_outputs) >= len(corrections), f"{cid}: missing corrected output")
            for prior, correction in zip(
                model_outputs[: len(corrections)], corrections, strict=True
            ):
                require(
                    correction["content_sha256"][2] == prior["content_sha256"],
                    f"{cid}/{arm}: the correction did not quote the previous model output",
                )
            checks["wire_initial_content_sha256"] = first_wire["content_sha256"]
            checks["matched_output"] = apply_both_to_terminal(case, row, arm)
            pair[arm] = checks
        for field in ("digest", "prompt_sha", "wire_initial_content_sha256", "candidate", "reader"):
            require(pair["A"][field] == pair["B"][field], f"{cid}: paired {field}")
        require(
            row_a["request_payload_sha256"] == row_b["request_payload_sha256"],
            f"{cid}: public request payload differs",
        )
        order = {"A": "A", "B": "B", "baseline": "A", "candidate": "B"}[case["arm_order"][0]]
        before, after = (row_a, row_b) if order == "A" else (row_b, row_a)
        require(
            datetime.fromisoformat(before["utc"]) <= datetime.fromisoformat(after["utc"]),
            f"{cid}: violated frozen within-question order",
        )
        if cid in fresh:
            counts[order] += 1
        details[cid] = pair
    require(counts == {"A": 25, "B": 25}, "Independent order balance")

    def scope_result(ids):
        return {arm: summarize([details[cid][arm] for cid in ids]) for arm in ("A", "B")}

    return {
        "protocol_sha256_lf": protocol_sha,
        "independent": scope_result(fresh),
        "controls": scope_result([cid for cid in cases if cid not in fresh]),
        "historical_sentence_reranker_parity": historical_parity(cases, receipts),
        "identical_pool_six_passages_and_initial_prompt": len(details),
        "independent_evidence": retention([details[cid]["A"] for cid in fresh]),
        "matched_independent_changes": {
            arm: [
                cid
                for cid in fresh
                if details[cid][arm]["matched_output"].get("rule_changes_same_output", False)
            ]
            for arm in ("A", "B")
        },
        "first_arms_independent": dict(counts),
        "rows": details,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=RESULTS)
    parser.add_argument("--train", type=Path, default=TRAIN)
    parser.add_argument("--check", action="store_true", help="Compare with the published audit")
    args = parser.parse_args()
    path = args.root / "receipt-audit.json"
    result = audit(args.root, args.train)
    if args.check:
        require(canon(load(path)) == canon(result), "Published audit differs from the receipts")
    else:
        save(path, result)
    print(f"{result['identical_pool_six_passages_and_initial_prompt']} verified pairs")


if __name__ == "__main__":
    main()
