"""Same-model thinking isolation on already source-reviewed, exposed evidence.

This is a development diagnosis, not new evidence for independent promotion.
The existing gateway, source-view adapter and scorer own their respective work.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import subprocess
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

from benchmarks.public_rag.answer_path_data import validate_source_slices
from benchmarks.public_rag.answer_path_live import (
    PROMPT,
    all_jobs,
    empty,
    parse_answer,
    request_for,
)
from benchmarks.public_rag.answer_path_report import score_diagnostic
from benchmarks.public_rag.consumer_replay import CONTEXT, to_wire
from benchmarks.public_rag.paired_analysis import grouped_difference
from benchmarks.public_rag.paragraph_qa_live import RouteClient, configured_route
from packages.agent_runtime.context_budget import (
    ContextBudgetConfig,
    ContextBudgetPolicy,
    ContextCategory,
)
from packages.model_gateway.adapters.litellm import LiteLLMProviderAdapter
from packages.model_gateway.configured_route import build_gateway
from scripts.model_route import projection

ROOT = Path("benchmarks/public_rag/results/rag-root-cause-20261024/reader-isolation-1")
SOURCE = Path("benchmarks/public_rag/results/answer-path-20261024/frozen-dataset.json")
ARMS = ("disabled", "enabled")
SEED = 2026102407
MAX_TOKENS = 4096
EFFORTS = {"disabled": "none", "enabled": "high"}


def clean_build():
    """Bind committed source without adding a parallel content-hash mechanism."""
    code_paths = ["apps", "packages", "benchmarks/public_rag", "scripts"]
    changed = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=no", "--", *code_paths], text=True
    )
    if changed.strip():
        raise ValueError("commit changed source before registering or executing")
    untracked = subprocess.check_output(
        ["git", "ls-files", "--others", "--exclude-standard", "--", *code_paths], text=True
    )
    if untracked.strip():
        raise ValueError("commit untracked experiment source before registering or executing")
    source = Path(__file__).resolve().relative_to(Path.cwd().resolve()).as_posix()
    subprocess.run(["git", "ls-files", "--error-unmatch", source], check=True, capture_output=True)
    return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()


def thinking_observed(outcomes):
    for outcome in outcomes:
        if outcome.get("reasoning_content_chars", 0) > 0:
            return True
        details = (outcome.get("provider_usage") or {}).get("completion_tokens_details") or {}
        if isinstance(details.get("reasoning_tokens"), int) and details["reasoning_tokens"] > 0:
            return True
    return False


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_new(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def route_for():
    route = configured_route()
    return replace(
        route,
        primary=replace(route.primary, max_tokens=MAX_TOKENS, timeout_seconds=60),
        fallbacks=tuple(replace(p, max_tokens=MAX_TOKENS) for p in route.fallbacks),
    )


def admit(request, profiles):
    result = ContextBudgetPolicy(
        profiles=profiles,
        config=ContextBudgetConfig(
            reserved_output_tokens=MAX_TOKENS,
            max_retrieval_tokens=8000,
            max_tool_result_tokens=4000,
        ),
    ).admit(
        request.messages,
        categories=(
            ContextCategory.SYSTEM_PROMPT,
            ContextCategory.CURRENT_USER_TASK,
            ContextCategory.RAG_EVIDENCE,
        ),
    )
    if result.messages != request.messages:
        raise ValueError("source view changed at model admission")
    return request


async def prepared(data, route):
    if data["state"] != "FROZEN_DEVELOPMENT":
        raise ValueError("source-reviewed frozen development data required")
    if any(c["review_status"] != "REVIEWED_SOURCE_CONTRAST" for c in data["cases"]):
        raise ValueError("unreviewed contrast")
    validate_source_slices(
        data,
        read(Path("benchmarks/public_rag/results/paragraph-qa-20261023/frozen-dataset.json")),
    )
    gateway, pid = build_gateway(route, CONTEXT)
    profiles = await gateway.resolver.resolve_chain(CONTEXT, pid)
    return {
        (s, c["query_id"], condition): admit(request_for(c, condition), profiles)
        for s, c, condition in all_jobs(data)
    }


async def freeze():
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    build_commit = clean_build()
    data, route = read(SOURCE), route_for()
    requests = await prepared(data, route)
    ROOT.mkdir()  # Registration is exclusive and makes no provider call.
    write_new(ROOT / "frozen-dataset.json", data)
    write_new(
        ROOT / "prepared-requests.json",
        [{"key": list(k), "messages": to_wire(v.messages)} for k, v in requests.items()],
    )
    write_new(
        ROOT / "protocol.json",
        {
            "state": "FROZEN_DEVELOPMENT_MODEL_ISOLATION",
            "registered_at_utc": datetime.now(UTC).isoformat(),
            "hypothesis": "Extra thinking may reduce relation errors under identical evidence.",
            "source_dataset": SOURCE.as_posix(),
            "data_status": "Previously exposed development; machine-assisted source review.",
            "arms": list(ARMS),
            "seed": SEED,
            "model_route": projection(route),
            "prompt": PROMPT,
            "max_tokens": MAX_TOKENS,
            "reasoning_effort": EFFORTS,
            "upstream_thinking": {"disabled": "disabled", "enabled": "enabled"},
            "wire_correction": (
                "First registration sent thinking only and stopped after one unobservable "
                "response. Second registration added the Chat field but dropped the existing "
                "upstream switch; disabled still exposed reasoning. This registration preserves "
                "both controls; same model, question, evidence, scorer and total ceiling. "
                "Both stopped registrations remain unchanged; no further parameter variants."
            ),
            "budget_note": "Equal total output ceiling, shared by reasoning and final output; "
            "no claim of equal final-answer tokens or a separately controllable reasoning cap.",
            "planned_logical_requests": len(requests) * len(ARMS),
            "maximum_physical_requests": len(requests) * len(ARMS) + 1,
            "metrics": [
                "unchanged EM/F1",
                "removed refusal",
                "natural refusal",
                "usage",
                "latency",
            ],
            "statistics": "paired paper-cluster percentile bootstrap; descriptive only",
            "scorer_version": "answer_path_report from build_commit plus committed source",
            "stop_rules": [
                "Any fallback, unknown transport, identity mismatch or non-400 provider error.",
                "Three HTTP400 rejections; no prompt repair, retries or continuation.",
                "Do not infer thinking effectiveness unless provider observability supports it.",
            ],
            "promotion": "Forbidden from this exposed small diagnostic, regardless of score.",
            "stronger_model": "NOT_RUN; first isolate a controlled change within one alias.",
            "build_commit": build_commit,
            "source_binding": (
                "clean tracked code and identical HEAD required at execution; no added hashes"
            ),
            "thinking_observability_gate": (
                "Stop if enabled exposes neither reasoning content nor reasoning tokens, "
                "or disabled exposes reasoning. Absence is not proof of private backend behavior."
            ),
            "paired_inference_gate": (
                "All registered results valid, one primary dispatch each and enabled reasoning "
                "observed; otherwise no paired CI."
            ),
            "cached_token_policy": (
                "Retain raw provider usage including cache details; canonical cached_tokens "
                "may be unknown. No monetary cache-cost inference."
            ),
            "formal_experiment": False,
            "holdout_exposed": False,
        },
    )
    print(json.dumps({"registered": len(requests) * len(ARMS), "new_model_calls": 0}))


def paired_report(data, rows):
    reports = {arm: score_diagnostic(data, rows[arm]) for arm in ARMS}
    all_results = [
        r
        for arm in ARMS
        for stratum in rows[arm].values()
        for row in stratum
        for r in row["conditions"].values()
    ]
    eligible = all(
        r["status"] == "VALID" and len(r.get("physical_outcomes", [])) == 1 for r in all_results
    ) and all(
        thinking_observed(r["physical_outcomes"])
        for stratum in rows["enabled"].values()
        for row in stratum
        for r in row["conditions"].values()
    )
    if not eligible:
        return {
            "arms": reports,
            "paired_differences": None,
            "comparison_status": "INELIGIBLE_INCOMPLETE_OR_UNOBSERVABLE",
            "production_promoted": False,
        }
    groups = {c["query_id"]: c["paper_id"] for c in data["cases"]}
    differences = {}
    from benchmarks.public_rag.consumer_answer_metrics import answer_metrics

    for condition in ("actual", "sufficient", "removed"):
        scores = {}
        for arm in ARMS:
            scores[arm] = {}
            for case, row in zip(data["cases"], rows[arm]["cases"], strict=True):
                result = row["conditions"][condition]
                valid = result["status"] == "VALID"
                score = (
                    float(valid and result["unanswerable"])
                    if condition == "removed"
                    else answer_metrics(result["answer"] or "", case["answer_references"])[
                        "token_f1"
                    ]
                    if valid and not result["unanswerable"]
                    else 0.0
                )
                scores[arm][case["query_id"]] = score
        raw = grouped_difference(scores["disabled"], scores["enabled"], groups, seed=SEED)
        differences[condition] = {
            "metric": "correct_refusal" if condition == "removed" else "token_f1",
            "estimate": raw["estimate_ms"],
            "ci95": [raw["ci95_low_ms"], raw["ci95_high_ms"]],
            "groups": raw["groups"],
            "interpretation": "enabled minus disabled; descriptive exposed development",
        }
    return {"arms": reports, "paired_differences": differences, "production_promoted": False}


async def execute():
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    import litellm  # noqa: F401

    protocol, data, route = (
        read(ROOT / "protocol.json"),
        read(ROOT / "frozen-dataset.json"),
        route_for(),
    )
    if clean_build() != protocol["build_commit"] or data != read(SOURCE):
        raise ValueError("frozen code commit or source dataset changed")
    if protocol["prompt"] != PROMPT or protocol["model_route"] != projection(route):
        raise ValueError("frozen model route or prompt changed")
    if protocol.get("reasoning_effort") != EFFORTS or protocol["max_tokens"] != MAX_TOKENS:
        raise ValueError("frozen reasoning controls changed")
    if protocol.get("upstream_thinking") != {"disabled": "disabled", "enabled": "enabled"}:
        raise ValueError("frozen upstream thinking switch changed")
    requests = await prepared(data, route)
    wire = [{"key": list(k), "messages": to_wire(v.messages)} for k, v in requests.items()]
    if wire != read(ROOT / "prepared-requests.json"):
        raise ValueError("frozen model-visible inputs changed")
    if protocol["planned_logical_requests"] != len(requests) * len(ARMS):
        raise ValueError("registered request population changed")
    out = ROOT / "execution-1"
    out.mkdir()
    budget = {"dispatched": 0, "limit": protocol["maximum_physical_requests"]}
    results, outcomes, stop = {}, [], None
    jobs = [(s, c, cond, arm) for s, c, cond in all_jobs(data) for arm in ARMS]
    random.Random(SEED).shuffle(jobs)
    with (out / "api-events.jsonl").open("x", encoding="utf-8") as events:
        for stratum, case, condition, arm in jobs:
            key = (stratum, case["query_id"], condition)
            client = RouteClient(
                events,
                {"query_id": key[1], "condition": condition, "arm": arm},
                budget,
                protocol["model_route"],
                [],
                max_tokens=MAX_TOKENS,
                thinking=arm,
                strict_primary=True,
                reasoning_effort=protocol["reasoning_effort"][arm],
            )
            gateway, pid = build_gateway(
                route, CONTEXT, adapter=LiteLLMProviderAdapter(client=client)
            )
            result, started = empty(), time.perf_counter()
            try:
                response = await gateway.generate(CONTEXT, pid, requests[key])
                result.update(
                    status="INVALID_OUTPUT",
                    raw_content=response.content,
                    provider=response.provider,
                    model=response.model,
                    usage=asdict(response.usage) if response.usage else None,
                    finish_reason=response.finish_reason,
                )
                try:
                    parsed = parse_answer(response.content, case, condition)
                    if response.tool_calls or response.finish_reason == "length":
                        raise ValueError("unexpected tools or truncated output")
                    result.update(parsed, status="VALID")
                except (TypeError, ValueError) as error:
                    result["parse_error"] = str(error)
            except Exception as error:
                result.update(status="PROVIDER_ERROR", error_type=type(error).__name__)
            result["latency_ms"] = (time.perf_counter() - started) * 1000
            physical = [{k: v for k, v in r.items() if k != "request"} for r in client.raw_calls]
            result["physical_outcomes"] = physical
            outcomes.extend(physical)
            if result["status"] == "VALID" and (
                (arm == "enabled" and not thinking_observed(physical))
                or (arm == "disabled" and thinking_observed(physical))
            ):
                stop = "THINKING_CONTROL_UNOBSERVABLE_OR_CONTRADICTED"
            if any(
                r["status"] in {"TRANSPORT_UNCONFIRMED", "MODEL_IDENTITY_ERROR", "DISPATCHED"}
                for r in physical
            ):
                result["status"], stop = "UNKNOWN_OUTCOME", "UNKNOWN_TRANSPORT_OR_IDENTITY"
            if any(r["endpoint"] != protocol["model_route"]["chain"][0]["name"] for r in physical):
                stop = stop or "FALLBACK_SINGLE_MODEL_COMPARISON_STOPPED"
            if (
                client.halted
                or (result["status"] == "PROVIDER_ERROR" and not physical)
                or any(
                    r["status"] == "PROVIDER_ERROR" and r.get("http_status") != 400
                    for r in physical
                )
            ):
                stop = stop or "PROVIDER_OR_LOCAL_GATE_ERROR"
            if sum(r.get("http_status") == 400 for r in outcomes) >= 3:
                stop = "THREE_HTTP400_REJECTIONS"
            results[(*key, arm)] = result
            with (out / "answers.jsonl").open("a", encoding="utf-8") as log:
                log.write(
                    json.dumps({"key": [*key, arm], "result": result}, ensure_ascii=False) + "\n"
                )
            if stop:
                break
    rows = {
        arm: {
            stratum: [
                {
                    "query_id": c["query_id"],
                    "paper_id": c["paper_id"],
                    "conditions": {
                        cond: results.get((stratum, c["query_id"], cond, arm), empty())
                        for cond in (
                            ("actual", "sufficient", "removed")
                            if stratum == "cases"
                            else ("actual",)
                        )
                    },
                }
                for c in data[stratum]
            ]
            for stratum in ("cases", "natural_controls")
        }
        for arm in ARMS
    }
    write_new(out / "run.json", rows)
    write_new(out / "report.json", paired_report(data, rows))
    complete = len(results) == len(jobs) and stop is None
    write_new(
        out / "completed.json",
        {
            "state": "COMPLETE" if complete else "NEEDS_ATTENTION",
            "planned": len(jobs),
            "completed": len(results),
            "physical_dispatches": budget["dispatched"],
            "stop_reason": stop,
            "outcomes": outcomes,
            "cost": None,
            "finished_at_utc": datetime.now(UTC).isoformat(),
            "production_promoted": False,
        },
    )
    print(json.dumps({"complete": complete, "completed": len(results), "stop_reason": stop}))
    if not complete:
        raise SystemExit(2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("freeze", "execute"))
    parser.add_argument("--registration-name", default="reader-isolation-1")
    args = parser.parse_args()
    if (
        not args.registration_name.startswith("reader-isolation-")
        or Path(args.registration_name).name != args.registration_name
    ):
        parser.error("a fresh experiment-local reader-isolation name is required")
    ROOT = ROOT.parent / args.registration_name
    asyncio.run(freeze() if args.action == "freeze" else execute())
