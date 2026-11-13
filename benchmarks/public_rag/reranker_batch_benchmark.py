"""Paired offline GPU replay of the frozen SciFact TRAIN reranker workload."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import platform
import subprocess
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from random import Random
from statistics import fmean
from uuid import UUID

import numpy as np

from benchmarks.public_rag.data import load_scifact, query_groups
from benchmarks.public_rag.experimental.length_batched_reranker import BgeReranker
from benchmarks.public_rag.scoring import evaluate_questions
from packages.knowledge.chunking import build_deterministic_chunks
from packages.knowledge.contracts import RerankCandidate
from packages.knowledge.parser import ParsedBlock, ParsedDocument

ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = Path(__file__).parent / "results/published/reranker-batching-20261114/protocol.json"
CANDIDATE = Path(__file__).parent / "experimental/length_batched_reranker.py"
CANDIDATE_BLOB = "2a7e04fdf47855e19e8965c4f382a07750c6d409e21d93e05b1aff640c169d62"
ARMS = ("baseline", "candidate")


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def checked_file(folder: Path, name: str, expected: dict) -> Path:
    path = folder / name
    if sha256(path) != expected[name]:
        raise ValueError(f"frozen input differs: {name}")
    return path


def records(path: Path):
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            yield json.loads(line)


def save(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")


@dataclass(frozen=True)
class Case:
    question: str
    candidates: tuple[RerankCandidate, ...]
    documents: tuple[str, ...]
    fusion_scores: tuple[float, ...]

    def ranked_indices(self, scores: list[float]) -> list[int]:
        if len(scores) != len(self.candidates) or not all(math.isfinite(v) for v in scores):
            raise ValueError("invalid number or value of adapter scores")
        return sorted(
            range(len(scores)),
            key=lambda i: (-scores[i], -self.fusion_scores[i], self.candidates[i].chunk_id),
        )


def load_cases(protocol: dict):
    data = ROOT / protocol["data_directory"]
    history = ROOT / protocol["historical_directory"]
    frozen = protocol["input_sha256"]
    for name in ("corpus.jsonl", "queries.jsonl", "qrels/train.tsv"):
        checked_file(data, name, frozen)
    meta = json.loads(checked_file(history, "dev-baseline.meta.json", frozen).read_bytes())
    if (meta["split"], meta["snapshot_id"]) != (protocol["split"], protocol["snapshot_id"]):
        raise ValueError("historical source is not the frozen TRAIN snapshot")
    source = load_scifact(data, "train")
    raw = {}
    for row in records(checked_file(history, "dev-baseline.jsonl", frozen)):
        qid = row["query_id"]
        if qid in raw:
            raise ValueError(f"duplicate query ID: {qid}")
        raw[qid] = row["stages"]["selection"]["ranking"]
    if (
        len(raw) != protocol["source_query_count"]
        or set(raw) != set(source.queries)
        or len(source.corpus) != protocol["corpus_documents"]
    ):
        raise ValueError("candidate capture and official TRAIN inventory differ")
    sample = protocol["sample"]
    ids = sorted(raw, key=lambda q: hashlib.sha256(f"{sample['salt']}:{q}".encode()).digest())
    measured = ids[: sample["measured"]]
    warmup = ids[sample["measured"] : sample["measured"] + sample["warmup"]]
    selected = [*measured, *warmup]
    documents = {hit["document_id"] for q in selected for hit in raw[q]}
    revisions = {}
    for pair in records(checked_file(history, "pair-token-audit.jsonl", frozen)):
        doc = pair["source_document_id"]
        if pair["arm"] == "parent" and doc in documents:
            if revisions.setdefault(doc, pair["pair_id"]) != pair["pair_id"]:
                raise ValueError(f"conflicting revision identities for {doc}")
    if revisions.keys() != documents:
        raise ValueError("missing source revision for a frozen candidate")
    chunks = {}
    for doc, revision in revisions.items():
        parsed = ParsedDocument(
            (ParsedBlock(source.corpus[doc].retrieval_text, {"type": "text_range"}),)
        )
        for chunk in build_deterministic_chunks(
            UUID(revision), parsed, chunk_size_chars=1200, chunk_overlap_chars=200
        ):
            chunks[(doc, chunk.chunk_id)] = RerankCandidate(chunk.chunk_id, chunk.text)
    cases = {}
    for q in selected:
        hits = raw[q]
        chunk_ids = {hit["chunk_id"] for hit in hits}
        if len(hits) != sample["candidates_per_query"] or len(chunk_ids) != len(hits):
            raise ValueError(f"unexpected candidate count or duplicate for {q}")
        cases[q] = Case(
            source.queries[q],
            tuple(chunks[(h["document_id"], h["chunk_id"])] for h in hits),
            tuple(h["document_id"] for h in hits),
            tuple(h["score"] for h in hits),
        )
    return source.qrels, cases, measured, warmup


def baseline_method(protocol: dict, directory: Path) -> Callable:
    source = subprocess.check_output(
        ["git", "show", f"{protocol['baseline_commit']}:{protocol['adapter_path']}"], cwd=ROOT
    )
    if hashlib.sha256(source).hexdigest() != protocol["baseline_blob_sha256"]:
        raise ValueError("baseline adapter differs from the frozen blob")
    path = directory / "baseline_adapter.py"
    path.write_bytes(source)
    spec = importlib.util.spec_from_file_location("frozen_scifact_baseline_adapter", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.BgeReranker.rerank


def input_shapes(torch, adapter, method, case: Case) -> tuple[list[dict], Counter]:
    """Observe the actual forward inputs in a separate untimed pass."""
    batches = []
    signatures = Counter()

    def capture(module, args, kwargs):
        mask = kwargs["attention_mask"].detach().cpu().numpy().astype(bool)
        ids = kwargs["input_ids"].detach().cpu().numpy()
        batches.append({"pairs": mask.shape[0], "width": mask.shape[1], "real": int(mask.sum())})
        for row, valid in zip(ids, mask, strict=True):
            signatures[hashlib.sha256(row[valid].tobytes()).hexdigest()] += 1

    handle = adapter._model.register_forward_pre_hook(capture, with_kwargs=True)
    try:
        method(adapter, case.question, case.candidates)
        torch.cuda.synchronize()
    finally:
        handle.remove()
    return batches, signatures


def summarize(protocol: dict, cases: dict, full_qrels: dict, rows: list, audits: dict) -> dict:
    qids = [row["id"] for row in rows]
    qrels = {q: full_qrels[q] for q in qids}
    grouped = query_groups(full_qrels)
    groups = sorted({grouped[q] for q in qids})
    group_idx = np.array([groups.index(grouped[q]) for q in qids])
    settings = protocol["bootstrap"]
    draws = np.random.default_rng(settings["seed"]).integers(
        len(groups), size=(settings["replicates"], len(groups))
    )
    group_sizes = np.bincount(group_idx)[draws].sum(axis=1)

    def paired(left, right):
        delta = np.array(right) - np.array(left)
        totals = np.bincount(group_idx, weights=delta)
        lo, hi = np.quantile(totals[draws].sum(axis=1) / group_sizes, [0.025, 0.975])
        return {
            "baseline": fmean(left),
            "candidate": fmean(right),
            "delta": float(delta.mean()),
            "ci95": [float(lo), float(hi)],
        }

    ranks = {}
    metrics = {}
    times = {arm: [r[arm]["ms"] for r in rows] for arm in ARMS}
    for arm in ARMS:
        ranks[arm] = {
            q: cases[q].ranked_indices(row[arm]["scores"])
            for q, row in zip(qids, rows, strict=True)
        }
        doc_ranks = {q: [cases[q].documents[i] for i in ranks[arm][q]] for q in qids}
        all_docs = evaluate_questions(qrels, doc_ranks)
        top_five = evaluate_questions(qrels, {q: docs[:5] for q, docs in doc_ranks.items()})
        if all_docs.evaluated_count != len(qids):
            raise ValueError("missing positive judgments in registered sample")
        metrics[arm] = {
            "document_ndcg10": [all_docs.questions[q].metrics["nDCG@10"] for q in qids],
            "document_recall5": [all_docs.questions[q].metrics["Recall@5"] for q in qids],
            "document_mrr5": [all_docs.questions[q].metrics["MRR@5"] for q in qids],
            "raw5_source_recall": [top_five.questions[q].gold_coverage for q in qids],
            "pool_source_recall": [all_docs.questions[q].gold_coverage for q in qids],
        }
    quality = {
        m: paired(metrics["baseline"][m], metrics["candidate"][m]) for m in metrics["baseline"]
    }
    latency = paired(times["baseline"], times["candidate"])
    latency["percentiles_ms"] = {
        arm: {f"p{k}": float(np.percentile(times[arm], k)) for k in (50, 95)} for arm in ARMS
    }
    latency["relative_mean_reduction"] = 1 - latency["candidate"] / latency["baseline"]
    latency["mean_delta_ms_by_first_arm"] = {
        a: fmean(
            row["candidate"]["ms"] - row["baseline"]["ms"] for row in rows if row["first"] == a
        )
        for a in ARMS
    }
    identity = {
        str(k): {
            "same_order": sum(ranks["baseline"][q][:k] == ranks["candidate"][q][:k] for q in qids),
            "same_set": sum(
                set(ranks["baseline"][q][:k]) == set(ranks["candidate"][q][:k]) for q in qids
            ),
        }
        for k in (1, 5, 10, 20)
    }
    drifts = [
        abs(a - b)
        for row in rows
        for a, b in zip(row["baseline"]["scores"], row["candidate"]["scores"], strict=True)
    ]
    counts = {}
    for arm in ARMS:
        shapes = [b for q in qids for b in audits[q][arm]]
        counts[arm] = {
            "forward_batches": len(shapes),
            "pairs": sum(b["pairs"] for b in shapes),
            "nonpadding_tokens": sum(b["real"] for b in shapes),
            "padded_tokens": sum(b["pairs"] * b["width"] for b in shapes),
        }
    gates = protocol["gates"]
    n = len(qids)
    checks = {
        "complete_pairs": n == gates["required_complete_pairs"] == len(set(qids)) == len(audits),
        "same_input_tokens": all(audits[q]["identical_inputs"] for q in qids)
        and counts["baseline"]["nonpadding_tokens"] == counts["candidate"]["nonpadding_tokens"]
        and counts["baseline"]["pairs"]
        == counts["candidate"]["pairs"]
        == n * protocol["sample"]["candidates_per_query"],
        "mean_latency": latency["relative_mean_reduction"] >= gates["min_mean_latency_reduction"],
        "latency_interval": latency["ci95"][1] < 0,
        "p95_latency": latency["percentiles_ms"]["candidate"]["p95"]
        <= gates["max_p95_latency_ratio"] * latency["percentiles_ms"]["baseline"]["p95"],
        "ndcg10": quality["document_ndcg10"]["delta"] >= gates["min_ndcg10_difference"]
        and quality["document_ndcg10"]["ci95"][0] >= gates["min_quality_difference_ci_lower"],
        "raw5_source_recall": quality["raw5_source_recall"]["delta"]
        >= gates["min_consumer_recall5_difference"]
        and quality["raw5_source_recall"]["ci95"][0] >= gates["min_quality_difference_ci_lower"],
        "top1": identity["1"]["same_order"] / n >= gates["min_top1_chunk_identity"],
        "top5_set": identity["5"]["same_set"] / n >= gates["min_top5_chunk_set_identity"],
        "score_drift": max(drifts) <= gates["max_absolute_logit_difference"],
    }
    return {
        "decision": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "queries": n,
        "first_in_pair_counts": dict(Counter(row["first"] for row in rows)),
        "bootstrap_groups": len(groups),
        "quality": quality,
        "adapter_latency_ms": latency,
        "chunk_rank_identity_counts": identity,
        "max_abs_logit_drift": max(drifts),
        "changed_logits": sum(v != 0 for v in drifts),
        "tensor_counts": counts,
        "pool_missing_all_judged_sources": metrics["baseline"]["pool_source_recall"].count(0),
        "raw5_no_judged_source": {a: metrics[a]["raw5_source_recall"].count(0) for a in ARMS},
    }


def run(args, protocol):
    import torch
    import transformers

    directory = args.output.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    candidate_source = CANDIDATE.read_bytes()
    (directory / "candidate_adapter.py").write_bytes(candidate_source)
    old = baseline_method(protocol, directory)
    candidate_hash = hashlib.sha256(candidate_source.replace(b"\r\n", b"\n")).hexdigest()
    if candidate_hash != CANDIDATE_BLOB:
        raise ValueError("benchmark candidate differs from the measured adapter")
    model_config = protocol["model"]
    model_path = args.model_dir or (
        ROOT.parent
        / "hf_cache"
        / "models--BAAI--bge-reranker-v2-m3"
        / "snapshots"
        / model_config["revision"]
    )
    for name in model_config["file_sha256"]:
        checked_file(model_path, name, model_config["file_sha256"])
    qrels, cases, measured, warmup = load_cases(protocol)
    methods = {"baseline": old, "candidate": BgeReranker.rerank}
    orders = ["baseline", "candidate"] * (len(measured) // 2)
    Random(protocol["order_seed"]).shuffle(orders)
    info = {
        "protocol_sha256": sha256(PROTOCOL),
        "runner_sha256": sha256(Path(__file__)),
        "baseline_commit": protocol["baseline_commit"],
        "baseline_blob_sha256": protocol["baseline_blob_sha256"],
        "candidate_blob_sha256_lf": candidate_hash,
        "model_snapshot": model_config["revision"],
        "evaluated_at_utc": datetime.now(UTC).isoformat(),
        "python": platform.python_version(),
        "torch": torch.__version__,
        "cuda": torch.version.cuda,
        "transformers": transformers.__version__,
        "gpu": torch.cuda.get_device_name(0),
        "model_load_ms": None,
    }
    save(directory / "cohort.json", {"measured": measured, "warmup": warmup, "first_arms": orders})
    adapter = BgeReranker(
        model_name=str(model_path),
        device="cuda",
        batch_size=model_config["batch_size"],
        max_length=model_config["max_length"],
    )
    started = time.perf_counter()
    adapter.warm()
    torch.cuda.synchronize()
    info["model_load_ms"] = (time.perf_counter() - started) * 1000
    if str(next(adapter._model.parameters()).dtype) != model_config["dtype"]:
        raise ValueError("unexpected model precision")
    for q in warmup:
        for method in methods.values():
            method(adapter, cases[q].question, cases[q].candidates)
    rows = []
    with (directory / "pairs.jsonl").open("x", encoding="utf-8") as stream:
        for q, first in zip(measured, orders, strict=True):
            case = cases[q]
            row = {"id": q, "first": first}
            for arm in (first, next(a for a in ARMS if a != first)):
                torch.cuda.synchronize()
                started = time.perf_counter()
                scores = methods[arm](adapter, case.question, case.candidates)
                torch.cuda.synchronize()
                row[arm] = {"ms": (time.perf_counter() - started) * 1000, "scores": list(scores)}
                case.ranked_indices(row[arm]["scores"])
            rows.append(row)
            stream.write(json.dumps(row, allow_nan=False) + "\n")
            stream.flush()
    audits = {}
    with (directory / "tensor-audit.jsonl").open("x", encoding="utf-8") as stream:
        for q in measured:
            shapes_a, ids_a = input_shapes(torch, adapter, old, cases[q])
            shapes_b, ids_b = input_shapes(torch, adapter, methods["candidate"], cases[q])
            audits[q] = {
                "baseline": shapes_a,
                "candidate": shapes_b,
                "identical_inputs": ids_a == ids_b,
            }
            stream.write(json.dumps({"id": q, **audits[q]}) + "\n")
    result = summarize(protocol, cases, qrels, rows, audits)
    result["environment"] = info
    result["local_record_sha256"] = {
        n: sha256(directory / n) for n in ("cohort.json", "pairs.jsonl", "tensor-audit.jsonl")
    }
    save(directory / "report.json", result)
    print(
        json.dumps(
            {"decision": result["decision"], "measured": len(rows), "output": str(directory)}
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, required=True, help="New directory for local records"
    )
    parser.add_argument("--model-dir", type=Path, help="Local snapshot of the frozen model")
    args = parser.parse_args()
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", TOKENIZERS_PARALLELISM="false")
    run(args, json.loads(PROTOCOL.read_bytes()))


if __name__ == "__main__":
    main()
