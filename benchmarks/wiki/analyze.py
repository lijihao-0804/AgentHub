"""Summarize a saved DEV run without new model requests or changing observations."""

import argparse
import json
from pathlib import Path
from statistics import mean


def summarize(data):
    case_by_id = {c["id"]: c for c in data["cases"]["cases"]}
    rows = data["results"]
    calls = data["calls"]
    compile_count = len(data["compile"]["telemetry"]["calls"])
    compile_count += data["compile"]["telemetry"].get("conflict_review", {}).get("request_count", 0)
    cursor = compile_count
    grouped = {name: [] for name in ("WIKI", "RAG", "HYBRID")}
    for row in rows:
        row_calls = []
        # Requests are sequential. Invalid responses still have a gateway call;
        # deterministic no-evidence refusal has telemetry=None and consumes none.
        if row.get("error_code") or row.get("answer", {}).get("telemetry") is not None:
            row_calls = [calls[cursor]]
            cursor += 1
        grouped[row["mode"]].append((row, row_calls))
    assert cursor + data["incremental"]["new_gateway_calls"] == len(calls)
    result = {
        "schema_version": 1,
        "scope": "NON_FORMAL_DEV",
        "build_sha": data["build_sha"],
        "dataset_hash": data["dataset_hash"],
        "cases": len(case_by_id),
        "attempted_mode_cases": len(rows),
        "gateway_requests": len(calls),
        "pricing": "UNKNOWN",
        "actual_cost": None,
        "actual_cost_status": "UNKNOWN",
        "usage_known_requests": sum(c.get("usage") is not None for c in calls),
        "compile": data["compile"],
        "embedding": data["embedding"],
        "incremental": data["incremental"],
        "page_count": len(data["pages"]),
        "claim_count": sum(len(p["claims"]) for p in data["pages"]),
        "navigation_edges": sum(len(p["relations"]) for p in data["pages"]),
        "compile_lint": data["lint"],
        "modes": {},
        "failures": [],
    }
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        result[key] = (
            sum(c["usage"][key] for c in calls)
            if result["usage_known_requests"] == len(calls)
            else None
        )
    for mode, items in grouped.items():
        used = [call for _, values in items for call in values]
        completed = [row for row, _ in items if "error_code" not in row]
        agreement = sum(
            (not row["answer"]["insufficient_evidence"]) == case_by_id[row["case_id"]]["answerable"]
            for row in completed
        )
        result["modes"][mode] = {
            "attempts": len(items),
            "valid_outputs": len(completed),
            "answerability_flag_agreement": agreement,
            "flag_agreement_denominator": len(items),
            "gateway_requests": len(used),
            "input_tokens": sum(c["usage"]["input_tokens"] for c in used),
            "output_tokens": sum(c["usage"]["output_tokens"] for c in used),
            "total_tokens": sum(c["usage"]["total_tokens"] for c in used),
            "mean_end_to_end_latency_ms": mean(row["latency_ms"] for row, _ in items),
            "actual_cost": None,
            "cost_status": "UNKNOWN",
        }
        result["failures"].extend(
            {"case_id": row["case_id"], "mode": mode, "error_code": row["error_code"]}
            for row, _ in items
            if "error_code" in row
        )
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = summarize(json.loads(args.input.read_text(encoding="utf-8")))
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                k: result[k]
                for k in (
                    "gateway_requests",
                    "total_tokens",
                    "page_count",
                    "claim_count",
                    "navigation_edges",
                    "modes",
                )
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
