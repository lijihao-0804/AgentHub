"""Offline replay of whole-source evidence admission after real BGE reranking."""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from benchmarks.public_rag import candidate_recall_data as data
from benchmarks.public_rag.candidate_recall_rationales import complete_rationale
from benchmarks.public_rag.candidate_recall_rerank import MODEL_PROTOCOL, rank, verified_inputs
from benchmarks.public_rag.scifact_recall_forensics import archive_records, sentence_ranges
from benchmarks.public_rag.scifact_recall_index import chunk_manifest, frozen_source
from packages.agent_runtime.context_budget import ApproxTokenEstimator
from packages.knowledge.chunking import normalize_chunk_text

HERE = Path(__file__).resolve().parent
ESTIMATOR = ApproxTokenEstimator()


def insert_span(spans, new_span):
    """Coalesce touching intervals of this document at their earliest ranking position."""
    doc, left, right = new_span
    overlaps = [i for i, (d, a, b) in enumerate(spans) if d == doc and a <= right and left <= b]
    if not overlaps:
        return [*spans, new_span]
    first = overlaps[0]
    union = (
        doc,
        min([left] + [spans[i][1] for i in overlaps]),
        max([right] + [spans[i][2] for i in overlaps]),
    )
    return [
        union if i == first else span
        for i, span in enumerate(spans)
        if i == first or i not in overlaps
    ]


def pack(windows, texts, budget, *, compact):
    kept = []
    tokens = 0
    for span in windows:
        proposed = insert_span(kept, span) if compact else [*kept, span]
        cost = sum(ESTIMATOR.estimate(texts[d][a:b]) for d, a, b in proposed)
        if cost <= budget:
            kept, tokens = proposed, cost
    return kept, tokens


def score(view, evidence, positions, positives):
    visible = {
        doc
        for doc, groups in evidence.items()
        if complete_rationale(groups, positions[doc], [(a, b) for d, a, b in view if d == doc])
    }
    return visible, positives & {d for d, _, _ in view}


def run(output: Path):
    config = json.loads((HERE / "protocol.json").read_text(encoding="utf-8"))
    study = data.ROOT / ".scratch/candidate-recall-scifact-20261114"
    archive = data.ROOT / ".scratch/scifact-rationale-source-20261114/data.tar.gz"
    _, cohorts, groups, candidates, cache, _ = verified_inputs(study)
    qids = cohorts[config["cohort"]]
    if len(qids) != config["registered_questions"]:
        raise ValueError("wrong frozen confirmation cohort")
    model = json.loads(MODEL_PROTOCOL.read_bytes())
    source = frozen_source(model)
    claims, documents = archive_records(archive)
    chunks = {h["chunk_id"]: h for h in chunk_manifest(source, data.SNAPSHOT, model)}
    texts = {d: normalize_chunk_text(v.retrieval_text) for d, v in source.corpus.items()}
    metrics = {arm: Counter() for arm in ("baseline", "compact")}
    rows = []
    denominators = Counter()
    stages = Counter()
    query_gains = query_losses = 0
    for qid in qids:
        pool = candidates[qid][config["candidate_arm"]]
        ranked = rank(pool, cache[qid])[: config["reranked_cutoff"]]
        windows = []
        for hit in ranked:
            chunk = chunks[hit["chunk_id"]]
            d, a, b = chunk["document_id"], chunk["char_start"], chunk["char_end"]
            # Historical text_range locators include whitespace trimmed by the chunker.
            raw = texts[d][a:b]
            a += len(raw) - len(raw.lstrip())
            b -= len(raw) - len(raw.rstrip())
            if hit["document_id"] != d or texts[d][a:b] != chunk["text"]:
                raise ValueError("chunk ID, source, and exact character ranges must agree")
            windows.append((d, a, b))
        evidence = {d: g for d, g in claims[qid]["evidence"].items() if g}
        positions = {d: sentence_ranges(source.corpus[d], documents[d]) for d in evidence}
        positives = {d for d, value in source.qrels[qid].items() if value > 0}
        if not evidence.keys() <= positives:
            raise ValueError("rationale-source label mismatch")
        top5_visible, top5_sources = score(windows, evidence, positions, positives)
        stages.update(
            fused20_positive_sources=len(positives & {hit["document_id"] for hit in pool}),
            reranked5_positive_sources=len(top5_sources),
            reranked5_complete_rationale_pairs=len(top5_visible),
        )
        denominators.update(
            queries=1,
            annotated_queries=bool(evidence),
            annotated_pairs=len(evidence),
            positive_pairs=len(positives),
        )
        outcomes = {}
        for arm in metrics:
            view, cost = pack(
                windows, texts, config["approx_token_budget"], compact=arm == "compact"
            )
            visible, delivered = score(view, evidence, positions, positives)
            # Sensitivity: a reader may reconstruct an entire sentence across
            # overlapping chunks already in its prompt, even without compaction.
            union = []
            for span in view:
                union = insert_span(union, span)
            covered, _ = score(union, evidence, positions, positives)
            metrics[arm].update(
                complete_rationale_pairs=len(visible),
                rationale_pairs_with_joint_source_coverage=len(covered),
                queries_with_rationale=bool(visible),
                positive_sources=len(delivered),
                evidence_tokens=cost,
                windows=len(view),
            )
            outcomes[arm] = (visible, delivered, view, cost, covered)
        bv, bd, bw, bc, bu = outcomes["baseline"]
        cv, cd, cw, cc, cu = outcomes["compact"]
        query_gains += bool(cv - bv)
        query_losses += bool(bv - cv)
        row = {
            "query_id": qid,
            "source_component": groups[qid],
            "baseline_complete_sources": sorted(bv),
            "compact_complete_sources": sorted(cv),
            "rationale_gains": sorted(cv - bv),
            "rationale_losses": sorted(bv - cv),
            "joint_coverage_gains": sorted(cu - bu),
            "joint_coverage_losses": sorted(bu - cu),
            "source_gains": sorted(cd - bd),
            "source_losses": sorted(bd - cd),
            "baseline_tokens": bc,
            "compact_tokens": cc,
        }
        if bw != cw:
            row.update(baseline_spans=bw, compact_spans=cw)
        rows.append(row)
    report = {
        "protocol_sha256": hashlib.sha256(
            (HERE / "protocol.json").read_text(encoding="utf-8").encode("utf-8")
        ).hexdigest(),
        "upstream_protocol_sha256": data.sha256(data.PROTOCOL),
        "model_protocol_sha256": data.sha256(MODEL_PROTOCOL),
        "official_source_sha256": data.sha256(archive),
        "ordered_ids_sha256": data.fingerprint(qids),
        "source_components": len({groups[q] for q in qids}),
        "denominators": dict(denominators),
        "stages_before_budget": stages,
        "metrics": metrics,
        "queries_with_changed_spans": sum("compact_spans" in row for row in rows),
        "queries_gaining_rationale": query_gains,
        "queries_losing_rationale": query_losses,
    }
    output.mkdir(exist_ok=True, parents=True)
    with (output / "pairs.jsonl").open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, separators=(",", ":")) + "\n")
    report["pairs_sha256"] = data.sha256(output / "pairs.jsonl")
    (output / "summary.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(json.dumps(run(parser.parse_args().output), indent=2))
