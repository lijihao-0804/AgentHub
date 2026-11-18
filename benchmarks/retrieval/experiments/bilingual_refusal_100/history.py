"""Replay archived completions or dispatch one metered candidate on the protected questions."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import subprocess
from contextlib import nullcontext
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

import httpx
from qdrant_client import QdrantClient
from sqlalchemy.engine import make_url

from apps.api.app import create_app
from apps.api.knowledge_dependencies import (
    get_agent_run_workspace_context,
    get_ingestion_queue,
    get_query_workspace_context,
    get_retrieval_components,
    get_workspace_context,
)
from apps.worker.tasks.knowledge import _load_chunks, _process_knowledge_ingestion, _vector_records
from benchmarks.retrieval.experiments.first_refusal_100 import attempts
from benchmarks.retrieval.experiments.first_refusal_100.dataset import HERE as OLD
from benchmarks.retrieval.experiments.first_refusal_100.dataset import load_cases, score_case
from benchmarks.retrieval.experiments.first_refusal_100.runner import (
    EMBED,
    MODEL,
    RERANK,
    SCRATCH,
    file_hash,
    model_hashes,
    observe_evidence,
    save,
)
from benchmarks.retrieval.revision_reuse import InlineQueue, arm_context, post
from benchmarks.retrieval.runner import _context
from benchmarks.retrieval.table_header_answers import receipt_cost, write_entry
from benchmarks.retrieval.table_header_cloud_qa import CloudTableMeter
from benchmarks.retrieval.table_header_local_qa import model_profile, score
from benchmarks.retrieval.table_headers import PROTOCOL
from packages.control_plane.models import Organization, Workspace
from packages.core.config.settings import Settings
from packages.core.database import create_database
from packages.knowledge import citation_qa
from packages.knowledge.adapters.embeddings import BgeM3DenseEmbedder
from packages.knowledge.adapters.qdrant import QdrantVectorIndex
from packages.knowledge.adapters.reranker import BgeReranker
from packages.knowledge.adapters.sparse import BgeM3SparseEncoder
from packages.knowledge.composition import RetrievalComponents
from packages.knowledge.models import KnowledgeSnapshot
from packages.model_gateway.adapters.litellm.provider_http import GuardedProviderTransport
from packages.model_gateway.credentials import ProviderCredentialCipher
from scripts.aliyun_api_smoke import check_url

from .policy import expected_prompt

ARCHIVE = SCRATCH / "first-refusal-100-20261118"
WAI_PAGE = SCRATCH / "rag-table-wai-20261118/irregular.html"
FIXES = {"R005", "R027", "R089"}


def rows(path):
    return [json.loads(line) for line in path.read_bytes().splitlines()]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def previous(group):
    """Bind archive bytes to the preceding report, then select its single candidate arm."""
    audit = json.loads((OLD / "results/billing.json").read_bytes())
    if group == "old100":
        directory = ARCHIVE / "independent"
        execution = json.loads((directory / "execution.json").read_bytes())
        for name in ("execution.json", "freeze.json"):
            if file_hash(directory / name) != audit["file_sha256"]["independent/" + name]:
                raise ValueError("Historical independent archive has changed.")
        for name, field in (
            ("wire-private.jsonl", "wire_sha256"),
            ("http-private.jsonl", "http_sha256"),
        ):
            if file_hash(directory / name) != execution[field]:
                raise ValueError("Historical independent response digest differs.")
        freeze = json.loads((directory / "freeze.json").read_bytes())
        cases = sorted(load_cases(), key=lambda c: c["id"])
        if len(cases) != 100 or attempts.digest_json(cases) != freeze["cases_sha256"]:
            raise ValueError("Historical case registration differs.")
        cases.sort(key=lambda c: (c["id"] not in FIXES, c["id"]))
        wire = {e["key"]: e for e in rows(directory / "wire-private.jsonl") if e["attempt"] == 1}
        http = {
            e["key"]: e for e in rows(directory / "http-private.jsonl") if e["state"] == "returned"
        }
        prior = {}
        for c in cases:
            key = c["id"] + "/candidate"
            event = wire[key]
            if event["messages"][0]["content"] != citation_qa._PROMPT_JSON_REFUSAL_SYSTEM:
                raise ValueError("Expected the earlier candidate prompt.")
            prior[c["id"]] = {
                "raw": event["content"],
                "finish": event["finish_reason"],
                "user": event["messages"][1]["content"],
                "evidence": http[key]["evidence"],
                "http": http[key]["body"],
            }
        return cases, prior, freeze["inputs"]
    directory = ARCHIVE / "wai-candidate"
    if file_hash(directory / "results.json") != audit["file_sha256"]["wai-candidate/results.json"]:
        raise ValueError("Historical WAI results have changed.")
    results = json.loads((directory / "results.json").read_bytes())
    visible = rows(directory / "visible-attempts.jsonl")
    protocol = json.loads(PROTOCOL.read_bytes())
    cases, prior = [], {}
    for index, (spec, old, event, top5) in enumerate(
        zip(protocol["cases"], results["cases"], visible, results["top5_text_sha256"], strict=True),
        1,
    ):
        question, _, _, value, cell = spec
        if (old["query"], event["query"], old["generated"]["calls"], event["validation"]) != (
            question,
            question,
            1,
            "accepted",
        ) or event["wire_call"] != index:
            raise ValueError("Expected one previously accepted visible answer per WAI question.")
        key = f"W{index:02}"
        cases.append(
            dict(
                id=key,
                doc_id="WAI",
                question=question,
                answerable=value is not None,
                value=value,
                cell=cell,
            )
        )
        prior[key] = {"raw": event["visible_content"], "finish": "stop", "top5": top5}
    if len(cases) != 14:
        raise ValueError("Expected all fourteen WAI questions.")
    return cases, prior, {}


def wai_payload():
    protocol = json.loads(PROTOCOL.read_bytes())
    tables = re.findall(
        r"<table\b[^>]*>.*?</table>", WAI_PAGE.read_text(encoding="utf8"), re.I | re.S
    )
    if [sha(t.encode()) for t in tables] != protocol["table_sha256"]:
        raise ValueError("Only the two registered W3C tables may be uploaded.")
    return json.dumps(
        [{"page_idx": i, "type": "table", "table_body": t} for i, t in enumerate(tables)]
    ).encode()


async def restore_snapshots(factory, components, inputs):
    """Rebuild only the ephemeral vector index, retaining the original PG document and chunk IDs."""
    components.index.ensure_collection()
    workspace_id = None
    async with factory() as session:
        for bound in inputs.values():
            snapshot = await session.get(KnowledgeSnapshot, UUID(bound["snapshot"]))
            if (
                snapshot is None
                or snapshot.content_hash != bound["snapshot_hash"]
                or str(snapshot.knowledge_base_id) != bound["kb"]
                or workspace_id not in (None, snapshot.workspace_id)
            ):
                raise ValueError(
                    "The registered historical snapshot is unavailable or has changed."
                )
            workspace_id = snapshot.workspace_id
            chunks = await _load_chunks(factory, UUID(bound["revision"]))
            if not chunks or any(
                str(c.document_id) != bound["document"]
                or c.workspace_id != workspace_id
                or str(c.knowledge_base_id) != bound["kb"]
                for c in chunks
            ):
                raise ValueError("Historical chunk scope differs.")
            components.index.upsert(
                _vector_records(snapshot, chunks, components.dense, components.sparse)
            )
        workspace = await session.get(Workspace, workspace_id)
        org = await session.get(Organization, workspace.organization_id)
        return _context(
            user_id=org.created_by, organization_id=org.id, workspace_id=workspace_id
        ).model_copy(update={"permissions": frozenset({"knowledge_run"})})


async def upload_wai(client, queue, context, settings, components):
    base = f"/api/v1/workspaces/{context.workspace_id}/knowledge-bases"
    kb = await post(client, base, json={"name": "WAI tables"})
    url = base + "/" + kb["id"]
    body = wai_payload()
    upload = await post(
        client, url + "/documents", files={"file": ("tables.json", body, "application/json")}
    )
    job = queue.jobs.popleft()
    if str(job) != upload["ingestion_job"]["id"]:
        raise ValueError("Ingestion identity differs.")
    await _process_knowledge_ingestion(job, settings, indexing_factory=lambda _: components)
    snap = await post(client, url + "/snapshots")
    if snap["item_count"] != 1:
        raise ValueError("Expected a snapshot of the one source file.")
    return {
        "WAI": dict(
            kb=kb["id"],
            revision=upload["revision"]["id"],
            document=upload["document"]["id"],
            snapshot=snap["id"],
            snapshot_hash=snap["content_hash"],
            source_sha256=sha(body),
        )
    }


def evidence_check(old, records, user):
    if "user" in old:
        if old["user"] != user or old["evidence"] != records:
            raise ValueError("Historical model-facing evidence or user message has changed.")
        return
    top5 = [
        {"page": e["locator"]["page"], "row": e["locator"]["row"], "text": e["text"]}
        for e in records[:5]
    ]
    if sha(json.dumps(top5, sort_keys=True).encode()) != old["top5"]:
        raise ValueError("The five saved WAI rows differ from the rows delivered to the model.")


def replay_provider(selected):
    async def respond(transport, request):
        key, old = selected[0]
        return httpx.Response(
            200,
            json={
                "id": "offline-history-" + key,
                "created": 0,
                "object": "chat.completion",
                "model": MODEL,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": old["raw"]},
                        "finish_reason": old["finish"],
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 25,
                    "total_tokens": 125,
                    "prompt_tokens_details": {"cached_tokens": 0},
                },
            },
        )

    return respond


def guard_candidate_request(registered_sha, observed, events):
    send = GuardedProviderTransport.handle_async_request

    async def checked(transport, request):
        messages = json.loads(request.content)["messages"]
        if (
            len(messages) != (4 if events else 2)
            or messages[0]["role"] != "system"
            or sha(messages[0]["content"].encode()) != registered_sha
            or messages[1] != {"role": "user", "content": observed.get("prompt")}
        ):
            raise ValueError("Unregistered system prompt, evidence or physical attempt.")
        if events and (
            messages[2] != {"role": "assistant", "content": events[0]["content"]}
            or messages[3]["role"] != "user"
            or messages[3]["content"]
            not in (citation_qa._CITATION_CORRECTION, citation_qa._PROMPT_CITATION_CORRECTION)
        ):
            raise ValueError("A correction must carry the original visible answer.")
        return await send(transport, request)

    return checked


def capture_events(stream):
    events = []

    def record(event):
        write_entry(stream, event)
        events.append(event)

    return events, record


def check_archived_evidence(question, old, observed):
    original = citation_qa._evidence_prompt

    def checked(query, items):
        prompt = original(query, items)
        if query != question:
            raise ValueError("Unexpected model-bound question.")
        evidence_check(old, observed["evidence"], prompt)
        return prompt

    return checked


def public_stage(case, result):
    if case["doc_id"] != "WAI":
        flags = attempts.scored(case, result, score_case)
    else:
        valid = result["state"] in {"answer", "refusal"}
        refuse = result["state"] == "refusal"
        value = cited = False
        if valid:
            response = httpx.Response(
                200,
                json={
                    "answer": result["answer"],
                    "insufficient_evidence": refuse,
                    "citations": [
                        dict(excerpt=c["text"], locator=c["locator"]) for c in result["citations"]
                    ],
                },
            )
            old_scores = score(response, case["value"], case["cell"])
            value, cited = old_scores["value_correct"], old_scores["cited_cell"]
        flags = dict(
            zip(
                attempts.SCORE_FLAGS,
                (
                    valid,
                    refuse,
                    value,
                    cited,
                    value and cited,
                    case["answerable"] and refuse,
                    not case["answerable"] and result["state"] == "answer",
                    valid,
                ),
                strict=True,
            )
        )
    return attempts.public_answer(result, flags)


async def run(args):
    directory = args.run_dir.resolve()
    if not directory.is_relative_to(SCRATCH) or directory == SCRATCH:
        raise ValueError(
            "Keep raw archive and live records in a fresh private .scratch subdirectory."
        )
    dsn = os.environ["AGENTHUB_TEST_DATABASE_URL"]
    target = make_url(dsn)
    db_name = "agenthub_test_first_refusal_100" if args.group == "old100" else "agenthub_nov18_rag"
    if target.host not in {"127.0.0.1", "localhost"} or target.database != db_name:
        raise ValueError(f"This group requires the dedicated loopback database {db_name}.")
    cases, previous_rows, inputs = previous(args.group)
    directory.mkdir(parents=True, exist_ok=False)
    os.environ.update(
        HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", LITELLM_LOCAL_MODEL_COST_MAP="True"
    )
    weights = model_hashes(EMBED, RERANK)
    fake = SimpleNamespace(
        provider="openai-compatible",
        model=MODEL,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key="offline-inert",
    )
    meter = CloudTableMeter(
        directory / "receipts.jsonl", max_calls=2 * len(cases), route=fake if args.replay else None
    )
    check_url(meter.route.base_url, provider=True)
    if meter.route.model != MODEL or meter.route.provider != fake.provider:
        raise ValueError("Expected the frozen Beijing provider and registered model.")
    settings = Settings(
        _env_file=None,
        database_url=dsn,
        blob_root=str(directory / "blobs"),
        credential_master_key=secrets.token_urlsafe(32),
        knowledge_qa_question_language_prompt=True,
        knowledge_qa_answer_contract_v1=False,
        knowledge_qa_contextual_abstention=False,
        knowledge_min_rerank_score=None,
        knowledge_vector_cache_enabled=False,
        knowledge_qa_max_evidence_chars=16000,
        knowledge_chunk_size_chars=1200,
        knowledge_chunk_overlap_chars=200,
        knowledge_superseded_rank_penalty=0.0,
    )
    engine, factory = create_database(dsn)
    index = QdrantVectorIndex(
        url="", collection_name="protected", dense_vector_size=1024, client=QdrantClient(":memory:")
    )
    components = RetrievalComponents(
        BgeM3DenseEmbedder(model_name=str(EMBED), device=args.device),
        BgeM3SparseEncoder(model_name=str(EMBED)),
        BgeReranker(model_name=str(RERANK), device=args.device),
        index,
    )
    try:
        context = (
            await restore_snapshots(factory, components, inputs)
            if inputs
            else await arm_context(factory)
        )
        queue, app = InlineQueue(), create_app(settings)
        app.state.db_session_factory, app.state.settings = factory, settings
        app.dependency_overrides.update(
            {
                get_workspace_context: lambda: context,
                get_agent_run_workspace_context: lambda: context,
                get_query_workspace_context: lambda: context,
                get_retrieval_components: lambda: components,
                get_ingestion_queue: lambda: queue,
            }
        )
        profile = await model_profile(
            factory,
            context.workspace_id,
            meter,
            route=meter.route,
            cipher=ProviderCredentialCipher.from_settings(settings),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://benchmark.invalid"
        ) as client:
            if not inputs:
                inputs = await upload_wai(client, queue, context, settings, components)
            freeze = {
                "group": args.group,
                "offline_replay": args.replay,
                "model": MODEL,
                "model_files_sha256": weights,
                "source_commit": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], text=True
                ).strip(),
                "registered_questions": len(cases),
                "cases_sha256": attempts.digest_json(cases),
                "max_wire_calls": meter.max_calls,
                "max_cny": str(meter.budget_cny),
                "inputs": inputs,
                "policy_sha256": {
                    c["id"]: sha(expected_prompt("candidate", c["question"]).encode())
                    for c in cases
                },
                "prior_visible_sha256": {
                    k: sha(v["raw"].encode()) for k, v in previous_rows.items()
                },
            }
            save(directory / "freeze.json", freeze)
            if not args.replay:
                save(
                    SCRATCH / f"bilingual-refusal-protected-{args.group}-paid-20261118.json",
                    {
                        "group": args.group,
                        "freeze_sha256": attempts.digest_json(freeze),
                        "run_directory": str(directory),
                    },
                )
            selected = []
            with (
                patch.object(
                    GuardedProviderTransport, "handle_async_request", replay_provider(selected)
                )
                if args.replay
                else nullcontext(),
                (directory / "wire-private.jsonl").open("x", encoding="utf8") as wire,
                (directory / "http-private.jsonl").open("x", encoding="utf8") as http,
                (directory / "derived.jsonl").open("x", encoding="utf8") as derived,
                meter.measure(),
            ):
                for case in cases:
                    qid, doc_id = case["id"], case["doc_id"]
                    key, question = qid + "/candidate", case["question"]
                    doc, old = inputs[doc_id], previous_rows[qid]
                    events, meter.attempt_observer = capture_events(wire)
                    selected[:] = [(qid, old)]
                    with meter.request_scope(key), observe_evidence(doc, doc_id, None) as observed:
                        with (
                            patch.object(
                                citation_qa,
                                "_evidence_prompt",
                                check_archived_evidence(question, old, observed),
                            ),
                            patch.object(
                                GuardedProviderTransport,
                                "handle_async_request",
                                guard_candidate_request(
                                    freeze["policy_sha256"][qid], observed, events
                                ),
                            ),
                        ):
                            write_entry(
                                http, {"key": key, "state": "started", "snapshot": doc["snapshot"]}
                            )
                            response = await client.post(
                                "/api/v1/knowledge/query",
                                json={
                                    "workspace_id": str(context.workspace_id),
                                    "knowledge_base_id": doc["kb"],
                                    "knowledge_snapshot_id": doc["snapshot"],
                                    "model_profile_id": profile,
                                    "query": question,
                                    "output_mode": "PROMPT_JSON",
                                },
                            )
                    write_entry(
                        http,
                        {
                            "key": key,
                            "state": "returned",
                            "http": response.status_code,
                            "body": response.json(),
                            "evidence": observed.get("evidence", []),
                        },
                    )
                    if meter.halted or meter.outstanding_cny or not events or len(events) > 2:
                        raise RuntimeError(f"Missing or unconfirmed physical attempt for {key}.")
                    for event in events:
                        if event["messages"][:2] != [
                            {"role": "system", "content": expected_prompt("candidate", question)},
                            {"role": "user", "content": observed["prompt"]},
                        ]:
                            raise ValueError("Delivered prompt differs from the registered inputs.")
                    first = attempts.read_attempt(events[0], observed["evidence"])
                    final = attempts.read_http(response, doc, doc_id)
                    if args.replay and (
                        len(events) != 1
                        or events[0]["content"] != old["raw"]
                        or (first["answer"], first["state"]) != (final["answer"], final["state"])
                    ):
                        raise ValueError(
                            "The archived raw and endpoint projection did not replay unchanged."
                        )
                    if (
                        args.replay
                        and "http" in old
                        and final
                        != attempts.read_http(httpx.Response(200, json=old["http"]), doc, doc_id)
                    ):
                        raise ValueError(
                            "The replayed endpoint answer or citations differ from the archive."
                        )
                    row = {
                        "id": qid,
                        "arm": "candidate",
                        "group": args.group,
                        "answerable": case["answerable"],
                        "http": response.status_code,
                        "evidence_sha256": observed["identity"],
                        "attempts": len(events),
                        "first": public_stage(case, first),
                        "final": public_stage(case, final),
                        "estimated_cny": "0"
                        if args.replay
                        else str(sum(Decimal(e["estimated_cny"]) for e in events)),
                    }
                    write_entry(derived, row)
            verified = reconcile_history(directory, cases, freeze)
            if not args.replay and Decimal(verified["estimated_cny"]) != meter.confirmed_cny:
                raise ValueError("Persistent billing and the live meter differ.")
            save(directory / "execution.json", verified)
    except BaseException as exc:
        save(
            directory / "stopped.json",
            dict(
                exception=type(exc).__name__,
                calls=meter.calls,
                confirmed=len(meter.receipts),
                outstanding=str(meter.outstanding_cny),
                halted=meter.halted,
            ),
        )
        raise
    finally:
        index.close()
        await engine.dispose()


def reconcile_history(directory, cases, freeze):
    wire, http, ledger, derived = [
        rows(directory / n)
        for n in ("wire-private.jsonl", "http-private.jsonl", "receipts.jsonl", "derived.jsonl")
    ]
    if len(derived) != len(cases) or len(http) != 2 * len(cases) or len(ledger) != 2 * len(wire):
        raise ValueError("Incomplete protected response or receipt stream.")
    seen, paid, tokens, ix = set(), Decimal(0), [0, 0, 0], 0
    for i, (case, row) in enumerate(zip(cases, derived, strict=True)):
        qid = case["id"]
        key = qid + "/candidate"
        start, end = http[2 * i : 2 * i + 2]
        if (row["id"], start["key"], end["key"], start["state"], end["state"], end["http"]) != (
            qid,
            key,
            key,
            "started",
            "returned",
            row["http"],
        ) or row["attempts"] not in (1, 2):
            raise ValueError("Invalid protected request/response pairing.")
        amount = Decimal(0)
        sequence = wire[ix : ix + row["attempts"]]
        if (
            len(sequence) != row["attempts"]
            or len(sequence[0]["messages"]) != 2
            or sha(sequence[0]["messages"][0]["content"].encode()) != freeze["policy_sha256"][qid]
            or attempts.digest_json([end["evidence"], sequence[0]["messages"][1]["content"]])
            != row["evidence_sha256"]
        ):
            raise ValueError("First attempt differs from registered inputs.")
        if len(sequence) == 2:
            retry = sequence[1]["messages"]
            if (
                len(retry) != 4
                or retry[:2] != sequence[0]["messages"]
                or retry[2] != {"role": "assistant", "content": sequence[0]["content"]}
                or retry[3]["role"] != "user"
                or retry[3]["content"]
                not in (citation_qa._CITATION_CORRECTION, citation_qa._PROMPT_CITATION_CORRECTION)
            ):
                raise ValueError("Correction does not contain the actual first model answer.")
        for attempt, e in enumerate(sequence, 1):
            reserve, confirm = ledger[2 * ix : 2 * ix + 2]
            ix += 1
            if (
                any(
                    (r["call"], r["key"], r["attempt"]) != (ix, key, attempt)
                    for r in (reserve, confirm, e)
                )
                or reserve["state"] != "reserved"
                or confirm["state"] != "confirmed"
                or confirm["id"] in seen
                or confirm["id"] != e["provider_response"]["id"]
                or Decimal(reserve["cny"]) != CloudTableMeter.reserve
            ):
                raise ValueError("Unmatched or duplicate protected receipt.")
            seen.add(confirm["id"])
            inp, out, cached, price = receipt_cost(
                e["provider_response"], MODEL, max_input=8192, max_output=256
            )
            if (
                any(
                    (r["input"], r["output"], r["cached"], Decimal(r["estimated_cny"]))
                    != (inp, out, cached, price)
                    for r in (confirm, e)
                )
                or price > CloudTableMeter.reserve
            ):
                raise ValueError("Protected receipt usage or price differs.")
            tokens = [a + b for a, b in zip(tokens, (inp, out, cached), strict=True)]
            amount += price
        if not freeze["offline_replay"] and amount != Decimal(row["estimated_cny"]):
            raise ValueError("Per-question estimate differs from physical receipts.")
        paid += amount
    if ix != len(wire) or ix > freeze["max_wire_calls"] or paid > Decimal(freeze["max_cny"]):
        raise ValueError("Extra attempt or exceeded protected group budget.")
    replay = freeze["offline_replay"]
    return {
        "group": freeze["group"],
        "offline_replay": replay,
        "http_requests": len(cases),
        "recorded_attempts": ix,
        "physical_paid_calls": 0 if replay else ix,
        "internal_corrections": ix - len(cases),
        "estimated_cny": "0" if replay else str(paid),
        "unconfirmed": 0,
        "provider_usage": None
        if replay
        else dict(zip(("input", "output", "cached"), tokens, strict=True)),
        "sha256": {
            n: file_hash(directory / n)
            for n in (
                "wire-private.jsonl",
                "http-private.jsonl",
                "derived.jsonl",
                "receipts.jsonl",
                "freeze.json",
            )
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", choices=("old100", "wai14"), required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--replay",
        action="store_true",
        help="Replay archived visible completions locally without provider calls.",
    )
    mode.add_argument(
        "--dispatch",
        action="store_true",
        help="One paid candidate request per registered question. No resumption.",
    )
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cuda")
    parser.add_argument("--run-dir", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
