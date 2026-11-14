"""Validate frozen sources, shared ranks, request sizes and all three dispatch paths offline."""

import argparse
import ast
import json
import subprocess
from collections import Counter
from pathlib import Path

from benchmarks.public_rag.confirmation_cases import OUT, ROOT, digest, read, save
from benchmarks.public_rag.confirmation_meter import INPUT, LIMITS, MODEL, PER_REQUEST, input_bound
from benchmarks.public_rag.confirmation_pool import blind_inputs
from benchmarks.public_rag.product_confirmation import evaluate
from benchmarks.public_rag.scifact_recall_forensics import vector_hit
from packages.knowledge.retrieval import fuse_reciprocal_rank
from packages.model_gateway.contracts import ModelResponse

A = "a2d47e4"
B = "8f385b5ca105a252da9408369e9b77731d33a3c7"


def historical_functions(ref):
    source = subprocess.check_output(
        ["git", "show", f"{ref}:packages/knowledge/retrieval.py"], cwd=ROOT
    )
    wanted = {
        "fuse_reciprocal_rank",
        "_ranked_hits",
        "_payload_chunk_id",
        "select_rerank_candidates",
    }
    return {
        n.name: digest(ast.dump(n).encode())
        for n in ast.parse(source).body
        if isinstance(n, ast.FunctionDef) and n.name in wanted
    }


class ContractProbe:
    def __init__(self, stage, counts, sizes):
        self.stage, self.counts, self.sizes = stage, counts, sizes

    async def generate_resolved(self, context, plan, request):
        payload = {
            "messages": [dict(role=m.role, content=m.content) for m in request.messages],
            "temperature": 0,
            "max_tokens": plan.primary.max_tokens,
            "extra_body": {"enable_thinking": False},
        }
        size = input_bound(payload)
        if size > INPUT:
            raise ValueError("Study request would fail physical input admission")
        self.sizes.append(size)
        self.counts[self.stage] += 1
        # Deliberately scripted JSON for a transport/schema dry run. No semantic judgments.
        raw = (
            '{"selected_ids":[0,1,2,3,4,5]}'
            if self.stage == "selector"
            else json.dumps(
                dict(answer="Insufficient evidence.", insufficient_evidence=True, citation_ids=[])
            )
        )
        return ModelResponse(raw, "scripted-contract", MODEL, finish_reason="stop")


async def preflight(pools):
    sources, _ = blind_inputs()
    defs = read(OUT / "cohort.json")["cases"]
    reviews = [r for name in ("a", "b") for r in read(OUT / f"source-review-{name}.json")]
    negatives = {c["id"]: c for c in defs if c["negative"]}
    if len(reviews) != 16 or {r["id"] for r in reviews} != set(negatives):
        raise ValueError("Missing or duplicated pre-output source adjudication")
    for r in reviews:
        body = sources[negatives[r["id"]]["gold_documents"][0]][1]
        if not r["quotes"] or not all(q and q in body for q in r["quotes"]):
            raise ValueError("Adjudication quote outside source paper")
    a, b = historical_functions(A), historical_functions(B)
    if len(a) != 4 or a != b:
        raise ValueError("Historical retrieval mechanisms diverged")
    case_rows = read(pools)
    counts, sizes = Counter(), []
    for c in case_rows["cases"]:
        pool = fuse_reciprocal_rank(*(tuple(map(vector_hit, lane)) for lane in c["lanes"]))
        if [(v.chunk_id, v.retrieval_score) for v in pool] != [
            (r["chunk_id"], r["retrieval_score"]) for r in c["pool"]
        ]:
            raise ValueError("A/B RRF rank or score differs from frozen receipt")
        for arm in c["schedule"]:
            result = await evaluate(c, arm, lambda stage: ContractProbe(stage, counts, sizes))
            if result["status"] != "ok" or len(result["visible"]) != 6:
                raise ValueError("Frozen request or delivery failed admission")
    if counts != {"selector": 40, "answer": 120}:
        raise ValueError("Incomplete paired schedule")
    names = [
        "cohort.json",
        "source-review-a.json",
        "source-review-b.json",
        "protocol.md",
        "pool-receipt.json",
        "scoring.md",
    ]
    return {
        "pools_sha256": digest(pools.read_bytes().replace(b"\r\n", b"\n")),
        "publication_sha256": {
            k: digest((OUT / k).read_bytes().replace(b"\r\n", b"\n")) for k in names
        },
        "source_review_counts": dict(Counter(r["ruling"] for r in reviews)),
        "rrf_historical_functions_ast": a,
        "rrf_matched_shared_pools": len(case_rows["cases"]),
        "snapshot_papers": case_rows["sources"],
        "snapshot_chunks": case_rows["chunks"],
        "offline_contract_calls": dict(counts),
        "largest_technical_input_token_bound": max(sizes),
        "physical_provider_requests": 0,
        "paid_quality": "NOT_RUN",
        "candidate_decision": "D_OPTIONAL",
        "paid_request_limits": {k: v[0] for k, v in LIMITS.items()},
        "peak_microcny_cap": sum(v[0] for v in LIMITS.values()) * PER_REQUEST,
    }


if __name__ == "__main__":
    import asyncio

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pools", type=Path)
    parser.add_argument("--freeze", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(preflight(args.pools))
    path = OUT / "preflight.json"
    if args.freeze:
        save(path, result)
    elif read(path) != result:
        raise ValueError("Preflight receipt differs")
    print(json.dumps(result, sort_keys=True))
