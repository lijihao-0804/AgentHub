"""P-C1 gate runner: decompose, fuse, measure hop-stratified recall@5.

Stage C1-4 of the plan: zero retrieval-model compute, one decomposition call
per question. Writes every artifact into the run directory; the verdict field
states the pre-registered gate outcome.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
from collections import defaultdict
from pathlib import Path

import httpx

from benchmarks.public_rag.query_decompose import (
    BM25Index,
    decompose_once,
    rrf_fuse_n,
)
from benchmarks.public_rag.scenario_metrics import evidence_metrics

HERE = Path(__file__).parent
RUN_DIR = HERE / "results" / "query-decompose-20261024"
BASELINE = HERE / "results" / "consumer-rag-20261023" / "confirmation-candidates-1"
DATA = Path("data/external/rag-public-20261021/musique/extracted/data")
MODEL = "deepseek-v4.1-flash-1017"
CUT = 5
BOOTSTRAP_SAMPLES = 5_000
SEED = 20261024
GATES = {"hop2_floor_pp": -1.0, "hop3_min_pp": 5.0, "hop4_min_pp": 5.0}


def _gateway_config() -> tuple[str, str]:
    base_url = os.environ.get("AGENTHUB_RAG_BASE_URL", "").strip("\"'")
    api_key = os.environ.get("AGENTHUB_RAG_API_KEY", "").strip("\"'")
    if not base_url or not api_key:
        for line in Path(".env").read_text(encoding="utf-8").splitlines():
            if line.startswith("AGENTHUB_RAG_BASE_URL="):
                base_url = line.split("=", 1)[1].strip().strip("\"'")
            elif line.startswith("AGENTHUB_RAG_API_KEY="):
                api_key = line.split("=", 1)[1].strip().strip("\"'")
    return base_url, api_key


def _load():
    from argparse import Namespace

    from benchmarks.public_rag.scenario_runner import load

    meta = json.loads((BASELINE / "meta.json").read_text(encoding="utf-8"))
    rows = [
        json.loads(line)
        for line in (BASELINE / "run.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    data = load(
        Namespace(dataset=meta["dataset"], split=meta["split"], data=DATA)
    )
    queries = {q["query_id"]: q for q in data["queries"]}
    unit_groups: dict[str, list] = defaultdict(list)
    for unit in data["corpus"]:
        unit_groups[unit["case_id"]].append(unit)
    return meta, rows, queries, unit_groups


async def _decompose_all(rows, queries) -> None:
    target = RUN_DIR / "decompositions.jsonl"
    if target.exists():
        print(f"reusing {target}")
        return
    base_url, api_key = _gateway_config()
    ledger = {"requests": 0, "valid": 0, "fallback": 0, "usage": {}}
    async with httpx.AsyncClient(timeout=45.0) as client:
        with target.open("x", encoding="utf-8") as out:
            for row in rows:
                question = queries[row["query_id"]]["query"]
                result = await decompose_once(
                    client, base_url=base_url, api_key=api_key, model=MODEL, question=question
                )
                ledger["requests"] += 1
                ledger["valid" if result["status"] == "VALID" else "fallback"] += 1
                usage = result.get("usage")
                if isinstance(usage, dict):
                    for key, value in usage.items():
                        ledger["usage"][key] = ledger["usage"].get(key, 0) + value
                out.write(
                    json.dumps(
                        {"query_id": row["query_id"], **result}, ensure_ascii=False
                    )
                    + "\n"
                )
    (RUN_DIR / "decompose-ledger.json").write_text(
        json.dumps(ledger, indent=2), encoding="utf-8"
    )
    print(f"decomposed {ledger['requests']} questions: {ledger}")


def _recall(evidence_sets, unit_ids):
    metrics = evidence_metrics(evidence_sets, unit_ids)
    return metrics["best_evidence_recall"] if metrics.get("evaluable", True) else None


def _bootstrap_deltas(items: list[tuple[str, float]], *, samples: int, seed: int):
    """Percentile CI on the mean delta, resampling question groups."""
    by_group: dict[str, list[float]] = defaultdict(list)
    for group, delta in items:
        by_group[group].append(delta)
    groups = sorted(by_group)
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(samples):
        draw = [groups[rng.randrange(len(groups))] for _ in groups]
        values = [delta for group in draw for delta in by_group[group]]
        means.append(sum(values) / len(values))
    means.sort()
    low = means[int(0.025 * len(means))]
    high = means[int(0.975 * len(means)) - 1]
    point = sum(delta for _, delta in items) / len(items)
    return point, low, high


def gate() -> None:
    meta, rows, queries, unit_groups = _load()
    decompositions = {}
    for line in (RUN_DIR / "decompositions.jsonl").read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        decompositions[entry["query_id"]] = entry

    per_question = []
    hop_items: dict[int, list[tuple[str, float]]] = defaultdict(list)
    with (RUN_DIR / "per-question.jsonl").open("x", encoding="utf-8") as out:
        for row in rows:
            query = queries[row["query_id"]]
            units = unit_groups[query["case_id"]]
            index = BM25Index([unit["text"] for unit in units])
            ids = [unit["document_id"] for unit in units]

            baseline_ids = [ids[i] for i in index.ranking(query["query"])[:CUT]]
            decomposition = decompositions[row["query_id"]]
            sub_rankings = [
                index.ranking(sub) for sub in decomposition["sub_queries"]
            ]
            fused_ids = [ids[i] for i in rrf_fuse_n(sub_rankings)[:CUT]]

            baseline_recall = _recall(query["evidence_sets"], baseline_ids)
            fused_recall = _recall(query["evidence_sets"], fused_ids)
            record = {
                "query_id": row["query_id"],
                "group_id": row["group_id"],
                "hops": row.get("hops"),
                "decompose_status": decomposition["status"],
                "sub_queries": decomposition["sub_queries"],
                "baseline_recall": baseline_recall,
                "fused_recall": fused_recall,
            }
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            per_question.append(record)
            if baseline_recall is not None and fused_recall is not None:
                hop_items[record["hops"]].append(
                    (record["group_id"], fused_recall - baseline_recall)
                )

    stratified = {}
    for hop, items in sorted(hop_items.items()):
        point, low, high = _bootstrap_deltas(
            items, samples=BOOTSTRAP_SAMPLES, seed=SEED + hop
        )
        stratified[f"hop{hop}"] = {
            "n": len(items),
            "delta_pp": round(point * 100, 2),
            "ci95_pp": [round(low * 100, 2), round(high * 100, 2)],
        }

    hop3, hop4 = stratified.get("hop3"), stratified.get("hop4")
    hop2 = stratified.get("hop2")
    gate3 = (
        hop3 is not None
        and hop3["delta_pp"] >= GATES["hop3_min_pp"]
        and hop3["ci95_pp"][0] > 0
    )
    gate4 = (
        hop4 is not None
        and hop4["delta_pp"] >= GATES["hop4_min_pp"]
        and hop4["ci95_pp"][0] > 0
    )
    guard2 = hop2 is None or hop2["delta_pp"] >= GATES["hop2_floor_pp"]
    verdict = "PASS" if gate3 and gate4 and guard2 else "FAIL"

    summary = {
        "questions": len(per_question),
        "cut": CUT,
        "bootstrap_samples": BOOTSTRAP_SAMPLES,
        "stratified": stratified,
        "gates": GATES,
        "verdict": verdict,
        "next_step": (
            "answer A/B (C1-5) with fused rankings injected into the frozen funnel"
            if verdict == "PASS"
            else "stop the project; negative result; no answer-generation spend"
        ),
    }
    (RUN_DIR / "gate.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--decompose", action="store_true", help="run the gateway phase")
    args = parser.parse_args()
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    meta, rows, queries, _ = _load()
    del meta
    if args.decompose:
        asyncio.run(_decompose_all(rows, queries))
    gate()


if __name__ == "__main__":
    main()
