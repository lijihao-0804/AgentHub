"""Actual retrieval/citation audit with explicitly scoped assistant semantic labels."""

import json
import re
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path

from benchmarks.evaluation.artifacts import write_json_atomic
from benchmarks.evaluation.interview_draft import POLICIES
from benchmarks.evaluation.reviewed_support_data import reviewed_source
from packages.core.canonical.json_hash import canonical_json_hash
from packages.evaluation.metrics import percentile


def audit(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    # These assistant labels were curated for this exact completed run only.
    if (
        canonical_json_hash(data)
        != "25ceaf990af225af0c42170c81a31ffe686d8feb149a7cda8ff4cafcf689b39c"
    ):
        raise ValueError("CURATED_REVIEW_REPORT_MISMATCH")
    if data["status"] != "SUCCEEDED" or len(data["cases"]) != 144:
        raise ValueError("RAG_RUN_INCOMPLETE")
    source = reviewed_source()
    labels = {c["case_id"]: c for c in source["cases"]}
    policies = {p[0]: p for p in POLICIES}
    rows, claims, summaries = [], [], {}
    for c in data["cases"]:
        label = labels[c["case_id"]]
        observation = c["observation"]
        rag = observation["rag_evidence"]
        if rag["output_hash"] != canonical_json_hash(c["output"]):
            raise ValueError("RAG_OUTPUT_HASH_MISMATCH")
        trace = rag["retrieval_calls"]
        strategy = trace[0]["retrieval_strategy"] if trace else "UNKNOWN"
        relevant = {
            id for s in label["source_ids"] for id in data["plan"]["corpus_source_to_chunk_ids"][s]
        }
        final = rag["returned_chunk_ids"]
        candidate = list(dict.fromkeys(id for t in trace for id in t["candidate_chunk_ids"]))
        citations = observation["citation_ids"]
        # Repetition zero of every question/strategy was read against the policy.
        # Other semantic labels remain unknown, even when execution succeeded.
        reviewed = c["repetition"] == 0
        row = {
            "case_result_id": c["id"],
            "case_id": c["case_id"],
            "repetition": c["repetition"],
            "strategy": strategy,
            "output_hash": rag["output_hash"],
            "relevant_chunk_ids": sorted(relevant),
            "candidate_recall": len(relevant.intersection(candidate)) / len(relevant)
            if candidate or strategy != "DENSE"
            else None,
            "final_recall": len(relevant.intersection(final)) / len(relevant),
            "reciprocal_rank": next(
                (1 / (i + 1) for i, id in enumerate(final) if id in relevant), 0
            ),
            "citation_count": len(citations),
            "returned_citation_count": len(set(citations).intersection(final)),
            "relevant_citation_count": len(set(citations).intersection(relevant)),
            "required_source_cited": relevant.issubset(citations),
            "semantic_correct": True if reviewed else None,
            "reviewer_kind": "assistant" if reviewed else None,
            "review_reason": "required_policy_facts_and_unknown_fee_answer_verified"
            if reviewed
            else "not_sampled",
            "latency_ms": c["latency_ms"],
            "cost_cny": c["cost_amount"],
            "retrieval_stage_ms": {
                stage: sum(v for t in trace for v in t["timing"]["stage_durations_ms"][stage])
                for stage in ("dense", "sparse", "fusion", "rerank")
            },
        }
        rows.append(row)
        if reviewed and label["category"] == "multi_hop":
            _, _, window, requirement = policies[label["source_group"]]
            # Six primary-policy atomic claims, manually checked in the 16 answers.
            # Neighbor-policy commentary, advice and metadata are outside this corpus.
            for kind, marker in (
                ("window", window),
                ("requirement", requirement),
                ("no_completed_business", "不能承诺已经完成办理"),
                ("approval_required", "取得人工审批"),
                ("denial_no_ticket", "审批被拒绝不得创建工单"),
                ("fee_unspecified", "未说明"),
            ):
                line = next(line for line in c["output"].splitlines() if marker in line)
                inline_ids = set(re.findall(r"\[([0-9a-f]{64})\]", line))
                claims.append(
                    {
                        "case_result_id": c["id"],
                        "kind": kind,
                        "text": line,
                        "source_chunk_ids": sorted(relevant),
                        "supported": True,
                        "inline_citation_support": bool(inline_ids.intersection(relevant)),
                        "reviewer_kind": "assistant",
                        "output_hash": rag["output_hash"],
                    }
                )
    for strategy in sorted({r["strategy"] for r in rows}):
        subset = [r for r in rows if r["strategy"] == strategy]
        reviewed = [r for r in subset if r["semantic_correct"] is not None]
        sample_claims = [
            c for c in claims if c["case_result_id"] in {r["case_result_id"] for r in subset}
        ]
        total = sum((Decimal(r["cost_cny"]) for r in subset), Decimal(0))
        summaries[strategy] = {
            "planned_trials": 72,
            "actual_trials": len(subset),
            "candidate_recall_mean": sum(r["candidate_recall"] for r in subset) / len(subset)
            if all(r["candidate_recall"] is not None for r in subset)
            else None,
            "candidate_unknown_count": sum(r["candidate_recall"] is None for r in subset),
            "final_recall_mean": sum(r["final_recall"] for r in subset) / len(subset),
            "mrr": sum(r["reciprocal_rank"] for r in subset) / len(subset),
            "cited_returned_ids": sum(r["returned_citation_count"] for r in subset),
            "cited_relevant_ids": sum(r["relevant_citation_count"] for r in subset),
            "emitted_unique_ids_sum": sum(r["citation_count"] for r in subset),
            "required_source_cited_count": sum(r["required_source_cited"] for r in subset),
            "semantic_sample_correct": sum(r["semantic_correct"] is True for r in reviewed),
            "semantic_sample_count": len(reviewed),
            "semantic_unknown_count": len(subset) - len(reviewed),
            "primary_claim_sample_count": len(sample_claims),
            "unsupported_primary_claims": sum(not c["supported"] for c in sample_claims),
            "inline_cited_primary_claims": sum(c["inline_citation_support"] for c in sample_claims),
            "latency_p50_ms": percentile([r["latency_ms"] for r in subset], 0.5),
            "latency_p95_ms": percentile([r["latency_ms"] for r in subset], 0.95),
            "stage_p95_ms": {
                s: percentile([r["retrieval_stage_ms"][s] for r in subset], 0.95)
                for s in ("dense", "sparse", "fusion", "rerank")
            },
            "upper_total_cny": str(total),
            "cost_coverage": sum(r["cost_cny"] is not None for r in subset) / 72,
            "full_effective_semantic_task_cost": None,
            "reason": "full semantic success denominator unavailable "
            "outside repetition zero sample",
        }
    call_cost = sum((Decimal(c["upper_estimated_cost_cny"]) for c in data["calls"]), Decimal(0))
    runtime_cost = sum((Decimal(r["cost_cny"]) for r in rows), Decimal(0))
    if call_cost != runtime_cost:
        raise ValueError("RAG_COST_RECONCILIATION_MISMATCH")
    result = {
        "version": "rag-assistant-sampled-audit-v1",
        "raw_report_hash": canonical_json_hash(data),
        "source_content_hash": source["content_hash"],
        "rows": rows,
        "claims": claims,
        "summary": summaries,
        "status_counts": dict(Counter(c["status"] for c in data["cases"])),
        "adapter_cost_cny": str(call_cost),
        "runtime_cost_cny": str(runtime_cost),
        "cost_reconciliation": "PASS",
        "limitations": [
            "DEV only, eight short synthetic policies",
            "assistant labels, no human agreement",
            "semantic sample: all 24 questions in repetition zero per strategy, 48/144",
            "claim corpus: six primary-policy claims in each multi_hop repetition-zero answer, "
            "16/144 outputs; no exhaustive hallucination rate",
            "citation relevance is question source matching; "
            "citation existence does not prove entailment",
            "Dense candidate fusion trace absent: candidate recall unavailable, not zero",
            "same-policy multi-fact questions, not cross-document reasoning",
        ],
        "Q08": {"status": "NOT_AVAILABLE", "reason": "no independent human labels"},
    }
    write_json_atomic(path.with_suffix(".audit.json"), result)
    print(json.dumps({"summary": summaries, "adapter_cost_cny": str(call_cost)}))


if __name__ == "__main__":
    audit(Path(sys.argv[1]))
