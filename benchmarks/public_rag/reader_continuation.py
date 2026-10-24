"""Human-authorized continuation of never-dispatched reader jobs only.

The interrupted request is UNKNOWN_OUTCOME and is never retried. Old ledgers
are read-only. An amendment binds the new driver and the unchanged old sources.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import subprocess
import time
from dataclasses import asdict
from datetime import UTC, datetime

from benchmarks.public_rag import reader_isolation as base
from benchmarks.public_rag.consumer_replay import CONTEXT, to_wire
from packages.model_gateway.adapters.litellm import LiteLLMProviderAdapter
from packages.model_gateway.configured_route import build_gateway
from scripts.model_route import projection

ROOT = base.ROOT.parent / "reader-models-1"
OUT = ROOT / "continuation-1"


def lines(path):
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def configure():
    base.COMPARISON = "models"
    base.ARMS = ("deepseek_high", "glm_high")
    base.EFFORTS = {a: "high" for a in base.ARMS}
    base.UPSTREAM = {a: "enabled" for a in base.ARMS}


def recover(planned, events, saved):
    """Keep known outputs and seal dispatched-but-unsaved results as unknown."""
    expected = {tuple(key) for key in planned}
    lookup = {(k[1], k[2], k[3]): tuple(k) for k in expected}
    dispatched, last, known = set(), {}, {}
    for event in events:
        short = (event.get("query_id"), event.get("condition"), event.get("arm"))
        if short not in lookup:
            raise ValueError("event outside registered population")
        key = lookup[short]
        if event["status"] == "DISPATCHED":
            if key in dispatched:
                raise ValueError("duplicate physical attempt: no automatic continuation")
            dispatched.add(key)
        last[key] = {k: v for k, v in event.items() if k != "request"}
    for row in saved:
        key = tuple(row["key"])
        if key not in dispatched or key in known:
            raise ValueError("saved output lacks unique registered dispatch")
        known[key] = row["result"]
    unknown = dispatched - set(known)
    for key in unknown:
        known[key] = {
            **base.empty(),
            "status": "UNKNOWN_OUTCOME",
            "interruption": "USER_STOP_WITHOUT_CONFIRMED_PERSISTED_OUTPUT",
            "physical_outcomes": [last[key]],
        }
    return known, [tuple(k) for k in planned if tuple(k) not in dispatched], sorted(unknown)


def unchanged_sources(old_commit):
    subprocess.run(
        [
            "git",
            "diff",
            "--exit-code",
            old_commit,
            "HEAD",
            "--",
            "apps",
            "packages",
            "scripts",
            "benchmarks/public_rag",
            ":(exclude)benchmarks/public_rag/reader_continuation.py",
            ":(exclude)benchmarks/public_rag/results/published/**",
        ],
        check=True,
        capture_output=True,
    )


async def inputs():
    configure()
    protocol, data = base.read(ROOT / "protocol.json"), base.read(ROOT / "frozen-dataset.json")
    unchanged_sources(protocol["build_commit"])
    if (
        protocol["comparison"] != "models"
        or protocol["arms"] != list(base.ARMS)
        or protocol["arm_routes"] != {a: projection(base.route_for(a)) for a in base.ARMS}
        or protocol["reasoning_effort"] != base.EFFORTS
        or protocol["upstream_thinking"] != base.UPSTREAM
        or protocol["expected_response_models"] != base.RESPONSE_MODELS
        or protocol["prompt"] != base.PROMPT
        or data != base.read(base.SOURCE)
    ):
        raise ValueError("original model, data or scoring controls changed")
    requests = await base.prepared(data, base.route_for())
    if [
        {"key": list(k), "messages": to_wire(v.messages)} for k, v in requests.items()
    ] != base.read(ROOT / "prepared-requests.json"):
        raise ValueError("original registered model-visible inputs changed")
    jobs = [(s, c, cond, arm) for s, c, cond in base.all_jobs(data) for arm in base.ARMS]
    random.Random(base.SEED).shuffle(jobs)
    planned = [(s, c["query_id"], cond, arm) for s, c, cond, arm in jobs]
    known, pending, unknown = recover(
        planned,
        lines(ROOT / "execution-1/api-events.jsonl"),
        lines(ROOT / "execution-1/answers.jsonl"),
    )
    return protocol, data, requests, jobs, known, pending, unknown


async def freeze():
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    commit = base.clean_build()
    protocol, _, _, _, known, pending, unknown = await inputs()
    OUT.mkdir()
    base.write_new(
        OUT / "amendment.json",
        {
            "state": "FROZEN_USER_AUTHORIZED_CONTINUATION",
            "at_utc": datetime.now(UTC).isoformat(),
            "authorization": "User explicitly said OK可以继续，运行调用 after requesting stop.",
            "original_build_commit": protocol["build_commit"],
            "continuation_build_commit": commit,
            "original_planned": protocol["planned_logical_requests"],
            "prior_classified": len(known),
            "unknown_sealed_no_retry": [list(k) for k in unknown],
            "pending_keys": [list(k) for k in pending],
            "maximum_new_dispatches": len(pending),
            "unchanged": "questions, views, models, high controls, budgets and scorer",
            "primary_inference": (
                "Original all-valid gate remains; prior unknown prevents full primary CI."
            ),
            "secondary_conditions": (
                "Descriptive intervals only for an originally registered condition whose "
                "entire 16 responses are valid. Never drop a case or promote from these intervals."
            ),
            "continuation_effect": "No resampling, no replay of completed or uncertain requests.",
        },
    )
    print(json.dumps({"pending_requests": len(pending), "prior_unknown_not_retried": len(unknown)}))


def secondary_conditions(data, rows):
    from benchmarks.public_rag.consumer_answer_metrics import answer_metrics
    from benchmarks.public_rag.paired_analysis import grouped_difference

    answer = {}
    for condition in ("actual", "sufficient", "removed"):
        values = {a: {} for a in base.ARMS}
        valid = True
        for arm in base.ARMS:
            for case, row in zip(data["cases"], rows[arm]["cases"], strict=True):
                result = row["conditions"][condition]
                valid = valid and result["status"] == "VALID"
                value = (
                    float(bool(result["unanswerable"]))
                    if condition == "removed"
                    else float(
                        answer_metrics(result["answer"] or "", case["answer_references"])[
                            "token_f1"
                        ]
                    )
                )
                values[arm][case["query_id"]] = value
        if not valid:
            answer[condition] = {
                "status": "NOT_IDENTIFIABLE_INCOMPLETE_CONDITION",
                "delta": None,
                "ci95": None,
                "registered_pairs": len(data["cases"]),
            }
            continue
        raw = grouped_difference(
            values[base.ARMS[0]],
            values[base.ARMS[1]],
            {c["query_id"]: c["paper_id"] for c in data["cases"]},
            seed=base.SEED,
        )
        answer[condition] = {
            "status": "AMENDED_DESCRIPTIVE_ONLY",
            "delta": raw["estimate_ms"],
            "ci95": [raw["ci95_low_ms"], raw["ci95_high_ms"]],
            "registered_pairs": len(data["cases"]),
            "paper_groups": raw["groups"],
            "metric": "registered_removed_refusal"
            if condition == "removed"
            else "official_token_f1",
        }
    return answer


async def execute():
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    import litellm  # noqa: F401

    amendment = base.read(OUT / "amendment.json")
    if base.clean_build() != amendment["continuation_build_commit"]:
        raise ValueError("continuation code changed")
    protocol, data, requests, jobs, results, pending, unknown = await inputs()
    if [list(k) for k in pending] != amendment["pending_keys"] or [
        list(k) for k in unknown
    ] != amendment["unknown_sealed_no_retry"]:
        raise ValueError("prior ledger changed since amendment")
    out = OUT / "execution-1"
    out.mkdir()
    budget = {"dispatched": 0, "limit": len(pending)}
    pending_set, stop = set(pending), None
    with (out / "api-events.jsonl").open("x", encoding="utf-8") as events:
        for s, case, condition, arm in jobs:
            key = (s, case["query_id"], condition, arm)
            if key not in pending_set:
                continue
            client = base.RouteClient(
                events,
                {"query_id": key[1], "condition": condition, "arm": arm},
                budget,
                protocol["arm_routes"][arm],
                [],
                max_tokens=base.MAX_TOKENS,
                thinking=base.UPSTREAM[arm],
                reasoning_effort=base.EFFORTS[arm],
                strict_primary=True,
                response_models=base.RESPONSE_MODELS,
            )
            gateway, pid = build_gateway(
                base.route_for(arm), CONTEXT, adapter=LiteLLMProviderAdapter(client=client)
            )
            result, started = base.empty(), time.perf_counter()
            try:
                response = await gateway.generate(CONTEXT, pid, requests[key[:3]])
                result.update(
                    status="INVALID_OUTPUT",
                    raw_content=response.content,
                    model=response.model,
                    provider=response.provider,
                    usage=asdict(response.usage) if response.usage else None,
                    finish_reason=response.finish_reason,
                )
                try:
                    value = base.parse_answer(response.content, case, condition)
                    if response.tool_calls or response.finish_reason == "length":
                        raise ValueError("unexpected tools or truncated output")
                    result.update(value, status="VALID")
                except (TypeError, ValueError) as error:
                    result["parse_error"] = str(error)
            except Exception as error:
                result.update(status="PROVIDER_ERROR", error_type=type(error).__name__)
            result["latency_ms"] = (time.perf_counter() - started) * 1000
            physical = [{k: v for k, v in r.items() if k != "request"} for r in client.raw_calls]
            result["physical_outcomes"] = physical
            if any(
                r["status"] in {"DISPATCHED", "TRANSPORT_UNCONFIRMED", "MODEL_IDENTITY_ERROR"}
                for r in physical
            ):
                result["status"], stop = "UNKNOWN_OUTCOME", "UNKNOWN_TRANSPORT_OR_IDENTITY"
            if client.halted or (result["status"] == "PROVIDER_ERROR" and not physical):
                stop = stop or "PROVIDER_OR_LOCAL_GATE_ERROR"
            if any(
                r["endpoint"] != protocol["arm_routes"][arm]["chain"][0]["name"] for r in physical
            ):
                stop = stop or "FALLBACK_COMPARISON_STOPPED"
            results[key] = result
            with (out / "answers.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps({"key": list(key), "result": result}, ensure_ascii=False) + "\n"
                )
            if stop:
                break
    rows = {
        arm: {
            s: [
                {
                    "query_id": c["query_id"],
                    "paper_id": c["paper_id"],
                    "conditions": {
                        cond: results.get((s, c["query_id"], cond, arm), base.empty())
                        for cond in (
                            ("actual", "sufficient", "removed") if s == "cases" else ("actual",)
                        )
                    },
                }
                for c in data[s]
            ]
            for s in ("cases", "natural_controls")
        }
        for arm in base.ARMS
    }
    base.write_new(out / "run.json", rows)
    report = base.paired_report(data, rows)
    report["secondary_complete_conditions"] = secondary_conditions(data, rows)
    report["execution_scope"] = (
        "Merged original and authorized continuation; one sealed prior unknown."
    )
    base.write_new(out / "report.json", report)
    done = {
        "state": "REMAINING_DISPATCHES_COMPLETE_WITH_PRIOR_UNKNOWN"
        if not stop and budget["dispatched"] == len(pending)
        else "NEEDS_ATTENTION",
        "original_planned": protocol["planned_logical_requests"],
        "new_dispatches": budget["dispatched"],
        "maximum_new_dispatches": len(pending),
        "prior_unknown_not_retried": len(unknown),
        "stop_reason": stop,
        "production_promoted": False,
        "cost": None,
    }
    base.write_new(out / "completed.json", done)
    print(json.dumps(done))
    if stop or budget["dispatched"] != len(pending):
        raise SystemExit(2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("freeze", "execute"))
    asyncio.run(freeze() if parser.parse_args().action == "freeze" else execute())
