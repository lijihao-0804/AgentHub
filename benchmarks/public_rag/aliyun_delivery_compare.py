"""Single-attempt Aliyun regression of frozen before/current consumer messages.

Uses existing ContextBudgetPolicy replay captures, not live retrieval or a
platform Formal Experiment. No retries, fallback, new sampling or old-file edits.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import subprocess
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import httpx
import numpy as np

from benchmarks.public_rag.consumer_answer_client import (
    EXPERIMENT_MODEL,
    _observed_usage,
    _parse_answer,
    _strict_json_loads,
)
from benchmarks.public_rag.consumer_answer_metrics import answer_metrics, citation_metrics
from benchmarks.public_rag.scenario_metrics import paired_group_ci
from packages.model_gateway.configured_route import load_route

ROOT = Path("benchmarks/public_rag/results/aliyun-delivery-20261024")
BEFORE = Path(".scratch/rag-fix-verify/replay-before/prepared.jsonl")
CURRENT = ROOT / "current-replay-1/prepared.jsonl"
SEED = 20261024
ARMS = ("before", "current")


def write(path, data):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def append(path, data):
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(data, ensure_ascii=False) + "\n")
        stream.flush()


def read_rows(path):
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    index = {r["query_id"]: r for r in rows}
    if len(index) != len(rows):
        raise ValueError("duplicate question identity")
    return index


def paired_inputs(before, current):
    if set(before) != set(current) or len(before) != 200:
        raise ValueError("require all original 200 questions, without intersection selection")
    for q in before:
        if any(before[q][k] != current[q][k] for k in ("group_id", "hops", "answers")):
            raise ValueError("question metadata/reference mismatch")
        if any(not data[q]["answerable"] for data in (before, current)):
            raise ValueError("this replay is answerable-only")
        left, right = (data[q]["arms"]["legacy5"] for data in (before, current))
        if left["messages"][:4] != right["messages"][:4]:
            raise ValueError("task prompt/question differ")
        if not left["admission_verified"] or not right["admission_verified"]:
            raise ValueError("unverified context admission")
    return before, current


def endpoint():
    route = load_route(Path("benchmarks/public_rag/model-route.json"))
    e = next(e for e in route.fallbacks if e.name == "aliyun-rag")
    if e.model != EXPERIMENT_MODEL or ".maas.aliyuncs.com/" not in e.base_url:
        raise ValueError("expected existing Aliyun DeepSeek route")
    return e


def freeze():
    before, current = paired_inputs(read_rows(BEFORE), read_rows(CURRENT))
    e = endpoint()
    build = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    protocol = {
        "declared_at_utc": datetime.now(UTC).isoformat(),
        "build_commit": build,
        "baseline_commit": "42215c8",
        "current_product_commit": "96587c3",
        "scope": "Exposed 200-question MuSiQue regression; cached ranks through actual "
        "consumer code and real generation; not fresh confirmation or full production E2E.",
        "inputs": {"before": BEFORE.as_posix(), "current": CURRENT.as_posix()},
        "route": {"name": e.name, "base_url": e.base_url, "model": e.model},
        "request": {
            "model": e.model,
            "temperature": 0,
            "max_tokens": 256,
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
        },
        "thinking_note": "Requested disabled, not an independently verified mode isolation.",
        "planned_questions": 200,
        "planned_requests": 400,
        "concurrency": 4,
        "timeout_seconds": 90,
        "attempts_per_request": 1,
        "fallback": False,
        "ordering": "Seeded shuffle of all400 paired jobs; no answer-based selection",
        "metrics": ["official_alias_token_f1", "exact_match", "citation_ID_validity_only"],
        "failure_policy": "Retain every planned row; failed outputs zero for delivery-quality "
        "endpoint, not factual error. UNKNOWN stops new dispatch and withholds primary CI; "
        "no retry. Await in-flight requests. Identity/auth/rate-limit errors stop dispatch.",
        "ci": {
            "method": "paired source-overlap cluster bootstrap",
            "samples": 5000,
            "confidence": 0.95,
            "seed": SEED,
            "groups": len({r["group_id"] for r in before.values()}),
        },
        "regression_gate": {
            "min_f1_gain": 0.03,
            "ci_lower_gt": 0,
            "min_valid_per_arm": 0.95,
            "unknown": 0,
        },
        "gate_scope": "Local regression evidence only; never new production promotion",
        "pricing_snapshot": None,
    }
    frozen = {
        q: {
            "group_id": before[q]["group_id"],
            "hops": before[q]["hops"],
            "answers": before[q]["answers"],
            "arms": {
                a: data[q]["arms"]["legacy5"]
                for a, data in zip(ARMS, (before, current), strict=True)
            },
        }
        for q in sorted(before)
    }
    write(ROOT / "protocol.json", protocol)
    write(ROOT / "frozen-inputs.json", frozen)
    print(
        json.dumps({"registered": 200, "requests": 400, "endpoint": e.base_url, "model": e.model})
    )


def classify(body):
    result = {"status": "INVALID_OUTPUT", "usage": None}
    try:
        value = _strict_json_loads(body)
        result.update(usage=_observed_usage(value.get("usage")), actual_model=value.get("model"))
        if value.get("model") != EXPERIMENT_MODEL:
            return {**result, "status": "MODEL_IDENTITY_ERROR"}
        choice = value["choices"][0]
        result["finish_reason"] = choice.get("finish_reason")
        if choice.get("finish_reason") != "stop" or choice["message"].get("tool_calls"):
            return result
        result.update(_parse_answer(choice["message"]["content"]), status="VALID")
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        pass
    return result


def report(inputs, results):
    rows, arm_reports = [], {}
    for q, case in inputs.items():
        arms = {}
        for a in ARMS:
            r = results.get((q, a), {"status": "NOT_DISPATCHED"})
            scores = (
                answer_metrics(r["answer"], case["answers"])
                if r["status"] == "VALID"
                else {"token_f1": 0.0, "exact_match": 0}
            )
            arms[a] = {**r, **scores}
            if r["status"] == "VALID":
                arms[a]["citation"] = citation_metrics(
                    r["cited_chunk_ids"], case["arms"][a]["admitted_chunk_ids"]
                )
        rows.append(
            {"query_id": q, "group_id": case["group_id"], "hops": case["hops"], "arms": arms}
        )
    complete = len(results) == 2 * len(inputs)
    unknown = sum(r["status"] == "UNKNOWN_OUTCOME" for r in results.values())
    identity_errors = sum(r["status"] == "MODEL_IDENTITY_ERROR" for r in results.values())
    for a in ARMS:
        values = [r["arms"][a] for r in rows]
        known = [r for r in values if r.get("request_ms") is not None]
        usage = {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            counts = [(r.get("usage") or {}).get(key) for r in values]
            usage[key] = {
                "known_total": sum(v for v in counts if v is not None),
                "unknown_count": sum(v is None for v in counts),
            }
        arm_reports[a] = {
            "registered": len(values),
            "status_counts": dict(Counter(r["status"] for r in values)),
            "token_f1_failure_zero": sum(r["token_f1"] for r in values) / len(values),
            "exact_match_failure_zero": sum(r["exact_match"] for r in values) / len(values),
            "valid_fraction": sum(r["status"] == "VALID" for r in values) / len(values),
            "valid_refusals": sum(bool(r.get("unanswerable")) for r in values),
            "usage": usage,
            "latency_ms": {
                "observed": len(known),
                **{
                    f"p{p}": float(np.percentile([r["request_ms"] for r in known], p))
                    if known
                    else None
                    for p in (50, 95)
                },
            },
        }
    eligible = complete and not unknown and not identity_errors
    ci = (
        {m: paired_group_ci(rows, m, *ARMS, seed=SEED) for m in ("token_f1", "exact_match")}
        if eligible
        else None
    )
    passed = bool(
        ci
        and ci["token_f1"]["difference"] >= 0.03
        and ci["token_f1"]["ci_low"] > 0
        and min(r["valid_fraction"] for r in arm_reports.values()) >= 0.95
    )
    return {
        "state": "COMPLETE" if complete else "STOPPED_INCOMPLETE",
        "arms": arm_reports,
        "unknown": unknown,
        "identity_errors": identity_errors,
        "primary_ci_eligible": eligible,
        "paired_cluster_ci": ci,
        "local_regression_gate_passed": passed,
        "promotion": False,
        "pricing_snapshot": None,
        "monetary_cost": None,
        "limitations": [
            "Already exposed question-local English MuSiQue Ans regression",
            "F1/EM are lexical; citation validity is not entailment",
            "Failures zero measure delivered quality, not factual accuracy",
            "Requested thinking off is not independently verified",
            "No live index/database, platform Formal Experiment or fresh holdout",
        ],
        "cases": rows,
    }


async def execute():
    p = json.loads((ROOT / "protocol.json").read_text(encoding="utf-8"))
    inputs = json.loads((ROOT / "frozen-inputs.json").read_text(encoding="utf-8"))
    if (
        subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        != p["build_commit"]
    ):
        raise ValueError("build differs from frozen registration")
    e = endpoint()
    if {"name": e.name, "base_url": e.base_url, "model": e.model} != p["route"]:
        raise ValueError("route differs from registration")
    out = ROOT / "execution-1"
    out.mkdir(exist_ok=False)
    jobs = [(q, a) for q in sorted(inputs) for a in ARMS]
    random.Random(SEED).shuffle(jobs)
    results, stop = {}, asyncio.Event()
    queue = asyncio.Queue()
    for job in jobs:
        queue.put_nowait(job)
    async with httpx.AsyncClient(timeout=p["timeout_seconds"], follow_redirects=False) as client:

        async def worker():
            while not stop.is_set() and not queue.empty():
                q, a = queue.get_nowait()
                payload = {**p["request"], "messages": inputs[q]["arms"][a]["messages"]}
                append(
                    out / "events.jsonl",
                    {
                        "query_id": q,
                        "arm": a,
                        "event": "DISPATCHED",
                        "at_utc": datetime.now(UTC).isoformat(),
                    },
                )
                started = time.perf_counter()
                try:
                    response = await client.post(
                        e.base_url.rstrip("/") + "/chat/completions",
                        headers={"Authorization": "Bearer " + e.api_key},
                        json=payload,
                    )
                    if response.is_success:
                        r = classify(response.content)
                        append(
                            out / "responses.jsonl",
                            {"query_id": q, "arm": a, "body": response.text},
                        )
                    else:
                        r = {"status": "PROVIDER_ERROR", "http_status": response.status_code}
                        if response.status_code in {401, 403, 429}:
                            stop.set()
                except httpx.RequestError as error:
                    r = {"status": "UNKNOWN_OUTCOME", "error_type": type(error).__name__}
                r["request_ms"] = (time.perf_counter() - started) * 1000
                if r["status"] in {"UNKNOWN_OUTCOME", "MODEL_IDENTITY_ERROR"}:
                    stop.set()
                results[q, a] = r
                append(out / "results.jsonl", {"query_id": q, "arm": a, "result": r})

        await asyncio.gather(*(worker() for _ in range(p["concurrency"])))
    value = report(inputs, results)
    write(out / "report.json", value)
    done = {k: v for k, v in value.items() if k not in {"cases", "arms"}}
    done["physical_dispatches"] = len(results)
    write(out / "completed.json", done)
    print(json.dumps({**done, "arms": value["arms"]}))
    if value["state"] != "COMPLETE" or value["unknown"] or value["identity_errors"]:
        raise SystemExit(2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("freeze", "execute"))
    asyncio.run(execute()) if parser.parse_args().action == "execute" else freeze()
