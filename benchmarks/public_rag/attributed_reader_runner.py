"""One fixed span-selection/answer experiment on unchanged admitted RAG sources."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import time
from dataclasses import asdict, replace
from pathlib import Path

import httpx

from benchmarks.public_rag.consumer_answer_client import (
    EXPERIMENT_MODEL,
    _observed_usage,
    _parse_answer,
    _strict_json_loads,
)
from benchmarks.public_rag.consumer_answer_metrics import answer_metrics, citation_metrics
from benchmarks.public_rag.consumer_replay import CONTEXT, _spec, compact, to_wire
from benchmarks.public_rag.experimental.attributed_reader import (
    AttributedEvidenceReader,
    admit_reader_request,
    strict_object,
    visible_records,
)
from benchmarks.public_rag.fiqa_confirmation import _credentials
from benchmarks.public_rag.scenario_runner import dump
from packages.model_gateway.contracts import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelToolCall,
)

PROFILES = ["direct256_prior", "direct768", "attribute512_answer256"]


def from_wire(messages):
    return tuple(
        ModelMessage(
            role=m["role"],
            content=m.get("content"),
            name=m.get("name"),
            tool_call_id=m.get("tool_call_id"),
            tool_calls=tuple(
                ModelToolCall(
                    c["function"]["name"], json.loads(c["function"]["arguments"]), c["id"]
                )
                for c in m.get("tool_calls", [])
            ),
        )
        for m in messages
    )


class CallFailure(Exception):
    pass


def fatal(api):
    return api["status"] in {"UNKNOWN_OUTCOME", "MODEL_IDENTITY_ERROR"} or (
        api["status"] == "PROVIDER_ERROR" and api.get("http_status") != 400
    )


class RecordedGateway:
    """Benchmark adapter only; frozen identity/JSON mode/one attempt per stage."""

    def __init__(self, client, endpoint, key, events, query_id, profile):
        self.client, self.endpoint, self.key, self.events = client, endpoint, key, events
        self.query_id, self.profile = query_id, profile
        self.stage, self.calls = None, {}

    def log(self, event, **fields):
        self.events.write(
            compact(
                {
                    "event": event,
                    "query_id": self.query_id,
                    "profile": self.profile,
                    "stage": self.stage,
                    **fields,
                }
            )
            + "\n"
        )
        self.events.flush()

    async def generate_resolved(self, context, plan, request):
        if context != CONTEXT or plan.primary.model != EXPERIMENT_MODEL or self.stage in self.calls:
            raise ValueError("unexpected model binding or repeated physical stage")
        payload = {
            "model": EXPERIMENT_MODEL,
            "temperature": 0,
            "max_tokens": plan.primary.max_tokens,
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
            "messages": to_wire(request.messages),
        }
        self.log("dispatch", request=payload)
        api = {"status": "UNKNOWN_OUTCOME", "usage": None, "request_ms": 0.0}
        self.calls[self.stage] = api
        start = time.perf_counter()
        try:
            response = await self.client.post(
                self.endpoint,
                headers={"Authorization": "Bearer " + self.key},
                json=payload,
                follow_redirects=False,
            )
        except httpx.RequestError:
            api["request_ms"] = (time.perf_counter() - start) * 1000
            self.log("outcome", api=api)
            raise CallFailure from None
        api["request_ms"] = (time.perf_counter() - start) * 1000
        if not response.is_success:
            api.update(status="PROVIDER_ERROR", http_status=response.status_code)
            self.log("outcome", api=api)
            raise CallFailure
        try:
            body = _strict_json_loads(response.content)
            api["usage"] = _observed_usage(body.get("usage"))
            if body.get("model") != EXPERIMENT_MODEL:
                api["status"] = "MODEL_IDENTITY_ERROR"
                raise CallFailure
            choice = body["choices"][0]
            message = choice["message"]
            content = message["content"]
            if type(content) is not str or message.get("tool_calls"):
                raise ValueError("unexpected output modality")
            api.update(
                status="RECEIVED", content=content, finish_reason=choice.get("finish_reason")
            )
            return ModelResponse(content, "openai-compatible", EXPERIMENT_MODEL)
        except (AttributeError, KeyError, IndexError, TypeError, ValueError):
            api["status"] = "INVALID_OUTPUT"
            raise CallFailure from None
        finally:
            self.log("outcome", api=api)

    def parsed(self, *, status, **fields):
        self.calls[self.stage].update(status=status, **fields)
        self.log("validated", api=self.calls[self.stage])


def prior_capture(captures, answers):
    if [c["query_id"] for c in captures] != [a["query_id"] for a in answers]:
        raise ValueError("prior answer questions differ")
    for capture, answer in zip(captures, answers, strict=True):
        source = capture["arms"]["set_rank5"]
        if not source["admission_verified"]:
            raise ValueError("unverified source capture")
        if to_wire(from_wire(source["messages"])) != source["messages"]:
            raise ValueError("prior request wire representation changed")
        if fatal(answer["arms"]["set_rank5"]["api"]):
            raise ValueError("ambiguous historical request cannot be reused")


async def run(args):
    if args.output.exists():
        raise ValueError("refusing an existing reader experiment output")
    captures = [
        json.loads(s)
        for s in (args.prepared / "prepared.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    previous = [
        json.loads(s) for s in (args.answers / "run.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    meta = json.loads((args.prepared / "meta.json").read_text(encoding="utf-8"))
    previous_meta = json.loads((args.answers / "meta.json").read_text(encoding="utf-8"))
    previous_cfg = previous_meta["protocol"]["answers"]
    if (
        Path(previous_meta["prepared"]).resolve() != args.prepared.resolve()
        or previous_cfg["model"] != EXPERIMENT_MODEL
        or previous_cfg["max_output_tokens"] != 256
        or previous_cfg["temperature"] != 0
        or previous_cfg["thinking"] != "disabled"
    ):
        raise ValueError("historical reuse requires the identical original preparation/config")
    done = json.loads((args.prepared / "completed.json").read_text(encoding="utf-8"))
    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    if not captures or not len(captures) == meta["queries"] == done["expected"]:
        raise ValueError("full fixed source capture required")
    if len({c["query_id"] for c in captures}) != len(captures):
        raise ValueError("duplicate reader questions")
    prior_capture(captures, previous)
    prior_by_id = {r["query_id"]: r for r in previous}
    plans = {
        n: replace(
            _spec({}).model_plan, primary=replace(_spec({}).model_plan.primary, max_tokens=n)
        )
        for n in (256, 512, 768)
    }
    for row in captures:
        request = ModelRequest(messages=from_wire(row["arms"]["set_rank5"]["messages"]))
        visible_records(request)
        admit_reader_request(request, plans[768])
    args.output.mkdir(parents=True)
    dump(
        args.output / "meta.json",
        {
            "protocol": protocol,
            "prepared": str(args.prepared),
            "prior_answers": str(args.answers),
            "profiles": PROFILES,
            "queries": len(captures),
            "dataset": meta["dataset"],
            "split": meta["split"],
            "scope": __doc__,
            "prior_control": (
                "original direct256 outcomes reused after exact message check; "
                "not contemporaneous primary"
            ),
        },
    )
    dump(
        args.output / "label-access.json",
        {
            "scope": "exposed DEV, aliases for scoring only",
            "dataset": meta["dataset"],
            "split": meta["split"],
            "queries": len(captures),
        },
    )
    endpoint, key = _credentials()
    stop, results = asyncio.Event(), {}
    jobs = [(r, p) for r in captures for p in PROFILES[1:]]
    random.Random(protocol["seed"]).shuffle(jobs)
    first = (captures[0], "attribute512_answer256")
    jobs.remove(first)
    jobs.insert(0, first)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    async with httpx.AsyncClient(
        timeout=30, transport=httpx.AsyncHTTPTransport(retries=0)
    ) as client:
        with (args.output / "api-events.jsonl").open("x", encoding="utf-8") as events:
            iterator = iter(jobs)

            async def execute(row, profile):
                source = row["arms"]["set_rank5"]
                original_request = ModelRequest(messages=from_wire(source["messages"]))
                gateway = RecordedGateway(client, endpoint, key, events, row["query_id"], profile)
                preparation, quotes, request = "NOT_REQUIRED", [], original_request
                started = time.perf_counter()
                api = None
                if profile == "attribute512_answer256":
                    gateway.stage = "attribute"
                    try:
                        prepared = await AttributedEvidenceReader(
                            gateway, plans[512], plans[256]
                        ).prepare(CONTEXT, original_request, instruction_index=1)
                        # Annotation adds substrings; original source records remain identical.
                        _, old_payload, old_records = visible_records(original_request)
                        final_payload = strict_object(
                            next(
                                m.content
                                for m in prepared.answer_request.messages
                                if m.role == "tool"
                            )
                        )
                        clean = [
                            {k: v for k, v in r.items() if k != "reading_quotes"}
                            for r in final_payload["data"]["results"]
                        ]
                        if (
                            clean != old_records
                            or final_payload["data"]["evidence_refs"]
                            != old_payload["data"]["evidence_refs"]
                        ):
                            raise ValueError("reader changed original source fragments")
                        preparation = "VALID"
                        request, quotes = (
                            prepared.answer_request,
                            [asdict(q) for q in prepared.quotes],
                        )
                        gateway.parsed(status="VALID")
                    except (ValueError, CallFailure):
                        stage = gateway.calls.get(
                            "attribute",
                            {"status": "BUDGET_FALLBACK", "usage": None, "request_ms": 0},
                        )
                        if stage["status"] == "RECEIVED":
                            gateway.parsed(status="INVALID_SPANS")
                        preparation = stage["status"]
                        if fatal(stage):
                            stop.set()
                            api = dict(stage)
                        else:
                            # Exact old256 request; reuse its observed outcome, including errors.
                            api = {
                                **prior_by_id[row["query_id"]]["arms"]["set_rank5"]["api"],
                                "reused_from_prior256": True,
                            }
                if api is None:
                    gateway.stage = "answer" if profile != "direct768" else "direct"
                    plan = plans[256] if profile != "direct768" else plans[768]
                    try:
                        response = await gateway.generate_resolved(
                            CONTEXT, plan, admit_reader_request(request, plan)
                        )
                        parsed = _parse_answer(response.content)
                        gateway.parsed(status="VALID", **parsed)
                    except (ValueError, CallFailure):
                        stage = gateway.calls.get(gateway.stage)
                        if stage is not None and stage["status"] == "RECEIVED":
                            gateway.parsed(status="INVALID_OUTPUT")
                    api = gateway.calls.get(
                        gateway.stage, {"status": "BUDGET_FALLBACK", "usage": None, "request_ms": 0}
                    )
                    if fatal(api):
                        stop.set()
                results[(row["query_id"], profile)] = {
                    "api": api,
                    "attribution_status": preparation,
                    "quotes": quotes,
                    "calls": gateway.calls,
                    "pipeline_ms": (time.perf_counter() - started) * 1000,
                    "answer_messages": to_wire(request.messages),
                    "source_fragments_unchanged": True,
                }
                invalid = sum(a["api"]["status"] == "INVALID_OUTPUT" for a in results.values())
                if invalid > max(3, int(len(jobs) * 0.05)):
                    stop.set()

            async def worker():
                while not stop.is_set():
                    job = next(iterator, None)
                    if job is None:
                        return
                    await execute(*job)

            await execute(*next(iterator))
            first_result = results[(first[0]["query_id"], first[1])]
            if (
                first_result["attribution_status"] != "VALID"
                or first_result["api"]["status"] != "VALID"
            ):
                stop.set()
            await asyncio.gather(*(worker() for _ in range(protocol["workers"])))
    with (args.output / "run.jsonl").open("x", encoding="utf-8") as output:
        for row in captures:
            original = row["arms"]["set_rank5"]
            arms = {}
            for profile in PROFILES:
                if profile == "direct256_prior":
                    result = {
                        "api": {
                            **prior_by_id[row["query_id"]]["arms"]["set_rank5"]["api"],
                            "historical_reuse": True,
                        },
                        "attribution_status": "NOT_REQUIRED",
                        "quotes": [],
                        "calls": {},
                        "pipeline_ms": None,
                        "source_fragments_unchanged": True,
                    }
                else:
                    result = results.get(
                        (row["query_id"], profile),
                        {
                            "api": {"status": "NOT_ATTEMPTED", "usage": None, "request_ms": None},
                            "attribution_status": "NOT_ATTEMPTED",
                            "quotes": [],
                            "calls": {},
                            "pipeline_ms": None,
                            "source_fragments_unchanged": False,
                        },
                    )
                valid = result["api"]["status"] == "VALID"
                arms[profile] = {
                    **result,
                    **answer_metrics(result["api"]["answer"] if valid else "", row["answers"]),
                    "citation": citation_metrics(
                        result["api"]["cited_chunk_ids"] if valid else [],
                        original["admitted_chunk_ids"],
                    ),
                    "evidence_recall": original["stages"]["final_admitted_context"][
                        "best_evidence_recall"
                    ],
                    "complete_evidence_set": original["stages"]["final_admitted_context"][
                        "complete_evidence_set"
                    ],
                    "full_text_evidence_recall": original["stages"]["final_admitted_context"][
                        "full_text_evidence_recall"
                    ],
                    "final_evidence_bytes": original["final_evidence_bytes"],
                }
            output.write(
                compact(
                    {
                        "query_id": row["query_id"],
                        "group_id": row["group_id"],
                        "hops": row["hops"],
                        "arms": arms,
                    }
                )
                + "\n"
            )
    complete = not stop.is_set() and len(results) == len(jobs)
    dump(
        args.output / "completed.json",
        {
            "expected": len(captures),
            "queries": len(captures),
            "run_state": "COMPLETE" if complete else "NEEDS_ATTENTION",
            "completed_profiles": len(results),
            "physical_calls": sum(len(r["calls"]) for r in results.values()),
            "prior_control_reuses": len(captures),
            "candidate_fallback_reuses": sum(
                bool(r["api"].get("reused_from_prior256")) for r in results.values()
            ),
        },
    )
    print(
        compact(
            {
                "questions": len(captures),
                "complete": complete,
                "physical_calls": sum(len(r["calls"]) for r in results.values()),
            }
        )
    )
    if not complete:
        raise SystemExit(2)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("prepared", "answers", "protocol", "output"):
        p.add_argument("--" + name, type=Path, required=True)
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
