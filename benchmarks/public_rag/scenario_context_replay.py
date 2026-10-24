"""Replay cached inputs through the shared production byte selector, no inference."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

from benchmarks.public_rag.scenario_runner import budget_context, chunks, dump, load, select_context


def replay(run: Path, data_root: Path):
    meta = json.loads((run / "meta.json").read_text(encoding="utf-8"))
    cfg = meta["protocol"][meta["dataset"]]
    data = load(SimpleNamespace(dataset=meta["dataset"], split=meta["split"], data=data_root))
    key = "paper_id" if meta["dataset"] == "qasper" else "case_id"
    units, query_groups = defaultdict(list), {}
    for unit in data["corpus"]:
        units[unit[key]].append(unit)
    for q in data["queries"]:
        query_groups[q["query_id"]] = q
    matched = 0
    for line in (run / "run.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        q = query_groups[row["query_id"]]
        source = units[q[key]]
        for profile, old in row["arms"].items():
            cs = chunks(source, profile != "plain_small", cfg["chunk_chars"], cfg["overlap_chars"])
            mapping = {c["chunk_id"]: c for c in cs}
            ranked = [mapping[c["chunk_id"]] for c in old["rerank_ranking"]]
            context, budget = budget_context(
                q["query"],
                select_context(
                    ranked,
                    cs,
                    source,
                    neighbors=profile == "heading_neighbor",
                    fill=profile == "heading_fill",
                    anchor_limit=cfg["retrieval_anchor_chunks"],
                ),
                cfg["context_byte_budget"],
            )
            ids = list(dict.fromkeys(c["document_id"] for c in context))
            if ids != old["selected_ids"] or budget["evidence_bytes"] != old["evidence_bytes"]:
                raise ValueError(
                    f"shared production selector differs for {row['query_id']} {profile}"
                )
            matched += 1
    target = run / "shared-selector-replay.json"
    if target.exists():
        raise ValueError("historical replay exists")
    dump(
        target,
        {
            "status": "PASS",
            "exact_contexts_matched": matched,
            "model_calls": 0,
            "gpu_inference": False,
            "scope": "Compatibility regression on exposed results, not new confirmation; "
            "same record projection, not complete production API or answer path",
        },
    )
    print(f"Matched {matched} actual contexts through shared production selector")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--data", type=Path, required=True)
    args = p.parse_args()
    replay(args.run, args.data)


if __name__ == "__main__":
    main()
