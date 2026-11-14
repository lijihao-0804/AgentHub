"""Paired historical replay of document fusion, original-block selection and real BGE."""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

from benchmarks.public_rag import candidate_recall_data as data
from benchmarks.public_rag.article_fusion_candidates import (
    select_article_candidates,
    select_local_passages,
)
from benchmarks.public_rag.candidate_recall_methods import select_candidates
from benchmarks.public_rag.candidate_recall_rationales import complete_rationale
from benchmarks.public_rag.candidate_recall_rerank import (
    MODEL_PROTOCOL,
    direct,
    rank,
    verified_inputs,
)
from benchmarks.public_rag.scifact_recall_forensics import (
    archive_records,
    sentence_ranges,
    verify_artifacts,
)
from benchmarks.public_rag.scifact_recall_index import frozen_source

OUT = data.PUBLIC.parent / "article-recall-20261114/fusion"
SCRATCH = data.ROOT / ".scratch"
WORK = SCRATCH / "article-fusion-20261114"
STUDY = SCRATCH / "candidate-recall-scifact-20261114-serialization-replay"
VECTOR = SCRATCH / "scifact-recall-forensics-20261114"
INFERENCE = SCRATCH / "candidate-recall-bge-20261114/inference.jsonl"
ARMS = ("baseline", "article_refill4", "article_first", "article_local_passage")
LAYERS = ("sources20", "sources5", "rationales20", "rationales5")


def load():
    _, cohorts, groups, old, scores, _ = verified_inputs(STUDY)
    if (
        data.sha256(INFERENCE)
        != json.loads((data.PUBLIC / "rerank-execution.json").read_bytes())[
            "private_inference_sha256"
        ]
    ):
        raise ValueError("reranker cache changed")
    for row in data.jsonl(INFERENCE):
        for cid, value in row["scores"].items():
            scores[row["query_id"]].setdefault(cid, value)
    verify_artifacts(VECTOR)
    rows = list(data.jsonl(VECTOR / "manifest.jsonl"))
    chunks = {r["chunk_id"]: r for r in rows}
    return cohorts, groups, old, scores, chunks


