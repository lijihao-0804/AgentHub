"""Actual API relevance selection over a frozen, authorized public candidate run."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import httpx
from dotenv import dotenv_values
from sqlalchemy import select

from benchmarks.public_rag.budget_ablation import exposure
from benchmarks.public_rag.data import load_scifact
from benchmarks.public_rag.onyx_selection_adapter import (
    SYSTEM_PROMPT,
    build_sections,
    model_sections,
    parse_selection,
    promote_selection,
)
from benchmarks.public_rag.parent_rerank_runner import verified_parents
from benchmarks.public_rag.scifact_runner import (
    COLLECTION,
    DATA,
    DATABASE,
    authorized_context,
    dump,
    retrieval_identity,
    settings,
)
from packages.agent_runtime.context_budget import (
    ContextBudgetConfig,
    ContextBudgetPolicy,
    ContextCategory,
)
from packages.core.database import create_database
from packages.knowledge.models import DocumentChunk, KnowledgeSnapshotItem
from packages.knowledge.snapshots import KnowledgeSnapshotService
from packages.model_gateway.contracts import ModelCapabilities, ModelMessage

MODEL = "deepseek-v4.1-flash-1017"


def admitted_request(query, sections):
    """Admit all source evidence or use an explicit no-API baseline fallback."""
    messages = (
        ModelMessage("system", SYSTEM_PROMPT),
        ModelMessage(
            "user",
            json.dumps(
                model_sections(sections), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        ),
        ModelMessage("user", "User query:\n" + query),
    )
    policy = ContextBudgetPolicy(
        profiles=(
            SimpleNamespace(
                max_tokens=256, capabilities=ModelCapabilities(max_context_tokens=32768)
            ),
        ),
        config=ContextBudgetConfig(
            reserved_output_tokens=256, max_retrieval_tokens=28000, max_tool_result_tokens=0
        ),
    )
    admitted = policy.admit(
        messages,
        categories=(
            ContextCategory.SYSTEM_PROMPT,
            ContextCategory.RAG_EVIDENCE,
            ContextCategory.CURRENT_USER_TASK,
        ),
    )
    usage = admitted.usage
    budget = {
        name: getattr(usage, name)
        for name in (
            "context_limit",
            "reserved_output",
            "available_input",
            "estimated_input_before",
            "estimated_input_after",
            "dropped_exchange_count",
        )
    }
    budget["truncated_by_category"] = {str(k): v for k, v in usage.truncated_by_category.items()}
    preserved = (
        len(admitted.messages) == 3
        and admitted.messages[0] == messages[0]
        and admitted.messages[2] == messages[2]
    )
    if preserved:
        try:
            preserved = json.loads(admitted.messages[1].content) == model_sections(sections)
        except (TypeError, ValueError):
            preserved = False
    budget["evidence_semantically_preserved"] = preserved
    if not preserved:
        return None, budget
    return {
        "model": MODEL,
        "temperature": 0,
        "max_tokens": 256,
            "thinking": {"type": "disabled"},
        "response_format": {"type": "json_object"},
        "messages": [{"role": m.role, "content": m.content} for m in admitted.messages],
    }, budget


def observed_usage(value):
    if not isinstance(value, dict):
        return None
    output = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens"):
        item = value.get(key)
        if type(item) is int and item >= 0:
            output[key] = item
    for key in ("prompt_tokens_details", "completion_tokens_details"):
        if isinstance(value.get(key), dict):
            output[key] = {
                name: item for name, item in value[key].items() if type(item) is int and item >= 0
            }
    return output or None


async def select_once(client, url, key, query, sections, baseline, *, on_attempt=None):
    request, budget = admitted_request(query, sections)
    result = {
        "baseline_ranking": baseline,
        "final_ranking": baseline,
        "section_source_ids": [s["source_id"] for s in sections],
        "section_chunk_ids": [s["chunk_ids"] for s in sections],
        "selection": [],
        "status": "BUDGET_FALLBACK",
        "attempted": False,
        "request_ms": 0.0,
        "usage": None,
        "budget": budget,
        "http_status": None,
    }
    if request is None:
        return result, False
    result["attempted"] = True
    if on_attempt is not None:
        on_attempt()
    start = time.perf_counter()
    try:
        response = await client.post(url, headers={"Authorization": "Bearer " + key}, json=request)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        result.update(status="UNKNOWN_OUTCOME", error_type=type(exc).__name__)
        return result, False
    finally:
        result["request_ms"] = (time.perf_counter() - start) * 1000
    result["http_status"] = response.status_code
    if response.status_code != 200:
        result["status"] = "HTTP_ERROR"
        # Stop issuing new requests on any HTTP error; await in-flight responses.
        return result, True
    try:
        body = response.json()
        result["usage"] = observed_usage(body.get("usage"))
        response_model = body.get("model")
        result["response_model_verified"] = response_model == MODEL
        if response_model is not None and response_model != MODEL:
            result.update(status="HTTP_ERROR", error_type="RESPONSE_MODEL_MISMATCH")
            return result, True
        ids = parse_selection(body["choices"][0]["message"]["content"], count=len(sections))
        result.update(
            status="VALID",
            selection=list(ids),
            final_ranking=promote_selection(baseline, sections, ids),
        )
    except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
        result.update(status="INVALID_OUTPUT", error_type=type(exc).__name__)
    return result, False


async def authorized_chunks(state, source):
    config = settings()
    if (
        state["database"] != DATABASE
        or state["collection"] != COLLECTION
        or state["retrieval_identity"] != retrieval_identity(config)
    ):
        raise ValueError("only the isolated prepared benchmark is permitted")
    engine, factory = create_database(config.database_url)
    try:
        async with factory() as session:
            context = await authorized_context(session, state["user_id"], state["workspace_id"])
            snapshot = await KnowledgeSnapshotService().resolve_snapshot(
                session, context, UUID(state["knowledge_base_id"]), state["snapshot_id"]
            )
            if snapshot.content_hash != state["snapshot_hash"]:
                raise ValueError("frozen snapshot differs")
            rows = list(
                await session.scalars(
                    select(DocumentChunk)
                    .join(
                        KnowledgeSnapshotItem,
                        (KnowledgeSnapshotItem.workspace_id == DocumentChunk.workspace_id)
                        & (
                            KnowledgeSnapshotItem.knowledge_base_id
                            == DocumentChunk.knowledge_base_id
                        )
                        & (KnowledgeSnapshotItem.document_id == DocumentChunk.document_id)
                        & (
                            KnowledgeSnapshotItem.document_revision_id
                            == DocumentChunk.document_revision_id
                        ),
                    )
                    .where(
                        DocumentChunk.workspace_id == UUID(state["workspace_id"]),
                        DocumentChunk.knowledge_base_id == UUID(state["knowledge_base_id"]),
                        KnowledgeSnapshotItem.snapshot_id == UUID(state["snapshot_id"]),
                    )
                )
            )
            verified_parents(rows, state, source.corpus)
            return {row.chunk_id: row for row in rows}
    finally:
        await engine.dispose()


async def run(args):
    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    state = json.loads(args.state.read_text(encoding="utf-8"))
    expected = {
        "model": MODEL,
        "temperature": 0,
        "max_output_tokens": 256,
        "concurrency": 4,
        "timeout_seconds": 120,
        "request_attempts_per_query": 1,
        "max_selected_sections": 10,
        "max_chunks_per_section": 3,
        "thinking": {"type": "disabled"},
        "snapshot_id": state["snapshot_id"],
    }
    if any(protocol.get(k) != v for k, v in expected.items()):
        raise ValueError("protocol differs from fixed API experiment")
    args.output.mkdir(parents=True, exist_ok=True)
    path = args.output / "run.jsonl"
    if path.exists() or path.with_suffix(".meta.json").exists():
        raise ValueError("no overwriting or automatic replay of attempted API calls")
    values = dotenv_values(".env")
    key = os.getenv("AGENTHUB_RAG_API_KEY") or values.get("AGENTHUB_RAG_API_KEY")
    base = os.getenv("AGENTHUB_RAG_BASE_URL") or values.get("AGENTHUB_RAG_BASE_URL")
    if not key or not base:
        raise ValueError("configured RAG experiment API credentials unavailable")
    # Never persist or print the configured endpoint or authorization value.
    url = base.rstrip("/") + "/chat/completions"
    exposure(
        args.output,
        "ONYX_SELECTION_DEV_PREPARE_LABEL_ACCESS"
        if args.prepare_only
        else "ONYX_SELECTION_DEV_API_LABEL_ACCESS",
        protocol=str(args.protocol),
    )
    source = load_scifact(args.data, "train")
    baseline_path = Path(protocol["input_run"])
    meta = json.loads(baseline_path.with_suffix(".meta.json").read_text(encoding="utf-8"))
    if (
        meta["snapshot_id"] != state["snapshot_id"]
        or meta["arm"] != "baseline"
        or meta["queries"] != 809
        or meta["split"] != "train"
        or meta["retrieval_identity"] != state["retrieval_identity"]
    ):
        raise ValueError("baseline capture is not the complete fixed DEV run")
    rows = [json.loads(line) for line in baseline_path.read_text(encoding="utf-8").splitlines()]
    indexed = {row["query_id"]: row for row in rows}
    if len(rows) != 809 or len(indexed) != 809 or set(indexed) != set(source.qrels):
        raise ValueError("baseline and dataset must cover all 809 official queries")
    for row in rows:
        final = row["stages"]["rerank"]["ranking"]
        selected = row["stages"]["selection"]["ranking"]
        if (
            len(final) != 20
            or len(selected) != 20
            or ({hit["chunk_id"] for hit in final} != {hit["chunk_id"] for hit in selected})
        ):
            raise ValueError("baseline must retain exactly the authorized20 selected children")
    chunks = await authorized_chunks(state, source)
    titles = {key: doc.title for key, doc in source.corpus.items()}
    query_ids = sorted(source.queries)
    random.Random(protocol["seed"]).shuffle(query_ids)
    if args.prepare_only:
        target = args.output / "preflight.json"
        if target.exists():
            raise ValueError("refusing to overwrite historical preflight")
        admissions, sizes, section_counts = [], [], []
        for qid in query_ids:
            baseline = indexed[qid]["stages"]["rerank"]["ranking"]
            sections = build_sections(
                baseline, chunks=chunks, source_mapping=state["chunk_sources"], titles=titles
            )
            request, budget = admitted_request(source.queries[qid], sections)
            admissions.append(request is not None)
            sizes.append(budget["estimated_input_before"])
            section_counts.append(len(sections))
        dump(
            target,
            {
                "protocol": protocol,
                "queries": 809,
                "snapshot_scope_valid": True,
                "admitted_queries": sum(admissions),
                "budget_fallback_queries": 809 - sum(admissions),
                "max_input_byte_units": max(sizes),
                "min_sections": min(section_counts),
                "max_sections": max(section_counts),
                "generation_api_calls": 0,
                "qualifies_for_api_run": sum(admissions) / 809 >= 0.95,
            },
        )
        print(json.dumps({"preflight_admitted": sum(admissions), "api_calls": 0}))
        return
    run_meta = {
        "protocol": protocol,
        "queries": 809,
        "split": "train",
        "snapshot_id": state["snapshot_id"],
        "model": MODEL,
        "pairing": "cached baseline with actual incremental model calls",
        "generation_task": "retrieval selection only",
        "started_at": datetime.now(UTC).isoformat(),
    }
    dump(path.with_suffix(".meta.json"), run_meta)
    stop = asyncio.Event()
    completed, unknown, unknown_streak = 0, 0, 0
    logging.getLogger("httpx").setLevel(logging.CRITICAL)
    logging.getLogger("httpcore").setLevel(logging.CRITICAL)
    with (
        path.open("x", encoding="utf-8") as handle,
        (args.output / "request-events.jsonl").open("x", encoding="utf-8") as events,
    ):

        def event(kind, qid, **fields):
            events.write(
                json.dumps(
                    {"event": kind, "query_id": qid, "at": datetime.now(UTC).isoformat(), **fields}
                )
                + "\n"
            )
            events.flush()

        async with httpx.AsyncClient(
            timeout=120,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
        ) as client:

            async def process(ordinal, qid):
                nonlocal completed, unknown, unknown_streak
                baseline = indexed[qid]["stages"]["rerank"]["ranking"]
                sections = build_sections(
                    baseline, chunks=chunks, source_mapping=state["chunk_sources"], titles=titles
                )
                event("REQUEST_PREPARED", qid, attempt=1)
                result, fatal = await select_once(
                    client,
                    url,
                    key,
                    source.queries[qid],
                    sections,
                    baseline,
                    on_attempt=lambda: event("REQUEST_STARTED", qid, attempt=1),
                )
                result.update(query_id=qid, ordinal=ordinal)
                handle.write(json.dumps(result) + "\n")
                handle.flush()
                completed += 1
                unknown += result["status"] == "UNKNOWN_OUTCOME"
                unknown_streak = (
                    unknown_streak + 1
                    if result["status"] in {"UNKNOWN_OUTCOME", "INVALID_OUTPUT"}
                    else 0
                )
                event(
                    "QUERY_COMPLETED", qid, status=result["status"], attempted=result["attempted"]
                )
                if fatal or unknown_streak >= 3:
                    stop.set()
                if completed == 1 or completed % 25 == 0 or completed == 809:
                    print(f"selection query {completed}/809", flush=True)
                return result

            # First official query also checks the configured API; no extra probe or retry.
            initial = await process(0, query_ids[0])
            if initial["status"] in {"UNKNOWN_OUTCOME", "INVALID_OUTPUT"}:
                stop.set()
            queue = asyncio.Queue()
            for ordinal, qid in enumerate(query_ids[1:], 1):
                queue.put_nowait((ordinal, qid))

            async def worker():
                while not stop.is_set():
                    try:
                        ordinal, qid = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    await process(ordinal, qid)

            await asyncio.gather(*(worker() for _ in range(4)))
    run_meta.update(
        completed_queries=completed,
        unknown_outcomes=unknown,
        run_state="NEEDS_ATTENTION" if unknown else ("STOPPED" if stop.is_set() else "COMPLETED"),
        finished_at=datetime.now(UTC).isoformat(),
    )
    dump(path.with_suffix(".meta.json"), run_meta)
    if stop.is_set() or completed != 809:
        raise RuntimeError(
            "API run stopped; partial captures retained and requests are not retried"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--data", type=Path, default=DATA)
    parser.add_argument("--prepare-only", action="store_true")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
