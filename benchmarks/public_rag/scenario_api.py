"""Single-attempt source selection over cached scenario candidates, no gold in prompts."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections import Counter, defaultdict
from pathlib import Path

import httpx

from benchmarks.public_rag.fiqa_confirmation import _credentials
from benchmarks.public_rag.scenario_metrics import evidence_metrics
from benchmarks.public_rag.scenario_runner import (
    budget_context,
    chunks,
    complete_text_ids,
    dump,
    load,
    select_context,
)


async def run(args):
    from benchmarks.public_rag.scenario_selector import select_chain_once

    if args.output.exists():
        raise ValueError("refusing replay/resume or overwrite of API execution")
    meta = json.loads((args.baseline / "meta.json").read_text(encoding="utf-8"))
    baseline = [
        json.loads(x)
        for x in (args.baseline / "run.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    completed = json.loads((args.baseline / "completed.json").read_text(encoding="utf-8"))
    if len(baseline) != meta["queries"] or len(baseline) != completed["expected"]:
        raise ValueError("complete baseline required")
    if len({row["query_id"] for row in baseline}) != len(baseline):
        raise ValueError("duplicate baseline query IDs")
    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    if protocol != meta["protocol"]:
        raise ValueError("method differs from measured baseline")
    args.dataset, args.split = meta["dataset"], meta["split"]
    cfg = protocol[args.dataset]
    args.output.mkdir(parents=True)
    dump(
        args.output / "label-access.json",
        {
            "dataset": args.dataset,
            "split": args.split,
            "purpose": "cached source selection, all source labels excluded from API payload",
        },
    )
    data = load(args)
    by_query = {q["query_id"]: q for q in data["queries"]}
    group_key = "paper_id" if args.dataset == "qasper" else "case_id"
    grouped = defaultdict(list)
    for unit in data["corpus"]:
        grouped[unit[group_key]].append(unit)
    ready = {}
    for row in baseline:
        query = by_query[row["query_id"]]
        units = grouped[query[group_key]]
        all_chunks = {
            c["chunk_id"]: c for c in chunks(units, True, cfg["chunk_chars"], cfg["overlap_chars"])
        }
        ranked = [
            {**all_chunks[c["chunk_id"]], "rerank_score": c["rerank_score"]}
            for c in row["arms"]["heading_fill"]["rerank_ranking"]
        ]
        source_ids = list(dict.fromkeys(c["document_id"] for c in ranked))
        by_source = {u["document_id"]: u for u in units}
        candidates = [
            {k: by_source[source][k] for k in ("document_id", "title", "text")}
            for source in source_ids
        ]
        ready[row["query_id"]] = query, units, ranked, candidates
    url, key = _credentials()
    counts, stop = Counter(), asyncio.Event()
    logging.getLogger("httpx").setLevel(logging.CRITICAL)
    logging.getLogger("httpcore").setLevel(logging.CRITICAL)
    dump(
        args.output / "meta.json",
        {
            **meta,
            "profile": "chain_selection",
            "profiles": list(meta["profiles"]) + ["chain_selection"],
            "baseline": args.baseline.as_posix(),
            "selection_only": True,
            "pool": "same cached BGE20 children; show their complete source paragraphs",
        },
    )
    with (
        (args.output / "run.jsonl").open("x", encoding="utf-8") as out,
        (args.output / "request-events.jsonl").open("x", encoding="utf-8") as events,
    ):
        async with httpx.AsyncClient(
            timeout=120, follow_redirects=False, limits=httpx.Limits(max_connections=4)
        ) as client:

            async def process(row):
                q, units, ranked, candidates = ready[row["query_id"]]
                events.write(json.dumps({"event": "QUERY_STARTED", "id": q["query_id"]}) + "\n")
                events.flush()
                result = await select_chain_once(client, url, key, q["query"], candidates)
                events.write(
                    json.dumps(
                        {"event": "RESPONSE_CAPTURED", "id": q["query_id"], "result": result}
                    )
                    + "\n"
                )
                events.flush()
                selected = result["selected_document_ids"] if result["status"] == "VALID" else []
                selected_set = set(selected)
                ordered = [c for source in selected for c in ranked if c["document_id"] == source]
                ordered += [c for c in ranked if c["document_id"] not in selected_set]
                context, budget = budget_context(
                    q["query"],
                    select_context(ordered, list(ordered), units, fill=True),
                    cfg["context_byte_budget"],
                )
                source_ids = list(dict.fromkeys(c["document_id"] for c in context))
                metrics = evidence_metrics(q["evidence_sets"], source_ids)
                full = evidence_metrics(q["evidence_sets"], complete_text_ids(context, units))
                metrics.update(
                    full_text_evidence_recall=full["best_evidence_recall"],
                    selected_paragraphs=len(source_ids),
                    selected_ids=source_ids,
                    **budget,
                )
                capture = {
                    **row,
                    "api": result,
                    "arms": {**row["arms"], "chain_selection": metrics},
                }
                out.write(json.dumps(capture, ensure_ascii=False) + "\n")
                out.flush()
                counts[result["status"]] += 1
                if result.get("fatal") or result["status"] == "UNKNOWN_OUTCOME":
                    stop.set()
                if counts["INVALID_OUTPUT"] >= 3:
                    stop.set()
                n = sum(counts.values())
                if n == 1 or n % 25 == 0:
                    print(f"selection completed {n}/{len(baseline)}", flush=True)
                return result

            first = await process(baseline[0])
            if first["status"] not in {"VALID", "BUDGET_FALLBACK"}:
                stop.set()
            queue = asyncio.Queue()
            for row in baseline[1:]:
                queue.put_nowait(row)

            async def worker():
                while not stop.is_set():
                    try:
                        row = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    await process(row)

            await asyncio.gather(*(worker() for _ in range(4)))
    dump(
        args.output / "completed.json",
        {
            "queries": sum(counts.values()),
            "expected": len(baseline),
            "statuses": dict(counts),
            "stopped": stop.is_set(),
            "run_state": "NEEDS_ATTENTION"
            if counts["UNKNOWN_OUTCOME"]
            else "STOPPED"
            if stop.is_set()
            else "COMPLETED",
        },
    )
    if stop.is_set() or sum(counts.values()) != len(baseline):
        raise RuntimeError("partial API capture preserved without retry; no complete-split score")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--baseline", type=Path, required=True)
    p.add_argument("--protocol", type=Path, required=True)
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