def write_rows(path, rows):
    with path.open("x", encoding="utf8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def prepare(work: Path = WORK):
    work.mkdir(parents=True, exist_ok=True)
    _, _, old, scores, chunks = load()
    by_doc = defaultdict(list)
    for h in chunks.values():
        by_doc[h["document_id"]].append(h)
    history = data.historical_rows(list(old))
    texts = data.queries(list(old))
    plans, missing = [], []
    for q, stages in history.items():
        lanes = [stages[k]["ranking"] for k in ("dense", "sparse")]
        pools = {"baseline": select_candidates("baseline", *lanes)}
        for arm in ARMS[1:3]:
            pools[arm] = select_article_candidates(arm, *lanes)
        pools[ARMS[3]] = select_local_passages(texts[q], *lanes, by_doc)
        if pools["baseline"] != old[q]["baseline"]:
            raise ValueError("baseline tie/order/score differs")
        for pool in pools.values():
            if len({h["chunk_id"] for h in pool}) != 20:
                raise ValueError(f"raw budget mismatch {q} {pool}")
        absent = [h for h in pools[ARMS[3]] if h["chunk_id"] not in scores[q]]
        if absent:
            missing.append({"query_id": q, "query": texts[q], "hits": absent})
        plans.append({"query_id": q, "pools": pools})
    write_rows(work / "plans.jsonl", plans)
    write_rows(work / "missing.jsonl", missing)
    print("missing", sum(len(r["hits"]) for r in missing), "in", len(missing), flush=True)


def infer(work: Path = WORK):
    from packages.knowledge.adapters.reranker import BgeReranker

    model_spec = json.loads(MODEL_PROTOCOL.read_bytes())["model"]
    model_dir = (
        data.ROOT.parent
        / "hf_cache/models--BAAI--bge-reranker-v2-m3/snapshots"
        / model_spec["revision"]
    )
    if any(data.sha256(model_dir / n) != h for n, h in model_spec["file_sha256"].items()):
        raise ValueError("reranker model changed")
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    model = BgeReranker(model_name=str(model_dir), device="cuda", batch_size=8, max_length=8192)
    model.warm()
    if model._device != "cuda" or str(next(model._model.parameters()).dtype) != "torch.float16":
        raise ValueError("GPU FP16 required by protocol")
    chunks = {
        r["chunk_id"]: (r["document_id"], r["text"]) for r in data.jsonl(VECTOR / "manifest.jsonl")
    }
    write_rows(
        work / "scores.jsonl",
        (
            {"query_id": r["query_id"], "logits": direct(model, r["query"], r["hits"], chunks)}
            for r in data.jsonl(work / "missing.jsonl")
        ),
    )


def summarize(rows, groups):
    from benchmarks.public_rag.candidate_recall_protected import paired_delta

    group_list = sorted({groups[r["query_id"]] for r in rows})
    grouping = np.array([group_list.index(groups[r["query_id"]]) for r in rows])
    draws = np.random.default_rng(20261114).integers(len(group_list), size=(10000, len(group_list)))
    result = {}
    for arm in ARMS:
        counts = {}
        for index, layer in enumerate(LAYERS):
            c = Counter()
            macro = []
            for row in rows:
                old, new = (set(row[a]["layers"][index]) for a in ("baseline", arm))
                for metric, values in (
                    ("baseline", old),
                    ("candidate", new),
                    ("gained", new - old),
                    ("lost", old - new),
                    ("retained", old & new),
                ):
                    c[metric] += len(values)
                c["any_old"] += bool(old)
                c["any_new"] += bool(new)
                c["any_gained"] += bool(new) and not old
                c["any_lost"] += bool(old) and not new
                denom = len(row["gold" if index < 2 else "annotated"])
                if denom:
                    macro.append(len(new) / denom)
            counts[layer] = {**c, "macro_recall": sum(macro) / len(macro) if macro else None}
            if index < 2:
                baseline = [len(r["baseline"]["layers"][index]) / len(r["gold"]) for r in rows]
                counts[layer]["paired"] = paired_delta(baseline, macro, draws, grouping)
        counts["outside_both_lanes"] = sum(r[arm]["outside_lanes"] for r in rows)
        counts["old_best_source_block_dropped"] = sum(
            r[arm]["old_best_source_block_dropped"] for r in rows
        )
        result[arm] = counts
    return {
        "questions": len(rows),
        "groups": len({groups[r["query_id"]] for r in rows}),
        "gold_pairs": sum(len(r["gold"]) for r in rows),
        "annotated_pairs": sum(len(r["annotated"]) for r in rows),
        "arms": result,
    }


def report(work: Path, output: Path, scores_path: Path | None = None):
    output.mkdir(parents=True, exist_ok=True)
    cohorts, groups, old, scores, chunks = load()
    source = frozen_source(json.loads(MODEL_PROTOCOL.read_bytes()))
    claims, documents = archive_records(SCRATCH / "scifact-rationale-source-20261114/data.tar.gz")
    history = data.historical_rows(list(old))
    control = {r["query"]: r["baseline"] for r in data.jsonl(data.PUBLIC / "portable/hits.jsonl")}
    plans = {r["query_id"]: r["pools"] for r in data.jsonl(work / "plans.jsonl")}
    scores_path = scores_path or work / "scores.jsonl"
    for row in data.jsonl(scores_path):
        scores[row["query_id"]].update(row["logits"])
    rows = {}
    for q, pools in plans.items():
        gold = sorted(d for d, grade in source.qrels[q].items() if grade > 0)
        annotated = {d: e for d, e in claims[q]["evidence"].items() if e}
        spans = {d: sentence_ranges(source.corpus[d], documents[d]) for d in annotated}
        union = {h["chunk_id"] for k in ("dense", "sparse") for h in history[q][k]["ranking"]}
        record = {"query_id": q, "gold": gold, "annotated": sorted(annotated)}
        old_best = {}
        for arm in ARMS:
            pool = pools[arm]
            ranked = rank(pool, scores[q])
            best = {}
            for i, h in enumerate(ranked, 1):
                d, c = h["document_id"], h["chunk_id"]
                if d in gold and d not in best:
                    block = chunks[c]
                    best[d] = {
                        "chunk_id": c,
                        "ordinal": block["ordinal"],
                        "start": block["char_start"],
                        "end": block["char_end"],
                        "logit": scores[q][c],
                        "rerank_rank": i,
                        "pool_rank": 1 + next(j for j, x in enumerate(pool) if x["chunk_id"] == c),
                    }
            layers = []
            rationale_sets = []
            for view in (pool, ranked[:5]):
                layers.append(sorted({h["document_id"] for h in view}.intersection(gold)))
                visible = []
                for d, evidence in annotated.items():
                    windows = [
                        (chunks[h["chunk_id"]]["char_start"], chunks[h["chunk_id"]]["char_end"])
                        for h in view
                        if h["document_id"] == d
                    ]
                    if complete_rationale(evidence, spans[d], windows):
                        visible.append(d)
                rationale_sets.append(sorted(visible))
            layers.extend(rationale_sets)
            if arm == "baseline":
                if layers != control[q]:
                    raise ValueError(f"portable original metric mismatch: {q}")
                old_best = {d: b["chunk_id"] for d, b in best.items()}
            selected = {h["chunk_id"] for h in pool}
            record[arm] = {
                "layers": layers,
                "best": best,
                "outside_lanes": len(selected - union),
                "old_best_source_block_dropped": len(set(old_best.values()) - selected),
            }
        rows[q] = record
    misses = sorted(
        (q for q in cohorts["development"] if not rows[q]["baseline"]["layers"][0]), key=int
    )
    if len(misses) != 13:
        raise ValueError("unexpected baseline misses")
    write_rows(
        output / "per-query.jsonl",
        (rows[q] for k in ("development", "confirmation") for q in cohorts[k]),
    )
    examples = []
    for q in misses:
        r = rows[q]
        examples.append(
            {
                "claim": claims[q]["claim"],
                **r,
                "source_titles": {d: source.corpus[d].title for d in r["gold"]},
            }
        )
    data.save(output / "prior-misses.json", examples)
    examples = []
    for index in (2, 3):
        for direction in ("gained", "lost"):
            for q in cohorts["confirmation"]:
                record = rows[q]
                before, after = (set(record[a]["layers"][index]) for a in ARMS[2:])
                changed = sorted(after - before if direction == "gained" else before - after)
                if not changed:
                    continue
                d = changed[0]
                positions = sentence_ranges(source.corpus[d], documents[d])
                details = {}
                for arm in ARMS[2:]:
                    pool = plans[q][arm]
                    view = pool if index == 2 else rank(pool, scores[q])[:5]
                    details[arm] = [
                        {
                            k: chunks[h["chunk_id"]][k]
                            for k in (
                                "chunk_id",
                                "ordinal",
                                "char_start",
                                "char_end",
                                "text_sha256",
                            )
                        }
                        for h in view
                        if h["document_id"] == d
                    ]
                examples.append(
                    {
                        "query": q,
                        "claim": claims[q]["claim"],
                        "article": d,
                        "title": source.corpus[d].title,
                        "layer": LAYERS[index],
                        "change": direction,
                        "official_alternatives": [
                            {
                                "label": e["label"],
                                "sentences": [
                                    {
                                        "index": i,
                                        "text": documents[d]["abstract"][i],
                                        "span": positions[i],
                                    }
                                    for i in e["sentences"]
                                ],
                            }
                            for e in claims[q]["evidence"][d]
                        ],
                        "selected_source_blocks": details,
                    }
                )
                break
    data.save(output / "passage-examples.json", examples)
    report = {
        "protocol_sha256": data.sha256(OUT / "protocol.json"),
        "per_query_sha256": data.sha256(output / "per-query.jsonl"),
        "fresh_bge_scores_sha256": data.sha256(scores_path),
        "fresh_bge_pairs": sum(len(r["logits"]) for r in data.jsonl(scores_path)),
        "baseline_portable_four_layer_exact": len(rows),
        "plans_sha256": data.sha256(work / "plans.jsonl"),
        "code_sha256": {
            f: data.sha256(Path(__file__).with_name(f))
            for f in ("article_fusion_study.py", "article_fusion_candidates.py")
        },
        "cohorts": {
            name: summarize([rows[q] for q in ids], groups)
            for name, ids in (
                ("development", cohorts["development"]),
                ("historical_external_by_group", cohorts["confirmation"]),
                ("prior_misses", misses),
            )
        },
    }
    data.save(output / "report.json", report)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("prepare", "infer", "report"):
        command = commands.add_parser(action)
        command.add_argument("--work", type=Path, default=WORK)
        if action == "report":
            command.add_argument("--output", type=Path, required=True)
            command.add_argument("--scores", type=Path, help="Existing real BGE scores.jsonl")
    args = parser.parse_args()
    if args.action == "report":
        report(args.work, args.output, args.scores)
    else:
        {"prepare": prepare, "infer": infer}[args.action](args.work)
