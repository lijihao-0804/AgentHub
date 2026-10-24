"""Quality-only fixed DBSF replay using previously scored full candidate unions."""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import asdict
from pathlib import Path
from statistics import fmean

from benchmarks.public_rag.budget_ablation import exposure
from benchmarks.public_rag.data import load_scifact
from benchmarks.public_rag.project_score_fusion import fuse_dbsf
from benchmarks.public_rag.scifact_runner import DATA, dump, retrieval_identity, settings
from benchmarks.public_rag.scoring import evaluate_questions, paired_bootstrap_comparison
from packages.knowledge.contracts import VectorSearchHit
from packages.knowledge.retrieval import fuse_reciprocal_rank


def replay(row, source_mapping):
    stages = row["stages"]
    hit_lists = {
        name: stages[name]["ranking"]
        for name in ("dense", "sparse", "fusion", "selection", "rerank")
    }
    for name, hits in hit_lists.items():
        if len(hits) > (30 if name in ("dense", "sparse") else 60):
            raise ValueError("source capture exceeds declared candidate budgets")
        if len({hit["chunk_id"] for hit in hits}) != len(hits):
            raise ValueError("duplicate chunk in source stage")
        for hit in hits:
            score = hit["score"]
            if (
                isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not math.isfinite(score)
            ):
                raise ValueError("invalid source score")
            if source_mapping.get(hit["chunk_id"]) != hit["document_id"]:
                raise ValueError("source mapping differs from prepared snapshot")
    union = {hit["chunk_id"] for name in ("dense", "sparse") for hit in hit_lists[name]}
    if any(
        {hit["chunk_id"] for hit in hit_lists[name]} != union
        for name in ("fusion", "selection", "rerank")
    ):
        raise ValueError("quality replay requires every full-union candidate to be scored")
    lanes = [
        tuple(
            VectorSearchHit(hit["chunk_id"], hit["score"], {"chunk_id": hit["chunk_id"]})
            for hit in hit_lists[name]
        )
        for name in ("dense", "sparse")
    ]
    expected = fuse_reciprocal_rank(*lanes, rrf_k=60, candidate_top_k=60)
    if [item.chunk_id for item in expected] != [hit["chunk_id"] for hit in hit_lists["fusion"]]:
        raise ValueError("cached fusion order is not the frozen production RRF")
    if any(
        abs(item.retrieval_score - hit["score"]) > 1e-12
        for item, hit in zip(expected, hit_lists["fusion"], strict=True)
    ):
        raise ValueError("cached fusion scores differ")
    scores = {hit["chunk_id"]: hit["score"] for hit in hit_lists["rerank"]}
    pools = {
        "baseline": hit_lists["fusion"][:20],
        "dbsf": fuse_dbsf([hit_lists["dense"], hit_lists["sparse"]], limit=20),
    }
    output = {}
    for arm, pool in pools.items():
        final = sorted(
            pool, key=lambda hit: (-scores[hit["chunk_id"]], -hit["score"], hit["chunk_id"])
        )
        output[arm] = {
            "query_id": row["query_id"],
            "stages": {
                "fusion": {"ranking": pool},
                "rerank": {"ranking": [dict(hit, score=scores[hit["chunk_id"]]) for hit in final]},
            },
        }
    return output


