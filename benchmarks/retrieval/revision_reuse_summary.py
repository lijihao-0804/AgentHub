"""Export aggregate measurements and verifiable Git source citations from a local receipt."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def project(raw):
    out = {k: v for k, v in raw.items() if k not in ("trials", "summary")}
    out["timing_boundary"] = (
        "Warm-model wall seconds from multipart upload through worker READY, "
        "including the parser subprocess, SQL and vector upsert acknowledgement. "
        "Excludes broker transit, snapshot creation, query time and model/fingerprint warmup."
    )
    out["execution"] = (
        "Local ASGI production knowledge router with injected workspace authorization "
        "and inline job delivery, standalone PostgreSQL and in-process Qdrant. "
        "Each arm uses a fresh workspace, knowledge base and vector collection."
    )
    out["summary"] = {k: v for k, v in raw["summary"].items() if k != "rank_agreement_by_pair"}
    out["orders"] = [t["order"] for t in raw["trials"]]
    out["documents"] = {}
    for v in ("old", "new"):
        docs = out["documents"][v] = []
        for i in range(len(raw["trials"][0]["full"]["ingestion"][v])):
            record = {}
            for arm in ("full", "cache"):
                inputs = [t[arm]["ingestion"][v][i] for t in raw["trials"]]
                counts = [{k: v for k, v in r.items() if k != "seconds"} for r in inputs]
                assert all(c == counts[0] for c in counts)
                meta = counts[0]
                record.update(
                    {k: v for k, v in meta.items() if k not in ("dense_inputs", "sparse_inputs")}
                )
                record[arm] = {
                    "dense_inputs": meta["dense_inputs"],
                    "sparse_inputs": meta["sparse_inputs"],
                    "seconds_by_trial": [r["seconds"] for r in inputs],
                }
            docs.append(record)
    out["query_checks"] = {}
    out["verified_citations"] = 0
    for key in raw["trials"][0]["full"]["queries"]:
        record = {"exact_order_by_pair": [t[key] for t in raw["summary"]["rank_agreement_by_pair"]]}
        for arm in ("full", "cache"):
            receipts = [t[arm]["queries"][key] for t in raw["trials"]]
            record[arm] = {
                "gold_applicable": receipts[0]["gold_applicable"],
                "gold_rank_by_trial": [r["gold_rank"] for r in receipts],
            }
            out["verified_citations"] += sum(len(r["ranking"]) for r in receipts)
            # One representative per question and arm, recoverable from the tracked Git blob.
            if receipts[0]["gold_rank"]:
                record[arm]["first_pair_gold_citation"] = receipts[0]["ranking"][
                    receipts[0]["gold_rank"] - 1
                ]
        out["query_checks"][key] = record
    out["observed_ranking_differences"] = []
    for i, t in enumerate(raw["trials"], 1):
        for key, f in t["full"]["queries"].items():
            c = t["cache"]["queries"][key]
            if c["ranking"] != f["ranking"]:
                out["observed_ranking_differences"].append(
                    {
                        "pair": i,
                        "query": key,
                        "full": [[h["path"], h["ordinal"]] for h in f["ranking"]],
                        "cache": [[h["path"], h["ordinal"]] for h in c["ranking"]],
                    }
                )
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = json.loads(args.receipt.read_text(encoding="utf-8"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(project(raw), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
