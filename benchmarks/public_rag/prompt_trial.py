"""One bounded prompt-only public RAG trial; no production registration."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import httpx

from benchmarks.public_rag.attributed_reader_runner import (
    CallFailure,
    RecordedGateway,
    fatal,
    from_wire,
)
from benchmarks.public_rag.consumer_replay import CONTEXT, _spec, compact, to_wire
from benchmarks.public_rag.evidence_plan_replay import ANSWER_PROMPT
from benchmarks.public_rag.experimental.attributed_reader import (
    admit_reader_request,
    validate_reader_answer,
    visible_records,
)
from benchmarks.public_rag.fiqa_confirmation import _credentials
from benchmarks.public_rag.scenario_runner import dump
from packages.model_gateway.contracts import ModelRequest

TASK_GUIDANCE = """Identify the entity, relation and qualifiers actually requested.
Connect source facts only when the text establishes the link. Use intermediate
facts to identify the target, then answer its requested attribute, not a
neighboring attribute. Preserve time, scope, negation and comparison conditions.
Do not substitute a related fact or a remembered fact for a missing link.
If required links or the target attribute are absent, follow the insufficient-text rule.
"""
TASK_PROMPT = ANSWER_PROMPT.replace(
    "Return exactly this JSON object", TASK_GUIDANCE + "Return exactly this JSON object", 1
)
PROMPTS = {"baseline": ANSWER_PROMPT, "task_contract": TASK_PROMPT}


def prompt_request(messages: list[dict], arm: str) -> ModelRequest:
    """Change exactly one trusted instruction, preserving all other wire fields."""
    positions = [
        i
        for i, m in enumerate(messages)
        if m.get("role") == "system" and m.get("content") == ANSWER_PROMPT
    ]
    if len(positions) != 1:
        raise ValueError("exactly one original answer instruction required")
    updated = deepcopy(messages)
    updated[positions[0]]["content"] = PROMPTS[arm]
    request = ModelRequest(messages=from_wire(updated))
    if to_wire(request.messages) != updated:
        raise ValueError("wire representation changed")
    plan = _spec({}).model_plan
    if plan.primary.max_tokens != 256 or plan.fallbacks or plan.retry_policy.max_attempts != 1:
        raise ValueError("fixed one-attempt model plan required")
    admit_reader_request(request, plan)
    visible_records(request)
    return request


def delete_records(messages: list[dict], removed: list[str]) -> list[dict]:
    index, payload, records = visible_records(ModelRequest(messages=from_wire(messages)))
    known = {r["chunk_id"] for r in records}
    if not removed or len(set(removed)) != len(removed) or not set(removed) < known:
        raise ValueError("deletion requires a nonempty proper subset of original records")
    changed = deepcopy(messages)
    changed[index]["content"] = compact(
        {
            **payload,
            "data": {
                **payload["data"],
                "results": [r for r in records if r["chunk_id"] not in removed],
                "evidence_refs": [
                    r for r in payload["data"]["evidence_refs"] if r["chunk_id"] not in removed
                ],
            },
        }
    )
    return changed


def validate_frozen(dataset: dict, protocol: dict) -> None:
    """Enforce the exact frozen prompt and source-preserving contrast contract."""
    if dataset["state"] != "FROZEN_DEVELOPMENT" or protocol["prompts"] != PROMPTS:
        raise ValueError("frozen development data and exact registered prompts required")
    cases = dataset["cases"]
    if not cases or len({c["query_id"] for c in cases}) != len(cases):
        raise ValueError("unique planned cases required")
    development = {c["query_id"]: c for c in cases if c["cohort"] == "development"}
    if len(development) != protocol["development_queries"]:
        raise ValueError("complete development cohort required")
    for case in cases:
        if not case["aliases"] or any(
            not isinstance(a, str) or not a.strip() for a in case["aliases"]
        ):
            raise ValueError("official aliases required")
        for arm in PROMPTS:
            prompt_request(case["messages"], arm)
        if case["cohort"] == "source_contrast":
            source = development[case["pair_id"]]
            if case["group_id"] != source["group_id"] or case["aliases"] != source["aliases"]:
                raise ValueError("contrast source/alias binding changed")
            expected = (
                source["messages"]
                if case["condition"] == "supported"
                else delete_records(source["messages"], case["removed_source_ids"])
            )
            if case["messages"] != expected:
                raise ValueError("contrast may only delete registered original records and refs")
        elif case["cohort"] != "development" or case["condition"] != "original":
            raise ValueError("unregistered cohort")
    pairs = {}
    for case in cases:
        if case["cohort"] == "source_contrast":
            pairs.setdefault(case["pair_id"], []).append(case["condition"])
    if len(pairs) != protocol["contrast_pairs"] or any(
        sorted(v) != ["deleted", "supported"] for v in pairs.values()
    ):
        raise ValueError("all planned contrast pairs required")


async def run(args):
    from benchmarks.public_rag.prompt_trial_report import summarize

    if args.output.exists():
        raise ValueError("refusing an existing trial run")
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    validate_frozen(dataset, protocol)
    cases = dataset["cases"]
    requests = {
        (c["query_id"], a): prompt_request(c["messages"], a) for c in cases for a in PROMPTS
    }
    # Exact message equality, no digest or semantic deduplication. Reuse only
    # same-run outcomes, including failures, never a historical answer.
    owners, owner_for = {}, {}
    for key, request in requests.items():
        signature = (key[1], compact(to_wire(request.messages)))
        owner_for[key] = owners.setdefault(signature, key)
    jobs = list(dict.fromkeys(owner_for.values()))
    random.Random(protocol["seed"]).shuffle(jobs)
    args.output.mkdir(parents=True)
    dump(
        args.output / "meta.json",
        {
            "protocol": protocol,
            "dataset": str(args.dataset),
            "logical_cases": len(cases),
            "planned_logical_answers": len(requests),
            "planned_physical_requests": len(jobs),
            "started_at_utc": datetime.now(UTC).isoformat(),
            "source_scope": "frozen admitted exposed train160; no new holdout exposure",
        },
    )
    dump(
        args.output / "label-access.json",
        {
            "scope": "exposed development labels for offline scoring only",
            "labels_in_requests": False,
            "new_holdout_exposure": False,
        },
    )
    plan, stop, results = _spec({}).model_plan, asyncio.Event(), {}
    endpoint, key = _credentials()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    with (args.output / "api-events.jsonl").open("x", encoding="utf-8") as events:
        async with httpx.AsyncClient(
            timeout=30, transport=httpx.AsyncHTTPTransport(retries=0)
        ) as client:

            async def execute(job):
                qid, arm = job
                request = requests[job]
                gateway = RecordedGateway(client, endpoint, key, events, qid, arm)
                gateway.stage = "answer"
                _, _, records = visible_records(request)
                try:
                    response = await gateway.generate_resolved(CONTEXT, plan, request)
                    parsed = validate_reader_answer(response.content, records)
                    gateway.parsed(status="VALID", **parsed)
                except (ValueError, CallFailure) as exc:
                    api = gateway.calls.get("answer")
                    if api is None:
                        raise
                    if api["status"] == "RECEIVED":
                        gateway.parsed(status="INVALID_OUTPUT", validation_error=str(exc))
                api = gateway.calls["answer"]
                api["physical_id"] = qid + ":" + arm
                results[job] = api
                if fatal(api) or sum(c["status"] != "VALID" for c in results.values()) >= 5:
                    stop.set()

            # Both instructions get a sequential interface check before the
            # bounded interleaved population. These outcomes remain scored.
            first_case = next(c for c in cases if c["cohort"] == "development")
            for arm in PROMPTS:
                job = owner_for[(first_case["query_id"], arm)]
                jobs.remove(job)
                await execute(job)
                if results[job]["status"] != "VALID":
                    stop.set()
                if stop.is_set():
                    break
            iterator = iter(jobs)

            async def worker():
                while not stop.is_set():
                    job = next(iterator, None)
                    if job is None:
                        return
                    await execute(job)

            await asyncio.gather(*(worker() for _ in range(protocol["workers"])))
    rows = []
    for case in cases:
        arms = {}
        for arm in PROMPTS:
            logical = (case["query_id"], arm)
            owner = owner_for[logical]
            api = deepcopy(
                results.get(owner, {"status": "NOT_ATTEMPTED", "usage": None, "request_ms": None})
            )
            if logical != owner and owner in results:
                api["reused_from"] = owner[0]
            arms[arm] = {"api": api}
        rows.append({"query_id": case["query_id"], "arms": arms})
    dump(args.output / "run.json", rows)
    complete = len(results) == len(owners) and not stop.is_set()
    dump(
        args.output / "completed.json",
        {
            "run_state": "COMPLETE" if complete else "NEEDS_ATTENTION",
            "physical_requests": len(results),
            "planned_physical_requests": len(owners),
            "logical_answers": len(requests),
            "finished_at_utc": datetime.now(UTC).isoformat(),
        },
    )
    report = summarize(dataset, rows, protocol, complete)
    dump(args.output / "report.json", report)
    print(json.dumps(report, ensure_ascii=False))
    if not complete:
        raise SystemExit(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("dataset", "protocol", "output"):
        parser.add_argument("--" + key, type=Path, required=True)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
