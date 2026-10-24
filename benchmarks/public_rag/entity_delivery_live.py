"""One registered, bounded entity-delivery development batch. No continuation."""

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
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from benchmarks.public_rag.consumer_replay import CONTEXT, to_wire
from benchmarks.public_rag.experimental.entity_delivery import (
    BASELINE_PROMPT,
    TYPED_PROMPT,
    EntityDeliveryReader,
    EntityTask,
    admit_request,
    build_request,
)
from benchmarks.public_rag.paragraph_qa_live import RouteClient, article_and_spans, configured_route
from benchmarks.public_rag.scenario_runner import dump
from packages.model_gateway.adapters.litellm import LiteLLMProviderAdapter
from packages.model_gateway.configured_route import build_gateway
from packages.model_gateway.contracts import (
    ResolvedModelExecutionPlan,
    ResolvedModelExecutionProfile,
    RetryPolicy,
)
from scripts.model_route import projection

ROOT = Path("benchmarks/public_rag/results/entity-delivery-20261024")
ARMS = ("baseline", "typed")
CONDITIONS = ("full", "removed")
SEED = 2026102402


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def task_for(case):
    return EntityTask(
        case["question"],
        case["paper_id"],
        case["paper_title"],
        case["source_revision"],
        case["task"]["relation"],
    )


def jobs(data):
    return [
        (case, condition, arm) for case in data["cases"] for condition in CONDITIONS for arm in ARMS
    ]


async def model_plan(gateway, profile_id):
    chain = await gateway.resolver.resolve_chain(CONTEXT, profile_id)
    frozen = []
    for profile in chain:
        credential = await gateway.repository.get_provider_credential(
            CONTEXT, profile.provider_credential_id
        )
        if credential is None or not credential.enabled:
            raise ValueError("frozen execution credential is unavailable")
        frozen.append(
            ResolvedModelExecutionProfile(
                id=profile.id,
                workspace_id=profile.workspace_id,
                provider_credential_id=profile.provider_credential_id,
                provider=credential.provider,
                model=profile.model,
                temperature=profile.temperature,
                max_tokens=profile.max_tokens,
                timeout_seconds=profile.timeout_seconds,
                capabilities=profile.capabilities,
            )
        )
    return ResolvedModelExecutionPlan(
        primary=frozen[0], fallbacks=tuple(frozen[1:]), retry_policy=RetryPolicy(max_attempts=1)
    )


async def preflight(data, route):
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    if data["state"] != "FROZEN_DEVELOPMENT" or len(data["cases"]) < 8:
        raise ValueError("at least eight source-reviewed frozen cases required")
    articles = {p["paper_id"]: article_and_spans(p)[0] for p in data["corpus"]}
    qids, identities, checked = set(), {}, 0
    for case in data["cases"]:
        if case["review_status"] != "REVIEWED_SOURCE_CONTRAST" or case["query_id"] in qids:
            raise ValueError("unreviewed or duplicate case")
        qids.add(case["query_id"])
        positive = case["conditions"]["full"]
        negative = case["conditions"]["removed"]
        required = {e["value"] for e in case["gold_entities"]}
        if not required or set(positive["supported_entities"]) != required:
            raise ValueError("full entity targets not registered")
        if positive["expected_status"] != "ANSWERED":
            raise ValueError("invalid full delivery target")
        if not negative["records"] or not set(negative["supported_entities"]) < required:
            raise ValueError("negative must retain background while losing requested entity facts")
        expected = "PARTIAL" if negative["supported_entities"] else "INSUFFICIENT"
        if negative["expected_status"] != expected:
            raise ValueError("negative delivery labels contradict supplied entity facts")
        full_by_id = {r["source_id"]: r for r in positive["records"]}
        remaining = negative["records"]
        if any(full_by_id.get(r["source_id"]) != r for r in remaining):
            raise ValueError("negative changed the meaning of a retained source")
        if [
            r
            for r in positive["records"]
            if r["source_id"] in {item["source_id"] for item in remaining}
        ] != remaining:
            raise ValueError("negative changed source order")
        for condition in CONDITIONS:
            for record in case["conditions"][condition]["records"]:
                if record["source_revision"] != "qasper-train-v0.3:" + case["paper_id"]:
                    raise ValueError("unregistered source revision")
                text = articles[case["paper_id"]]
                parts = []
                for span in record["source_ranges"]:
                    start, end = span["char_start"], span["char_end"]
                    if (
                        type(start) is not int
                        or type(end) is not int
                        or not 0 <= start < end <= len(text)
                    ):
                        raise ValueError("invalid original source coordinates")
                    parts.append(text[start:end])
                if not parts or "".join(parts) != record["text"]:
                    raise ValueError("source view is not exact original text")
                identity = {
                    k: record[k] for k in ("paper_id", "source_revision", "source_ranges", "text")
                }
                if identities.setdefault(record["source_id"], identity) != identity:
                    raise ValueError("source identity changed across cases")
                checked += 1
    gateway, profile_id = build_gateway(route, CONTEXT)
    plan = await model_plan(gateway, profile_id)
    prepared = []
    for case, condition, arm in jobs(data):
        request = admit_request(
            build_request(task_for(case), case["conditions"][condition]["records"], arm), plan
        )
        await gateway.prepare_resolved(CONTEXT, plan, request, operation="generate")
        prepared.append(
            {
                "query_id": case["query_id"],
                "condition": condition,
                "arm": arm,
                "messages": to_wire(request.messages),
            }
        )
    return prepared, checked


