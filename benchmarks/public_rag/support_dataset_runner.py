"""Contemporaneous source-present/source-deleted baseline diagnostic, no new reader."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
from collections import Counter
from pathlib import Path
from statistics import fmean

import httpx

from benchmarks.public_rag.attributed_reader_runner import (
    CallFailure,
    RecordedGateway,
    fatal,
    from_wire,
)
from benchmarks.public_rag.consumer_replay import CONTEXT, _spec, to_wire
from benchmarks.public_rag.experimental.attributed_reader import (
    admit_reader_request,
    validate_reader_answer,
    visible_records,
)
from benchmarks.public_rag.fiqa_confirmation import _credentials
from benchmarks.public_rag.scenario_runner import dump
from benchmarks.public_rag.support_dataset import CONDITIONS, score_pairs, validate_dataset
from packages.model_gateway.contracts import ModelRequest


def summarize(dataset, rows, done):
    report = score_pairs(dataset, rows)
    calls = [
        r["conditions"][c]["api"]
        for r in rows
        for c in CONDITIONS
        if r["conditions"][c]["api"]["status"] != "NOT_ATTEMPTED"
    ]
    statuses = Counter(c["status"] for c in calls)
    usage = [c["usage"] for c in calls if isinstance(c.get("usage"), dict)]
    totals = [u["total_tokens"] for u in usage if type(u.get("total_tokens")) is int]
    per_condition = {}
    for condition in CONDITIONS:
        observed = [
            r["conditions"][condition]["api"]
            for r in rows
            if r["conditions"][condition]["api"]["status"] != "NOT_ATTEMPTED"
        ]
        per_condition[condition] = {
            "request_ms": [c["request_ms"] for c in observed],
            "mean_request_ms": fmean(c["request_ms"] for c in observed) if observed else None,
            "observed_tokens": {
                key: sum(
                    c["usage"][key]
                    for c in observed
                    if isinstance(c.get("usage"), dict) and type(c["usage"].get(key)) is int
                )
                for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            },
        }
    report.update(
        {
            "execution_complete": done["run_state"] == "COMPLETE",
            "physical_api": {
                "requests": len(calls),
                "statuses": dict(statuses),
                "observed_total_tokens": sum(totals),
                "usage_observed": len(totals),
                "usage_unknown": len(calls) - len(totals),
                "currency_cost": None,
            },
            "per_condition_cost": per_condition,
            "production_accepted": False,
            "default_changed": False,
            "new_reader_tested": False,
            "semantic_judge_used": False,
            "interpretation": (
                "Diagnostic calibration only; tiny exposed curated sample. "
                "Fresh requests in both conditions control request-time drift "
                "within this diagnostic, not an optimization comparison."
            ),
        }
    )
    return report


async def run(args):
    if args.output.exists():
        raise ValueError("refusing an existing diagnostic run")
    dataset = json.loads(args.dataset.read_text(encoding="utf-8"))
    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    validate_dataset(dataset)
    if len(dataset["cases"]) != protocol["pairs"] or len(dataset["ledger"]) != 32:
        raise ValueError("frozen diagnostic population differs from protocol")
    plan = _spec({}).model_plan
    if plan.primary.model != protocol["model"] or plan.primary.max_tokens != 256:
        raise ValueError("frozen baseline model plan differs")
    requests = {}
    for case in dataset["cases"]:
        for condition in CONDITIONS:
            wire = case["conditions"][condition]["messages"]
            request = admit_reader_request(ModelRequest(messages=from_wire(wire)), plan)
            if to_wire(request.messages) != wire:
                raise ValueError("diagnostic request changed after admission")
            requests[(case["query_id"], condition)] = request
    args.output.mkdir(parents=True)
    dump(
        args.output / "meta.json",
        {
            "dataset": str(args.dataset),
            "protocol": protocol,
            "pairs": len(dataset["cases"]),
            "scope": __doc__,
        },
    )
    dump(
        args.output / "label-access.json",
        {
            "scope": (
                "Previously exposed train cases only; labels for offline scoring, never model input"
            ),
            "new_holdout_or_confirmation_exposure": False,
            "all_32_ledger_cases_retained": True,
        },
    )
    stop, results = asyncio.Event(), {}
    first = [(dataset["cases"][0]["query_id"], c) for c in CONDITIONS]
    remaining = [key for key in requests if key not in first]
    random.Random(protocol["seed"]).shuffle(remaining)
    endpoint, key = _credentials()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    async with httpx.AsyncClient(
        timeout=30, transport=httpx.AsyncHTTPTransport(retries=0)
    ) as client:
        with (args.output / "api-events.jsonl").open("x", encoding="utf-8") as events:

            async def execute(job):
                qid, condition = job
                request = requests[job]
                gateway = RecordedGateway(client, endpoint, key, events, qid, condition)
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
                results[job] = {"api": api, "answer_messages": to_wire(request.messages)}
                if fatal(api) or sum(v["api"]["status"] != "VALID" for v in results.values()) >= 2:
                    stop.set()

            for job in first:
                if stop.is_set():
                    break
                await execute(job)
                if results[job]["api"]["status"] != "VALID":
                    stop.set()
            iterator = iter(remaining)

            async def worker():
                while not stop.is_set():
                    job = next(iterator, None)
                    if job is None:
                        return
                    await execute(job)

            await asyncio.gather(*(worker() for _ in range(protocol["workers"])))
    rows = [
        {
            "query_id": case["query_id"],
            "conditions": {
                condition: results.get(
                    (case["query_id"], condition),
                    {
                        "api": {"status": "NOT_ATTEMPTED", "usage": None, "request_ms": None},
                        "answer_messages": None,
                    },
                )
                for condition in CONDITIONS
            },
        }
        for case in dataset["cases"]
    ]
    dump(args.output / "run.json", rows)
    done = {
        "run_state": "COMPLETE"
        if len(results) == len(requests) and not stop.is_set()
        else "NEEDS_ATTENTION",
        "planned_requests": len(requests),
        "physical_requests": len(results),
    }
    dump(args.output / "completed.json", done)
    report = summarize(dataset, rows, done)
    dump(args.output / "report.json", report)
    print(
        json.dumps(
            {
                k: report[k]
                for k in (
                    "execution_complete",
                    "pairs",
                    "supported",
                    "answer_evidence_removed",
                    "both_correct",
                    "physical_api",
                )
            }
        )
    )
    if done["run_state"] != "COMPLETE":
        raise SystemExit(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("dataset", "protocol", "output"):
        parser.add_argument("--" + key, type=Path, required=True)
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
