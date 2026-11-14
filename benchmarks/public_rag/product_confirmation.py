"""Run the frozen B/C/D technical comparison, preserving every first dispatch."""

import argparse
import asyncio
import json
import os
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path
from time import perf_counter

from benchmarks.public_rag.confirmation_cases import OUT, ROOT, digest, read, save
from benchmarks.public_rag.confirmation_meter import MODEL, AliyunMeter
from benchmarks.public_rag.consumer_replay import CONTEXT, _spec
from benchmarks.public_rag.fiqa_confirmation import _credentials
from packages.agent_runtime.context_budget import ContextCategory
from packages.knowledge import citation_qa as qa
from packages.knowledge.contracts import RetrievedEvidence
from packages.knowledge.evidence_set import EvidenceSetSelector
from packages.model_gateway.adapters.litellm.adapter import _normalize_response
from packages.model_gateway.adapters.litellm.provider_http import guarded_sdk_client
from packages.model_gateway.contracts import ModelCapabilities, ModelMessage, ModelRequest
from packages.model_gateway.egress import ProviderEgressPolicy


def plan(max_tokens):
    base = _spec({}).model_plan
    return replace(
        base,
        primary=replace(
            base.primary,
            max_tokens=max_tokens,
            capabilities=ModelCapabilities(max_context_tokens=65536),
        ),
    )


class Gateway:
    def __init__(self, ledger, stage, identity, events):
        self.meter = AliyunMeter(ledger, stage)
        self.meter.task_id = identity
        self.events = events

    async def generate_resolved(self, context, selected_plan, request):
        endpoint, key = _credentials()
        base = endpoint.removesuffix("/chat/completions")
        payload = [dict(role=m.role, content=m.content) for m in request.messages]
        self.events.append({"stage": self.meter.stage, "messages": payload})
        async with guarded_sdk_client(
            "openai-compatible", base, key, 90, policy=ProviderEgressPolicy()
        ) as bound:
            raw = await self.meter.acompletion(
                client=bound.client,
                api_base=bound.base_url,
                model=MODEL,
                messages=payload,
                max_tokens=selected_plan.primary.max_tokens,
                temperature=0,
                num_retries=0,
                extra_body={"enable_thinking": False},
            )
        self.events[-1].update(
            model=raw.model,
            id=raw.id,
            usage=raw.usage.model_dump(),
            raw_message=raw.choices[0].message.model_dump() if raw.choices else None,
            finish_reason=raw.choices[0].finish_reason if raw.choices else None,
        )
        return _normalize_response(raw, provider="openai-compatible", model=MODEL)


async def evaluate(case, arm, gateway):
    candidates = [RetrievedEvidence(**r) for r in case["pool"]]
    if arm == "D":
        candidates = [
            replace(r, rerank_score=s)
            for r, s in zip(candidates, case["sentence_scores"], strict=True)
        ]
    candidates.sort(key=lambda r: (-r.rerank_score, -r.retrieval_score, r.chunk_id))
    ranked = candidates
    if arm == "C":
        result = await EvidenceSetSelector(gateway("selector"), plan(256)).select(
            CONTEXT, case["query"], tuple(candidates), max_items=6
        )
        ranked = result.rank_order
    evidence = qa._select_evidence(tuple(ranked[:6]), max_chars=6000)
    visible = [dict(number=r.id, **asdict(r.evidence)) for r in evidence]
    if not evidence:
        return dict(
            answer=qa._INSUFFICIENT_EVIDENCE,
            insufficient=True,
            citations=[],
            visible=visible,
            status="ok",
        )
    messages = (
        ModelMessage("system", qa._PROMPT_JSON_SYSTEM),
        ModelMessage("user", qa._evidence_prompt(case["query"], evidence)),
    )
    qa._deny_oversized(
        plan(2048),
        messages,
        (ContextCategory.SYSTEM_PROMPT, ContextCategory.RAG_EVIDENCE),
        response_schema=None,
    )
    try:
        result = await gateway("answer").generate_resolved(
            CONTEXT, plan(2048), ModelRequest(messages=messages)
        )
    except qa.ModelGatewayError as exc:
        return dict(status=exc.code.value, visible=visible)
    try:
        if not result.content.strip():
            return dict(status="gateway_empty_output", visible=visible)
        output = qa._parse_model_output(
            result.content, output_limit_reached=result.finish_reason == "length"
        )
        ids = qa._validate_model_output(output, evidence_count=len(evidence))
        answer, insufficient, ids = qa._normalize_validated_answer(
            case["query"], output, ids, contextual=False
        )
        return dict(
            answer=answer,
            insufficient=insufficient,
            citations=list(ids),
            visible=visible,
            status="ok",
        )
    except qa._InvalidCitationAnswer as exc:
        return dict(status=exc.reason.value, visible=visible)


