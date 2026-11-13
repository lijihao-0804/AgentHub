"""Score visibility of complete AllenAI TRAIN rationale sets in actual BGE top-five chunks."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from benchmarks.public_rag import candidate_recall_data as data
from benchmarks.public_rag.candidate_recall_rerank import (
    ARMS,
    MODEL_PROTOCOL,
    rank,
    verified_inputs,
)
from benchmarks.public_rag.scifact_recall_forensics import archive_records, sentence_ranges
from benchmarks.public_rag.scifact_recall_index import chunk_manifest, frozen_source


def complete_rationale(groups, positions, windows) -> bool:
    """One alternative suffices; every full sentence must be visible in a selected block."""
    for group in groups:
        sentences = group["sentences"]
        if not sentences or any(not 0 <= i < len(positions) for i in sentences):
            raise ValueError("invalid official positive rationale")
        if all(
            any(start <= positions[i][0] and positions[i][1] <= end for start, end in windows)
            for i in sentences
        ):
            return True
    return False


def run(study: Path, inference: Path, archive: Path) -> dict:
    _, cohorts, _, candidates, cache, _ = verified_inputs(study)
    report = json.loads((inference / "report.json").read_bytes())
    path = inference / "inference.jsonl"
    if data.sha256(path) != report["inference_file_sha256"]:
        raise ValueError("direct scores changed")
    records = {q: {} for q in candidates}
    for record in data.jsonl(path):
        q, src = record["query_id"], record["score_source"]
        if src in records[q]:
            raise ValueError("duplicate direct inference record")
        records[q][src] = record["scores"]
    model_protocol = json.loads(MODEL_PROTOCOL.read_bytes())
    source = frozen_source(model_protocol)
    chunks = {h["chunk_id"]: h for h in chunk_manifest(source, data.SNAPSHOT, model_protocol)}
    claims, docs = archive_records(archive)
    results = {}
    for phase in ("development", "confirmation"):
        counts = {a: Counter() for a in ARMS}
        pairs = {a: {"top5": Counter(), "pool": Counter()} for a in ARMS[1:]}
        examples = {}
        for q in cohorts[phase]:
            claim = claims[q]
            gold = {doc for doc, g in source.qrels[q].items() if g > 0}
            supplied = {d: e for d, e in claim["evidence"].items() if e}
            if not supplied.keys() <= gold:
                raise ValueError("official rationale refers to a source outside BEIR positives")
            positions = {doc: sentence_ranges(source.corpus[doc], docs[doc]) for doc in supplied}
            per_arm = {}
            for arm in ARMS:
                readings = records[q]
                if phase == "confirmation" and arm in report["full_direct_followup_confirmation"]:
                    scores = (
                        readings[arm + "_complete"]
                        if arm + "_complete" in readings
                        else readings[arm]
                    )
                else:
                    scores = {
                        **readings.get("lexical_refill4", {}),
                        **readings.get("out_of_union", {}),
                        **cache[q],
                    }
                ranked = rank(candidates[q][arm], scores)
                states = {}
                for stage, view in (("top5", ranked[:5]), ("pool", ranked)):
                    visible = set()
                    for doc, groups in supplied.items():
                        windows = [
                            (chunks[h["chunk_id"]]["char_start"], chunks[h["chunk_id"]]["char_end"])
                            for h in view
                            if h["document_id"] == doc
                        ]
                        if complete_rationale(groups, positions[doc], windows):
                            visible.add(doc)
                    states[stage] = visible
                    counts[arm][stage + "_covered_rationale_pairs"] += len(visible)
                    counts[arm][stage + "_queries_with_any_complete_set"] += bool(visible)
                counts[arm]["registered_queries"] += 1
                counts[arm]["annotated_queries"] += bool(supplied)
                counts[arm]["annotated_query_document_pairs"] += len(supplied)
                counts[arm]["positive_pairs_without_rationale"] += len(gold) - len(supplied)
                per_arm[arm] = states
            for arm in ARMS[1:]:
                for stage in ("pool", "top5"):
                    old, new = per_arm["baseline"][stage], per_arm[arm][stage]
                    pairs[arm][stage].update(
                        retained=len(old & new),
                        gained=len(new - old),
                        lost=len(old - new),
                        missing_both=len(supplied.keys() - (old | new)),
                    )
                    for name, docs_changed in (("gain", new - old), ("loss", old - new)):
                        if docs_changed:
                            examples.setdefault(
                                arm + "_" + stage + "_" + name,
                                {"query_id": q, "document_id": sorted(docs_changed)[0]},
                            )
        results[phase] = {"counts": counts, "paired": pairs, "first_in_frozen_order": examples}
    return {
        "kind": "descriptive_complete_sentence_annotation_audit",
        "implementation_sha256": data.sha256(Path(__file__)),
        "direct_inference_sha256": report["inference_file_sha256"],
        "official_archive_sha256": data.sha256(archive),
        "definition": (
            "Restricted to official TRAIN query/source pairs with annotated sentence sets. "
            "An alternative set is complete if each entire required sentence fits within a "
            "returned chunk of that source; different sentences may be in different chunks. "
            "Unannotated source pairs are counted separately."
        ),
        "cohorts": results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("study", "inference", "archive", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    data.save(args.output, run(args.study, args.inference, args.archive))


if __name__ == "__main__":
    main()
