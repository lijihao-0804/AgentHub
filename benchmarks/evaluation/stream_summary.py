"""Summarize recorded real HTTP streaming; keep cold and approval samples separate."""

import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

from benchmarks.evaluation.artifacts import write_json_atomic
from packages.core.canonical.json_hash import canonical_json_hash
from packages.evaluation.metrics import percentile


def summarize(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    if data["status"] != "EXECUTED":
        raise ValueError("STREAM_RUN_NOT_FINISHED")
    groups = {}
    for name, rows in (
        ("direct_answer_cold_first", data["cases"][:1]),
        ("direct_answer_warm", [c for c in data["cases"][1:] if c["group"] == "direct_answer"]),
        ("approval_wait", [c for c in data["cases"] if c["group"] == "approval_wait"]),
    ):
        metrics = {}
        for field in ("client_ttft_ms", "server_ttft_ms", "http_duration_ms"):
            values = [
                c.get("server_timing", {}).get(field) if field == "server_ttft_ms" else c[field]
                for c in rows
            ]
            known = [v for v in values if v is not None]
            metrics[field] = {
                "known": len(known),
                "missing": len(rows) - len(known),
                "p50": percentile(known, 0.5) if known else None,
                "p95": percentile(known, 0.95) if known else None,
            }
        groups[name] = {
            "count": len(rows),
            "statuses": dict(Counter(c["status"] for c in rows)),
            "metrics": metrics,
        }
    return {
        "version": "real-http-stream-summary-v1",
        "raw_report_hash": canonical_json_hash(data),
        "groups": groups,
        "provider_calls": len(data["calls"]),
        "upper_estimated_cny": str(
            sum((Decimal(c["upper_estimated_cost_cny"]) for c in data["calls"]), Decimal(0))
        ),
        "percentile_method": "project percentile, linear interpolation",
        "limitations": data["limitations"]
        + [
            "cold means first request in new local service process, "
            "not controlled provider cold start"
        ],
    }


if __name__ == "__main__":
    import sys

    path = Path(sys.argv[1])
    summary = summarize(path)
    write_json_atomic(path.with_suffix(".summary.json"), summary)
    print(json.dumps(summary))
