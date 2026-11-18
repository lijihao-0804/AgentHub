"""Compare the FiQA first-stage pool and the relevant sources fitting the QA message."""

import argparse
import asyncio
import hashlib
import json
import statistics
import time
from pathlib import Path

from benchmarks.public_rag.candidate_recall_rerank import MODEL_PROTOCOL, direct, rank
from benchmarks.public_rag.data import load_beir
from benchmarks.public_rag.fiqa_confirmation import _chunks
from benchmarks.public_rag.reranker_batch_benchmark import checked_file
from benchmarks.public_rag.scifact_recall_forensics import vector_hit
from benchmarks.public_rag.scifact_runner import offline_models
from packages.knowledge.adapters.reranker import BgeReranker
from packages.knowledge.citation_qa import _evidence_prompt, _select_evidence
from packages.knowledge.contracts import RetrievedEvidence
from packages.knowledge.retrieval import fuse_reciprocal_rank

ROOT = Path(__file__).resolve().parents[4]
DATA = ROOT / "data/external/rag-fiqa-20261022/extracted/fiqa"
STATE = ROOT / ".scratch/rag-fiqa-20261022/fiqa-state.json"
ARCHIVE = ROOT / "benchmarks/public_rag/results/fiqa-confirmation-20261022/baseline.jsonl"
STAGES = ("candidate", "six", "message_16000", "message_512")
ARMS = ("original20", "expanded40", "protected5")


def fingerprint(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def summarize(rows):
    arms = {}
    differences = {}
    for arm in ARMS:
        totals = {}
        paired = {}
        for stage in STAGES:
            hits = [row[arm][stage] for row in rows]
            totals[stage] = {
                "macro_recall": statistics.fmean(
                    len(h) / len(r["gold"]) for h, r in zip(hits, rows, strict=True)
                ),
                "positive_pairs": sum(map(len, hits)),
                "hit_questions": sum(bool(h) for h in hits),
            }
            gained, lost = {}, {}
            for row, observed in zip(rows, hits, strict=True):
                before = set(row["original20"][stage])
                if added := sorted(set(observed) - before):
                    gained[row["id"]] = added
                if removed := sorted(before - set(observed)):
                    lost[row["id"]] = removed
            paired[stage] = {"gained": gained, "lost": lost}
        arms[arm], differences[arm] = totals, paired
    return {
        "queries": len(rows),
        "gold_source_pairs": sum(len(r["gold"]) for r in rows),
        "arms": arms,
        "paired": differences,
    }


def evaluate(pool, top, gold, query, records):
    def relevant(hits):
        return sorted(gold & {h["document_id"] for h in hits})

    seen = {"candidate": relevant(pool), "six": relevant(top)}
    evidence = tuple(
        RetrievedEvidence(
            h["document_id"],
            str(row.document_revision_id),
            row.chunk_id,
            "fiqa:" + h["document_id"],
            row.locator,
            row.text,
            h["score"],
            None,
            {},
        )
        for h in top
        for row in [records[h["chunk_id"]]]
    )
    for budget in (16000, 512):
        selected = _select_evidence(evidence, max_chars=budget)
        prompt = _evidence_prompt(query, selected)
        if any(f"\ntext:\n{item.text}" not in prompt for item in selected):
            raise ValueError("Serialized message lost a selected passage")
        seen[f"message_{budget}"] = sorted(gold & {item.evidence.document_id for item in selected})
    return seen


async def run(output):
    offline_models()
    inputs = {"rankings": fingerprint(ARCHIVE), "qrels": fingerprint(DATA / "qrels/test.tsv")}
    if inputs != {
        "rankings": "190eea5eade1af85bda24c892dfef119fde5c94a151f13ec9681b6e3509280f1",
        "qrels": "6adc2a640dcdd22bb8b3858f89107adef2a7c3db20a63550dfa7a0f71e379e44",
    }:
        raise ValueError("Frozen FiQA inputs differ")
    data = load_beir(DATA, "test")
    state = json.loads(STATE.read_bytes())
    records = await _chunks(state, data.corpus)
    chunks = {cid: (state["chunk_sources"][cid], row.text) for cid, row in records.items()}
    questions = [json.loads(line) for line in ARCHIVE.read_bytes().splitlines()]
    if len(questions) != 648 or {q["query_id"] for q in questions} != set(data.queries):
        raise ValueError("Unexpected test population")
    spec = json.loads(MODEL_PROTOCOL.read_bytes())["model"]
    weights = ROOT.parent / "hf_cache/models--BAAI--bge-reranker-v2-m3/snapshots" / spec["revision"]
    for name in spec["file_sha256"]:
        checked_file(weights, name, spec["file_sha256"])
    model = BgeReranker(
        model_name=str(weights), device="cuda", batch_size=8, max_length=spec["max_length"]
    )
    model.warm()
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    rows, scored_pairs, archived_order_matches = [], 0, 0
    with (output / "pairs.jsonl").open("x", encoding="utf8") as pairs:
        for row in questions:
            qid, stages = row["query_id"], row["stages"]
            lanes = [
                tuple(vector_hit(h["chunk_id"]) for h in stages[k]["ranking"])
                for k in ("dense", "sparse")
            ]
            pool = [
                {
                    "chunk_id": h.chunk_id,
                    "document_id": chunks[h.chunk_id][0],
                    "score": h.retrieval_score,
                }
                for h in fuse_reciprocal_rank(*lanes, rrf_k=60, candidate_top_k=40)
            ]
            if [h["chunk_id"] for h in pool[:20]] != [
                h["chunk_id"] for h in stages["fusion"]["ranking"]
            ]:
                raise ValueError(f"Fused top 20 differs for query {qid}")
            # Score the first 20 separately to preserve their original last batch's shape.
            scores = direct(model, data.queries[qid], pool[:20], chunks)
            if pool[20:]:
                scores.update(direct(model, data.queries[qid], pool[20:], chunks))
            ranked = rank(pool, scores)
            old = rank(pool[:20], scores)[:6]
            archived_order_matches += [h["chunk_id"] for h in old] == [
                h["chunk_id"] for h in stages["rerank"]["ranking"][:6]
            ]
            protected = old[:5]
            protected_ids = {h["chunk_id"] for h in protected}
            sixth = next(h for h in ranked if h["chunk_id"] not in protected_ids)
            gold = {d for d, grade in data.qrels[qid].items() if grade > 0}
            obs = {"id": qid, "gold": sorted(gold)}
            for arm, candidates, top in zip(
                ARMS, (pool[:20], pool, pool), (old, ranked[:6], protected + [sixth]), strict=True
            ):
                obs[arm] = evaluate(candidates, top, gold, data.queries[qid], records)
            scored_pairs += len(pool)
            pairs.write(json.dumps(obs, separators=(",", ":")) + "\n")
            rows.append(obs)
    report = {
        **summarize(rows),
        "input_sha256": inputs,
        "reranker_revision": spec["revision"],
        "real_query_chunk_logits": scored_pairs,
        "six_matching_archived_order": archived_order_matches,
        "scoring_seconds": round(time.monotonic() - started, 3),
        "provider_calls": 0,
    }
    (output / "summary.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args().output))