def screen(args):
    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    state = json.loads(args.state.read_text(encoding="utf-8"))
    if protocol["arms"] != ["baseline", "dbsf"] or protocol["snapshot_id"] != state["snapshot_id"]:
        raise ValueError("protocol/state mismatch")
    if protocol["controls"] != {
        "dense_top_k": 30,
        "sparse_top_k": 30,
        "candidate_top_k": 20,
        "rerank_top_k": 20,
        "final_top_k": 20,
        "baseline_rrf_k": 60,
    }:
        raise ValueError("protocol controls differ from this fixed mechanism")
    if protocol["selection_rule"] != {
        "min_ndcg10_gain": 0.005,
        "max_recall5_loss": 0.005,
        "max_recall20_loss": 0.005,
        "require_primary_ci_above_zero": True,
    }:
        raise ValueError("selection gates differ from the declared screen")
    paths = {arm: args.output / f"screen-{arm}.jsonl" for arm in protocol["arms"]}
    if (
        any(path.exists() for path in paths.values())
        or (args.output / "screen-report.json").exists()
    ):
        raise ValueError("refusing to overwrite historical screening")
    source_path = Path(protocol["screen_source"])
    meta = json.loads(source_path.with_suffix(".meta.json").read_text(encoding="utf-8"))
    expected_meta = {
        "queries": 809,
        "split": "train",
        "phase": "screen",
        "arm": "superset",
        "snapshot_id": state["snapshot_id"],
        "final_top_k": 60,
        "controls": {"candidate_top_k": 60, "rerank_top_k": 60, "max_chunks_per_document": None},
        "retrieval_identity": retrieval_identity(settings()),
    }
    if any(meta.get(key) != value for key, value in expected_meta.items()):
        raise ValueError("full-union source metadata differs")
    exposure(
        args.output, "PROJECT_DBSF_DEV_QUALITY_REPLAY_LABEL_ACCESS", protocol=str(args.protocol)
    )
    data = load_scifact(args.data, "train")
    if len(data.qrels) != 809:
        raise ValueError("screening requires all official train queries")
    source_rows = [
        json.loads(line) for line in source_path.read_text(encoding="utf-8").splitlines()
    ]
    if (
        len(source_rows) != 809
        or len({row["query_id"] for row in source_rows}) != 809
        or {row["query_id"] for row in source_rows} != set(data.qrels)
    ):
        raise ValueError("cached queries differ from complete DEV")
    if any(doc not in data.corpus for doc in state["chunk_sources"].values()):
        raise ValueError("prepared source corpus differs")
    rows = {arm: {} for arm in protocol["arms"]}
    for row in source_rows:
        for arm, replayed in replay(row, state["chunk_sources"]).items():
            rows[arm][row["query_id"]] = replayed
    evaluations, fusion_evaluations = {}, {}
    for arm, arm_rows in rows.items():

        def rank(stage, captured_rows=arm_rows):
            return {
                qid: [hit["document_id"] for hit in row["stages"][stage]["ranking"][:20]]
                for qid, row in captured_rows.items()
            }

        evaluations[arm] = evaluate_questions(data.qrels, rank("rerank"))
        fusion_evaluations[arm] = evaluate_questions(data.qrels, rank("fusion"))
    comparisons = {
        metric: asdict(
            paired_bootstrap_comparison(
                evaluations["baseline"],
                evaluations["dbsf"],
                metric=metric,
                sample_count=10000,
                confidence=0.95,
                seed=protocol["seed"],
            )
        )
        for metric in ("nDCG@10", "Recall@5", "Recall@20", "MRR@10")
    }
    metrics = {arm: dict(value.metrics) for arm, value in evaluations.items()}
    rule, primary = protocol["selection_rule"], comparisons["nDCG@10"]
    gates = {
        "min_ndcg10_gain": primary["estimate"] >= rule["min_ndcg10_gain"],
        "primary_ci_above_zero": primary["group_count"] >= 2 and primary["ci_low"] > 0,
        "max_recall5_loss": metrics["baseline"]["Recall@5"] - metrics["dbsf"]["Recall@5"]
        <= rule["max_recall5_loss"],
        "max_recall20_loss": metrics["baseline"]["Recall@20"] - metrics["dbsf"]["Recall@20"]
        <= rule["max_recall20_loss"],
    }
    selected = "dbsf" if all(gates.values()) else "baseline"
    changed = sum(
        [hit["chunk_id"] for hit in rows["baseline"][qid]["stages"]["fusion"]["ranking"]]
        != [hit["chunk_id"] for hit in rows["dbsf"][qid]["stages"]["fusion"]["ranking"]]
        for qid in data.qrels
    )
    report = {
        "protocol": protocol,
        "scope": "Exploratory cached quality screening; no latency claim",
        "queries": 809,
        "source_metadata": meta,
        "metric_projection": "First20 raw chunks, deduplicate source documents before metric ranks",
        "metrics": metrics,
        "comparisons": comparisons,
        "fusion_metrics": {arm: dict(value.metrics) for arm, value in fusion_evaluations.items()},
        "selection": {
            "selected_arm": selected,
            "gates": gates,
            "qualifies_for_actual_run": selected == "dbsf",
        },
        "fusion_changed_queries": changed,
        "mean_selected_source_documents": {
            arm: fmean(
                len(set(hit["document_id"] for hit in row["stages"]["fusion"]["ranking"]))
                for row in arm_rows.values()
            )
            for arm, arm_rows in rows.items()
        },
        "latency_evaluated": False,
        "generation_api_calls": 0,
        "production_default_changed": False,
    }
    for arm, path in paths.items():
        with path.open("x", encoding="utf-8") as handle:
            for row in rows[arm].values():
                handle.write(json.dumps(row) + "\n")
    dump(args.output / "screen-report.json", report)
    dump(
        args.output / "frozen-selection.json",
        {
            "protocol": protocol,
            "selected_arm": selected,
            "gates": gates,
            "quality_screen_only": True,
        },
    )
    print(json.dumps({"queries": 809, "selection": selected, "gates": gates}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--data", type=Path, default=DATA)
    screen(parser.parse_args())


if __name__ == "__main__":
    main()
