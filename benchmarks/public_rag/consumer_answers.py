"""Single-attempt public answer experiment over already admitted consumer messages."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
from pathlib import Path

import httpx

from benchmarks.public_rag.consumer_answer_client import generate_once
from benchmarks.public_rag.consumer_answer_metrics import answer_metrics, citation_metrics
from benchmarks.public_rag.fiqa_confirmation import _credentials
from benchmarks.public_rag.scenario_runner import dump


async def run(args):
    if args.output.exists():
        raise ValueError("refusing API replay/resume or overwrite")
    meta = json.loads((args.prepared / "meta.json").read_text(encoding="utf-8"))
    done = json.loads((args.prepared / "completed.json").read_text(encoding="utf-8"))
    rows = [
        json.loads(line)
        for line in (args.prepared / "prepared.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    if len(rows) != meta["queries"] or len(rows) != done["expected"]:
        raise ValueError("complete prepared consumer capture required")
    if len({row["query_id"] for row in rows}) != len(rows):
        raise ValueError("duplicate prepared questions")
    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    if meta["protocol"] != protocol:
        raise ValueError("consumer preparation differs from API protocol")
    profiles = meta["profiles"]
    if getattr(args, "profiles", None):
        selected_profiles = args.profiles.split(",")
        if (
            len(set(selected_profiles)) != len(selected_profiles)
            or not set(selected_profiles).issubset(profiles)
            or "legacy5" not in selected_profiles
        ):
            raise ValueError("API profile subset must be unique, prepared and include baseline")
        profiles = selected_profiles
    for row in rows:
        answer_metrics("", row["answers"])
        for profile in profiles:
            arm = row["arms"][profile]
            if not arm["admission_verified"]:
                raise ValueError("only actually admitted messages may be sent")
            budget = arm["budget"]
            if budget["estimated_input_after"] > budget["available_input"]:
                raise ValueError("prepared context exceeds its frozen budget")
    prior_results = {}
    if args.continue_from is not None:
        prior_meta = json.loads((args.continue_from / "meta.json").read_text(encoding="utf-8"))
        if prior_meta.get("prior_run") is not None:
            raise ValueError("chained continuation needs a separately audited full request ledger")
        if (
            prior_meta["protocol"] != protocol
            or prior_meta["profiles"] != profiles
            or Path(prior_meta["prepared"]).resolve() != args.prepared.resolve()
        ):
            raise ValueError("continuation must preserve exact original preparation and protocol")
        allowed_pairs = {(r["query_id"], p) for r in rows for p in profiles}
        for line in (
            (args.continue_from / "api-events.jsonl").read_text(encoding="utf-8").splitlines()
        ):
            event = json.loads(line)
            pair = event["query_id"], event["profile"]
            api = event["api"]
            if pair not in allowed_pairs or pair in prior_results:
                raise ValueError("ambiguous prior request identity")
            if api["status"] in {"UNKNOWN_OUTCOME", "MODEL_IDENTITY_ERROR"}:
                raise ValueError("cannot continue a run with unknown or wrong-model outcomes")
            if api["status"] == "PROVIDER_ERROR" and api.get("http_status") != 400:
                raise ValueError("only known HTTP400 per-request rejections permit continuation")
            prior_results[pair] = api
    args.output.mkdir(parents=True)
    dump(
        args.output / "meta.json",
        {
            "protocol": protocol,
            "profiles": profiles,
            "queries": len(rows),
            "prepared": str(args.prepared),
            "dataset": meta["dataset"],
            "split": meta["split"],
            "scope": __doc__,
            "attempts": 1,
            "tool_invocation": "fixed benchmark search call, not an autonomous tool-choice test",
            "generation_model": protocol["answers"]["model"],
            "prior_run": str(args.continue_from) if args.continue_from else None,
            "prior_observed_calls": len(prior_results),
            "execution_policy": "Known HTTP400 rejections retained as0 without retry; "
            "stop transport unknown, other provider errors, wrong model or invalid fraction>5%",
        },
    )
    endpoint, key = _credentials()
    suffix = "/chat/completions"
    if not endpoint.endswith(suffix):
        raise ValueError("unexpected configured experiment endpoint shape")
    base = endpoint[: -len(suffix)]
    stop, results = asyncio.Event(), dict(prior_results)
    invalid_count = sum(api["status"] == "INVALID_OUTPUT" for api in results.values())
    jobs = [(row, profile) for row in rows for profile in profiles]
    random.Random(protocol["seed"]).shuffle(jobs)
    # First planned request checks the actual tool-message protocol; its result
    # is retained as part of this run, never retried or discarded as a smoke test.
    planned_calls = len(jobs)
    jobs = [job for job in jobs if (job[0]["query_id"], job[1]) not in prior_results]
    if not jobs:
        raise ValueError("all planned requests already observed, no reason to continue")
    first = jobs[0]
    if not prior_results:
        first = (rows[0], "legacy5")
        jobs.remove(first)
        jobs.insert(0, first)
    iterator = iter(jobs)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    async with httpx.AsyncClient(
        timeout=30, transport=httpx.AsyncHTTPTransport(retries=0)
    ) as client:
        with (args.output / "api-events.jsonl").open("x", encoding="utf-8") as events:

            async def execute(row, profile):
                nonlocal invalid_count
                api = await generate_once(
                    row["arms"][profile]["messages"], client=client, base_url=base, api_key=key
                )
                events.write(
                    json.dumps(
                        {"query_id": row["query_id"], "profile": profile, "api": api},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                events.flush()
                results[(row["query_id"], profile)] = api
                status = api["status"]
                if status == "INVALID_OUTPUT":
                    invalid_count += 1
                if status in {"UNKNOWN_OUTCOME", "MODEL_IDENTITY_ERROR"} or (
                    status == "PROVIDER_ERROR" and api.get("http_status") != 400
                ):
                    stop.set()
                if invalid_count > max(3, int(planned_calls * 0.05)):
                    stop.set()

            async def worker():
                while not stop.is_set():
                    job = next(iterator, None)
                    if job is None:
                        return
                    await execute(*job)

            await execute(*next(iterator))
            first_status = results[(first[0]["query_id"], first[1])]["status"]
            if first_status != "VALID":
                stop.set()
            await asyncio.gather(*(worker() for _ in range(protocol["answers"]["workers"])))
    with (args.output / "run.jsonl").open("x", encoding="utf-8") as output:
        for row in rows:
            arms = {}
            for profile in profiles:
                prepared = row["arms"][profile]
                api = results.get(
                    (row["query_id"], profile),
                    {"status": "NOT_ATTEMPTED", "usage": None, "request_ms": None},
                )
                valid = api["status"] == "VALID"
                answer = api["answer"] if valid else ""
                cited = api["cited_chunk_ids"] if valid else []
                evidence = prepared["stages"]["final_admitted_context"]
                arms[profile] = {
                    "api": api,
                    **answer_metrics(answer, row["answers"]),
                    "citation": citation_metrics(cited, prepared["admitted_chunk_ids"]),
                    "evidence_recall": evidence["best_evidence_recall"],
                    "complete_evidence_set": evidence["complete_evidence_set"],
                    "full_text_evidence_recall": evidence["full_text_evidence_recall"],
                    "final_evidence_bytes": prepared["final_evidence_bytes"],
                }
            output.write(
                json.dumps(
                    {
                        "query_id": row["query_id"],
                        "group_id": row["group_id"],
                        "hops": row["hops"],
                        "arms": arms,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    state = "NEEDS_ATTENTION" if stop.is_set() else "COMPLETE"
    dump(
        args.output / "completed.json",
        {
            "expected": len(rows),
            "queries": len(rows),
            "planned_calls": planned_calls,
            "observed_calls": len(results),
            "new_calls": len(results) - len(prior_results),
            "run_state": state,
        },
    )
    print(json.dumps({"run_state": state, "questions": len(rows), "calls": len(results)}))
    if stop.is_set():
        raise SystemExit(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--continue-from", type=Path)
    parser.add_argument("--profiles")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
