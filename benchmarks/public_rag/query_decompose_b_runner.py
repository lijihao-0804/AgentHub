"""C1b stage-1 gate runner: quota aggregation vs frozen baseline (zero API)."""

from __future__ import annotations

import json
import random
from collections import defaultdict
from pathlib import Path

from benchmarks.public_rag.query_decompose import BM25Index
from benchmarks.public_rag.query_decompose_b import quota_top_k
from benchmarks.public_rag.scenario_metrics import evidence_metrics

HERE = Path(__file__).parent
C1_RUN = HERE / "results" / "query-decompose-20261024"
RUN = HERE / "results" / "query-decompose-b-20261025"
BASELINE = HERE / "results" / "consumer-rag-20261023" / "confirmation-candidates-1"
DATA = Path("data/external/rag-public-20261021/musique/extracted/data")
CUT = 5
BOOTSTRAP_SAMPLES = 5_000
SEED = 20261024
GATES = {"hop2_floor_pp": -1.0, "hop3_min_pp": 5.0, "hop4_min_pp": 5.0}
REFERENCE_BASELINES = {3: 0.508, 4: 0.444}


def _load():
    from argparse import Namespace

    from benchmarks.public_rag.scenario_runner import load

    meta = json.loads((BASELINE / "meta.json").read_text(encoding="utf-8"))
    rows = [
        json.loads(line)
        for line in (BASELINE / "run.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    data = load(Namespace(dataset=meta["dataset"], split=meta["split"], data=DATA))
    queries = {q["query_id"]: q for q in data["queries"]}
    unit_groups: dict[str, list] = defaultdict(list)
    for unit in data["corpus"]:
        unit_groups[unit["case_id"]].append(unit)
    decompositions = {}
    for line in (C1_RUN / "decompositions.jsonl").read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        decompositions[entry["query_id"]] = entry
    return rows, queries, unit_groups, decompositions


def _recall(evidence_sets, ids):
    metrics = evidence_metrics(evidence_sets, ids)
    return metrics["best_evidence_recall"] if metrics.get("evaluable", True) else None


def _bootstrap_deltas(items, *, samples, seed):
    by_group: dict[str, list[float]] = defaultdict(list)
    for group, delta in items:
        by_group[group].append(delta)
    groups = sorted(by_group)
    rng = random.Random(seed)
    means = []
    for _ in range(samples):
        draw = [groups[rng.randrange(len(groups))] for _ in groups]
        values = [delta for group in draw for delta in by_group[group]]
        means.append(sum(values) / len(values))
    means.sort()
    point = sum(delta for _, delta in items) / len(items)
    return point, means[int(0.025 * len(means))], means[int(0.975 * len(means)) - 1]


def main() -> None:
    RUN.mkdir(parents=True, exist_ok=True)
    rows, queries, unit_groups, decompositions = _load()
    hop_items: dict[int, list[tuple[str, float]]] = defaultdict(list)
    hop_means: dict[int, dict[str, float]] = defaultdict(dict)
    with (RUN / "per-question.jsonl").open("x", encoding="utf-8") as out:
        for row in rows:
            query = queries[row["query_id"]]
            units = unit_groups[query["case_id"]]
            index = BM25Index([unit["text"] for unit in units])
            ids = [unit["document_id"] for unit in units]

            baseline_ids = [ids[i] for i in index.ranking(query["query"])[:CUT]]
            sub_rankings = [
                index.ranking(sub) for sub in decompositions[row["query_id"]]["sub_queries"]
            ]
            quota_ids = [ids[i] for i in quota_top_k(sub_rankings, k=CUT)]

            baseline_recall = _recall(query["evidence_sets"], baseline_ids)
            quota_recall = _recall(query["evidence_sets"], quota_ids)
            out.write(
                json.dumps(
                    {
                        "query_id": row["query_id"],
                        "group_id": row["group_id"],
                        "hops": row.get("hops"),
                        "sub_query_count": len(sub_rankings),
                        "baseline_ids": baseline_ids,
                        "quota_ids": quota_ids,
                        "baseline_recall": baseline_recall,
                        "quota_recall": quota_recall,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            if baseline_recall is not None and quota_recall is not None:
                hop = row["hops"]
                hop_items[hop].append((row["group_id"], quota_recall - baseline_recall))
                hop_means[hop]["baseline"] = hop_means[hop].get("baseline", 0.0) + baseline_recall
                hop_means[hop]["quota"] = hop_means[hop].get("quota", 0.0) + quota_recall
                hop_means[hop]["n"] = hop_means[hop].get("n", 0) + 1

    stratified = {}
    for hop, items in sorted(hop_items.items()):
        point, low, high = _bootstrap_deltas(items, samples=BOOTSTRAP_SAMPLES, seed=SEED + hop)
        count = hop_means[hop]["n"]
        stratified[f"hop{hop}"] = {
            "n": count,
            "baseline_recall": round(hop_means[hop]["baseline"] / count, 4),
            "quota_recall": round(hop_means[hop]["quota"] / count, 4),
            "delta_pp": round(point * 100, 2),
            "ci95_pp": [round(low * 100, 2), round(high * 100, 2)],
        }

    hop3, hop4, hop2 = stratified.get("hop3"), stratified.get("hop4"), stratified.get("hop2")
    consistency = all(
        abs(stratified[f"hop{hop}"]["baseline_recall"] - reference) < 0.02
        for hop, reference in REFERENCE_BASELINES.items()
        if f"hop{hop}" in stratified
    )
    gate3 = hop3 and hop3["delta_pp"] >= 5.0 and hop3["ci95_pp"][0] > 0
    gate4 = hop4 and hop4["delta_pp"] >= 5.0 and hop4["ci95_pp"][0] > 0
    guard2 = hop2 is None or hop2["delta_pp"] >= -1.0
    verdict = "PASS" if (gate3 and gate4 and guard2 and consistency) else "FAIL"

    summary = {
        "questions": len(rows),
        "cut": CUT,
        "stratified": stratified,
        "gates": GATES,
        "baseline_consistency_with_plan": consistency,
        "verdict": verdict,
        "next_step": (
            "stage 2: complementary-set aggregation (~200 calls)"
            if verdict == "PASS"
            else "C1b closed at stage 1; mechanical quotas do not realize the union ceiling"
        ),
    }
    (RUN / "gate1.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