async def freeze():
    data, route = read(ROOT / "frozen-dataset.json"), configured_route()
    prepared, checked = await preflight(data, route)
    target = ROOT / "protocol.json"
    if target.exists():
        raise ValueError("refusing to overwrite registration")
    protocol = {
        "registered_at_utc": datetime.now(UTC).isoformat(),
        "scope": "Development delivery prototype; supplied original evidence, not retrieval E2E",
        "seed_for_dispatch_order": SEED,
        "prompts": {"baseline": BASELINE_PROMPT, "typed": TYPED_PROMPT},
        "model_route": projection(route),
        "max_tokens": 1024,
        "temperature": 0,
        "thinking": "disabled",
        "source_budget_units": 8000,
        "case_ids": [c["query_id"] for c in data["cases"]],
        "planned_requests": len(prepared),
        "maximum_physical_requests": len(prepared) + 1,
        "source_records_checked": checked,
        "stop_rules": [
            "fallback or unknown outcome or wrong raw model: stop after current request",
            "non-400 provider error or 3 known 400s: stop; no retries or continuation",
        ],
        "development_gates": [
            "full entity-set exact rate no worse than shared-value baseline",
            "wrong complete negative delivery no worse; invalid output never safe delivery",
            "at least one strict gain in full set exact, "
            "wrong complete negatives or safe partial coverage",
            "typed valid format >=95%; combined recorded tokens <=1.6x baseline",
            "complete registered batch, one raw primary model, "
            "all source slices and request messages verified",
            "any claimed quality gain must persist on the predeclared unambiguous core",
        ],
        "sensitivity_excluded_query_ids": [
            "a8e4a67dd67ae4a9ebf983a90b0d256f4b9ff6c6",
            "2ff3898fbb5954aa82dd2f60b37dd303449c81ba",
        ],
        "sensitivity_reason": (
            "SST corpus-family/variant granularity and char-level subset/global dataset scope "
            "are disputed before answer calls; "
            "retain all eight primary rows and inspect six-case core."
        ),
        "limits": [
            "aliases and self-reported states do not prove semantic role or exhaustive coverage",
            "no expert calibrated semantic accuracy; no automatic production promotion",
            "new shared entity-value baseline is not historical long-form F1",
        ],
        "build_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "workspace_dirty": True,
        "formal_experiment": False,
        "holdout_exposed": False,
    }
    dump(ROOT / "prepared-requests.json", prepared)
    dump(target, protocol)
    paths = [
        Path("benchmarks/public_rag/experimental/entity_delivery.py"),
        *Path("benchmarks/public_rag").glob("entity_delivery*.py"),
        Path("docs/benchmark/entity-delivery-milestone-20261024.md"),
        Path("benchmarks/public_rag/paragraph_qa_live.py"),
        Path("benchmarks/public_rag/experimental/attributed_reader.py"),
        Path("benchmarks/public_rag/consumer_answer_metrics.py"),
        Path("packages/model_gateway/configured_route.py"),
        Path("packages/agent_runtime/context_budget.py"),
        Path("benchmarks/public_rag/model-route.json"),
        Path(".scratch/entity-delivery-20261024/curate.py"),
        ROOT / "parent-source-review.json",
        ROOT / "granularity-amendment.json",
        ROOT / "source-review-audit.json",
        ROOT / "frozen-dataset.json",
        ROOT / "source-review.json",
        target,
        ROOT / "prepared-requests.json",
    ]
    with ZipFile(ROOT / "execution-sources.zip", "x", compression=ZIP_DEFLATED) as archive:
        for path in paths:
            archive.write(path, str(path).replace("\\", "/"))
    print(json.dumps({"registered_requests": len(prepared), "cases": len(data["cases"])}))


def empty():
    return {
        "status": "NOT_ATTEMPTED",
        "delivery_status": None,
        "answer_values": [],
        "explanation": "",
        "cited_source_ids": [],
        "usage": None,
        "latency_ms": None,
    }


def execution_directory():
    amendment_path = ROOT / "execution-amendment-1.json"
    if not amendment_path.exists():
        return ROOT / "development-run-1"
    amendment = read(amendment_path)
    original = read(ROOT / "development-run-1/completed.json")
    events = (ROOT / "development-run-1/api-events.jsonl").read_text(encoding="utf-8")
    if (
        amendment.get("permitted_run_name") != "development-run-2"
        or amendment.get("original_physical_dispatches") != 0
        or original["physical_dispatches"] != 0
        or original["outcomes"]
        or events.strip()
    ):
        raise ValueError("pre-dispatch repair cannot replay a sent or uncertain request")
    return ROOT / "development-run-2"