def verify_inputs(pools):
    manifest = read(OUT / "preflight.json")
    if digest(pools.read_bytes().replace(b"\r\n", b"\n")) != manifest["pools_sha256"]:
        raise ValueError("Frozen shared pools differ")
    for name, expected in manifest["publication_sha256"].items():
        if digest((OUT / name).read_bytes().replace(b"\r\n", b"\n")) != expected:
            raise ValueError(f"Frozen protocol input differs: {name}")
    cases = read(pools)["cases"]
    definitions = read(OUT / "cohort.json")["cases"]
    if not (
        len(cases) == len(definitions) == 40
        and all(
            all(c[k] == d[k] for k in ("id", "query", "schedule")) and len(c["pool"]) == 20
            for c, d in zip(cases, definitions, strict=True)
        )
    ):
        raise ValueError("Frozen schedule or pool cardinality differs")
    return cases


async def run(pools, ledger, directory):
    cases = verify_inputs(pools)
    directory.mkdir(parents=True, exist_ok=False)
    with closing(sqlite3.connect(f"{ledger.resolve().as_uri()}?mode=rw", uri=True)) as db:
        if db.execute("SELECT count(*) FROM wires WHERE stage IN ('answer','selector')").fetchone()[
            0
        ]:
            raise ValueError("Technical confirmation has already been opened")
        db.execute("CREATE TABLE IF NOT EXISTS study_opening(name TEXT PRIMARY KEY)")
        db.execute("INSERT INTO study_opening VALUES ('public_bcd')")
        db.commit()
    save(
        directory / "identity.json",
        {
            "head": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            ).strip(),
            "pools_sha256": digest(pools.read_bytes()),
            "model": MODEL,
            "endpoint": "Beijing /compatible-mode/v1/chat/completions",
        },
    )
    with (directory / "events.jsonl").open("x", encoding="utf8") as stream:

        def log(row):
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

        for c in cases:
            for arm in c["schedule"]:
                task = c["id"] + "/" + arm
                record = {"task": task, "id": c["id"], "arm": arm, "events": []}
                log({"dispatch_scheduled": task})
                start = perf_counter()
                try:
                    record.update(
                        await evaluate(
                            c,
                            arm,
                            lambda stage, task=task, events=record["events"]: Gateway(
                                ledger, stage, task, events
                            ),
                        )
                    )
                except (ValueError, qa.ModelGatewayError) as exc:
                    # Already settled selector malformed JSON or selection contract violations.
                    record.update(status="selection_or_admission_error", reason=type(exc).__name__)
                except Exception as exc:
                    record.update(status="stopped", reason=type(exc).__name__)
                    raise
                finally:
                    record["model_and_assembly_ms"] = 1000 * (perf_counter() - start)
                    record["total_ms"] = (
                        record["model_and_assembly_ms"]
                        + c["whole_pipeline_ms"]
                        + (c["sentence_extra_ms"] if arm == "D" else 0)
                    )
                    log(record)
    save(directory / "completed.json", {"planned": 120, "first_responses_recorded": 120})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pools", type=Path, required=True)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.ledger is None and args.output is None:
        print(f"{len(verify_inputs(args.pools))} paired tasks verified, zero paid dispatches")
    elif args.ledger and args.output:
        asyncio.run(run(args.pools, args.ledger, args.output))
    else:
        parser.error("paid execution requires both --ledger and a fresh --output")


if __name__ == "__main__":
    main()