async def execute():
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    import litellm  # noqa: F401

    from benchmarks.public_rag.entity_delivery_report import score_delivery

    data, protocol = read(ROOT / "frozen-dataset.json"), read(ROOT / "protocol.json")
    route = configured_route()
    if protocol["prompts"] != {"baseline": BASELINE_PROMPT, "typed": TYPED_PROMPT}:
        raise ValueError("registered prompt changed")
    if projection(route) != protocol["model_route"]:
        raise ValueError("registered route changed")
    prepared, checked = await preflight(data, route)
    if (
        prepared != read(ROOT / "prepared-requests.json")
        or len(prepared) != protocol["planned_requests"]
    ):
        raise ValueError("registered messages or population changed")
    out = execution_directory()
    out.mkdir()
    budget = {"dispatched": 0, "limit": protocol["maximum_physical_requests"]}
    schedule = jobs(data)
    random.Random(SEED).shuffle(schedule)
    results, physical, stop = {}, [], None
    with (out / "api-events.jsonl").open("x", encoding="utf-8") as events:
        for case, condition, arm in schedule:
            labels = {"query_id": case["query_id"], "condition": condition, "arm": arm}
            client = RouteClient(events, labels, budget, protocol["model_route"], [])
            gateway, profile_id = build_gateway(
                route, CONTEXT, adapter=LiteLLMProviderAdapter(client=client)
            )
            plan = await model_plan(gateway, profile_id)
            result = empty()
            started = time.perf_counter()
            try:
                returned = await EntityDeliveryReader(gateway).read(
                    CONTEXT, plan, task_for(case), case["conditions"][condition]["records"], arm
                )
                response = returned.response
                result.update(
                    status="INVALID_OUTPUT",
                    raw_content=response.content,
                    usage=asdict(response.usage) if response.usage else None,
                    provider=response.provider,
                    model=response.model,
                    finish_reason=response.finish_reason,
                )
                if returned.delivery is not None:
                    result.update(returned.delivery, status="VALID")
                else:
                    result["parse_error"] = returned.contract_error
            except Exception as error:
                result.update(status="PROVIDER_ERROR", error_type=type(error).__name__)
            result["latency_ms"] = (time.perf_counter() - started) * 1000
            outcomes = [{k: v for k, v in c.items() if k != "request"} for c in client.raw_calls]
            result["physical_outcomes"] = outcomes
            physical.extend(outcomes)
            if any(
                c["status"] in {"TRANSPORT_UNCONFIRMED", "MODEL_IDENTITY_ERROR", "DISPATCHED"}
                for c in outcomes
            ):
                result["status"], stop = "UNKNOWN_OUTCOME", "UNCONFIRMED_TRANSPORT_OR_IDENTITY"
            if any(c["endpoint"] != protocol["model_route"]["chain"][0]["name"] for c in outcomes):
                stop = stop or "FALLBACK_NO_SINGLE_MODEL_COMPARISON"
            if (
                client.halted
                or (result["status"] == "PROVIDER_ERROR" and not outcomes)
                or any(
                    c["status"] == "PROVIDER_ERROR" and c.get("http_status") != 400
                    for c in outcomes
                )
            ):
                stop = stop or "PROVIDER_OR_LOCAL_GUARD_ERROR"
            if sum(c.get("http_status") == 400 for c in physical) >= 3:
                stop = "THREE_KNOWN_400_REJECTIONS"
            results[(case["query_id"], condition, arm)] = result
            with (out / "answers.jsonl").open("a", encoding="utf-8") as log:
                log.write(json.dumps({**labels, "result": result}, ensure_ascii=False) + "\n")
            if stop:
                break
    rows = [
        {
            "query_id": c["query_id"],
            "paper_id": c["paper_id"],
            "conditions": {
                condition: {
                    arm: results.get((c["query_id"], condition, arm), empty()) for arm in ARMS
                }
                for condition in CONDITIONS
            },
        }
        for c in data["cases"]
    ]
    done = {
        "run_state": "COMPLETE"
        if not stop and len(results) == len(prepared)
        else "NEEDS_ATTENTION",
        "planned_requests": len(prepared),
        "completed_requests": len(results),
        "physical_dispatches": budget["dispatched"],
        "stop_reason": stop,
        "outcomes": physical,
        "source_records_checked": checked,
        "finished_at_utc": datetime.now(UTC).isoformat(),
        "currency_cost": None,
        "production_promoted": False,
    }
    dump(out / "run.json", rows)
    dump(out / "completed.json", done)
    dump(out / "report.json", score_delivery(data, rows))
    print(json.dumps({k: v for k, v in done.items() if k != "outcomes"}))
    if done["run_state"] != "COMPLETE":
        raise SystemExit(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("freeze", "execute"))
    phase = parser.parse_args().phase
    asyncio.run(freeze() if phase == "freeze" else execute())


if __name__ == "__main__":
    main()
