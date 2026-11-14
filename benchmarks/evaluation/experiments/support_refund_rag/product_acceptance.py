"""Replay the governed HTTP support workflow with synthetic facts. See product-acceptance.md."""

import argparse
import asyncio
import hashlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import time
from contextlib import ExitStack, closing
from pathlib import Path
from uuid import uuid4

import httpx
from sqlalchemy import select

from apps.agent_run_composition import build_agent_run_service
from apps.api.app import create_app
from packages.agent_runtime.adapters.langgraph import configure_windows_asyncio_policy
from packages.core.database import create_database
from packages.knowledge.composition import production_retrieval_components
from packages.knowledge.models import DocumentChunk
from packages.knowledge.retrieval import SessionScopedKnowledgeRetriever
from packages.model_gateway.adapters.litellm import LiteLLMProviderAdapter
from packages.model_gateway.contracts import ModelResponse, ModelToolCall
from packages.model_gateway.credentials import ProviderCredentialCipher
from packages.model_gateway.gateway import SqlAlchemyModelGateway
from packages.tools.builtins import query_customer
from scripts.aliyun_api_smoke import aliyun_route

from .citation_contract import citation_identity_audit
from .fixture_tools import FixtureCaseScope, FixtureToolRegistry, case_scope, read_order_store
from .materialize import _RecordedQueue
from .product_setup import api, isolated_settings, policy_snapshot, publish, tenant
from .reviewed_dev import reviewed_cases
from .runtime_trial import (
    MODEL,
    ROOT,
    RecordingGateway,
    RecordingPrepared,
    RecordingRetriever,
    coverage,
    read,
    save,
    wire,
)


def tool_data(messages):
    return {m.name: json.loads(m.content) for m in messages if m.role == "tool"}


class ContractGateway:
    """Exercises the product contract using only user text and visible tool responses. No labels."""

    async def generate_resolved(self, context, plan, request):
        prompt = next(m.content for m in reversed(request.messages) if m.role == "user")
        data = tool_data(request.messages)
        order = re.search(r"ORD-S0-\d+", prompt)
        customer = re.search(r"CUST-NEW-[A-Z]+", prompt)
        calls = []
        answer = ""

        def tool(name, **args):
            calls.append(ModelToolCall(name, args, uuid4().hex))

        if not data:
            tool("search_knowledge", query=prompt)
            if order:
                tool("get_order", order_id=order[0])
                tool("get_refund_status", order_id=order[0])
            elif customer:
                tool("query_customer", customer_ref=customer[0], include_open_tickets=True)
        elif customer and "create_ticket" not in data:
            tool(
                "create_ticket",
                customer_ref=customer[0],
                subject=re.search("主题“(.*?)”", prompt)[1],
            )
        else:
            rows = data.get("search_knowledge", {}).get("data", {}).get("results", [])
            if rows:
                # The contract actor emits explicit citations. Cross-references in a
                # quoted policy (e.g. its superseding policy) are not its own citations.
                lines = []
                for row in rows:
                    excerpts = [
                        s
                        for s in re.split(r"(?<=[。！？])|\n", row["snippet"])
                        if s.strip() and "POL-" not in s and "或" in s
                    ]
                    lines.append("[" + row["chunk_id"] + "] " + " ".join(excerpts))
                answer = "已查到以下政策记录。\n" + "\n".join(lines)
            if order:
                refund = data.get("get_refund_status", {}).get("data", {})
                if "refund" in refund and refund["refund"] is None:
                    answer += "\n本次订单查询未返回退款记录，历史申请情况未知。"
            if customer:
                result = data["create_ticket"]
                ref = result.get("data", {}).get("ticket_ref")
                answer += "\n已创建工单 " + ref if ref else "\n本次未创建工单。"
        return ModelResponse(
            answer,
            "deterministic-contract",
            "local-script",
            tuple(calls),
            finish_reason="tool_calls" if calls else "stop",
        )


def admitted(calls, chunks):
    messages = calls[-1]["request"].messages if calls else []
    rows = []
    for msg in messages:
        if msg.role != "tool" or msg.name != "search_knowledge":
            continue
        for row in json.loads(msg.content)["data"]["results"]:
            chunk = chunks[row["chunk_id"]]
            if (
                row["snippet"] not in chunk["text"]
                or row["document_revision_id"] != chunk["revision"]
            ):
                raise ValueError("actual request snippet/revision differs from persisted policy")
            rows.append(
                {
                    **row,
                    "source_id": chunk["source_id"],
                    "start_in_chunk": chunk["text"].index(row["snippet"]),
                }
            )
    return rows


async def exercise(operator, owner, scope, agent, case, customer, calls, traces, chunks, record):
    key = case["case_id"]
    thread = await api(operator, "POST", agent + "/threads", {"kind": "support", "title": key})
    turns = scope + "/threads/" + thread["id"] + "/turns"
    record["thread_id"] = thread["id"]
    submitted = await api(
        operator, "POST", turns, {"input_text": case["input"], "client_token": uuid4().hex}
    )
    record["run_id"] = submitted["run_id"]
    run_path = scope + "/agent-runs/" + submitted["run_id"]
    approvals = await api(owner, "GET", run_path + "/approvals")
    ticket_path = scope + "/support/customers/" + customer["id"] + "/tickets"
    before = await api(operator, "GET", ticket_path)
    record.update(approvals_before=approvals, tickets_before=before)
    if "decision" in case and len(approvals) == 1:
        approval = approvals[0]
        args = approval["canonical_arguments"]
        if (
            approval["tool_identity"] != "create_ticket"
            or args.get("customer_ref") != customer["customer_ref"]
            or args.get("subject") != case["subject"]
            or approval["decision_status"] != "PENDING"
            or approval["execution_status"] != "NOT_STARTED"
            or before["total"] != 0
        ):
            raise ValueError("unexpected pending write/target")
        record["decision"] = await api(
            owner, "POST", scope + "/approvals/" + approval["id"] + "/" + case["decision"]
        )
    record["approvals_after"] = await api(owner, "GET", run_path + "/approvals")
    run = await api(operator, "GET", run_path)
    (turn,) = await api(operator, "GET", turns)
    record.update(
        run=run,
        turn=turn,
        steps=await api(operator, "GET", run_path + "/steps"),
        tickets_after=await api(operator, "GET", ticket_path),
        calls=wire(calls),
        retrieval=traces,
    )
    rows = admitted(calls, chunks)
    text = turn["final_output"] or ""
    audit = citation_identity_audit(text, rows)
    tool_results = {
        t["tool_identity"]
        for s in record["steps"]
        for t in s["safe_metadata"].get("tool_results", [])
        if t.get("status") == "SUCCESS"
    }
    expected_tools = (
        {"search_knowledge", "query_customer"}
        if "decision" in case
        else {"search_knowledge", "get_order", "get_refund_status"}
    )
    # Pure missing-fact replies may have no policy claim/citation. Semantic review
    # must verify the actual business lookup and the absence of positive claims.
    missing_refusal = (
        case.get("answerability") == "insufficient_evidence" and audit["status"] == "MISSING"
    )
    checks = {
        "run_succeeded": run["status"] == "SUCCEEDED",
        "persisted_reply": bool(text) and text == run["final_output"],
        "linked_turn": turn["agent_run_id"] == record["run_id"],
        "visible_policy_or_missing_fact_review": bool(rows) or missing_refusal,
        "citation_locatable_or_missing_fact_review": audit["status"] == "VALID" or missing_refusal,
        "required_tools": expected_tools <= tool_results,
    }
    if "decision" in case:
        approval = record.get("decision", {}).get("approval", {})
        after = record["tickets_after"]
        receipts = turn["ticket_requests"]
        approved = case["decision"] == "approve"
        checks["receipt"] = len(receipts) == 1 and receipts[0]["approval_id"] == approval.get("id")
        checks["approval"] = approval.get("decision_status") == (
            "APPROVED" if approved else "DENIED"
        )
        checks["execution"] = approval.get("execution_status") == (
            "SUCCEEDED" if approved else "NOT_STARTED"
        )
        checks["before_writes"] = before["total"] == 0
        checks["ticket_readback"] = after["total"] == int(approved)
        if approved and after["total"] == 1:
            ticket = await api(
                operator, "GET", scope + "/support/tickets/" + after["items"][0]["id"]
            )
            record["ticket"] = ticket
            checks["reply_receipt"] = (
                checks["receipt"]
                and ticket["id"] == receipts[0]["ticket_id"]
                and ticket["ticket_ref"] in text
                and ticket["subject"] == case["subject"]
                and ticket["customer_id"] == customer["id"]
            )
        elif not approved:
            checks["reply_receipt"] = (
                checks["receipt"] and not receipts[0].get("ticket_id") and "未创建" in text
            )
    else:
        checks["no_write"] = (
            not approvals
            and not turn["ticket_requests"]
            and before["total"] == record["tickets_after"]["total"] == 0
        )
    for i, query in enumerate(traces):
        candidates = query["result"]["trace"].get("selection")
        if candidates and "evidence_groups" in case:
            candidate_rows = [
                dict(
                    source_id=chunks[r["chunk_id"]]["source_id"],
                    snippet=chunks[r["chunk_id"]]["text"],
                )
                for r in candidates["results"]
            ]
            query["candidate_group_complete"] = coverage(case, candidate_rows)
        query["search_number"] = i + 1
    record.update(
        contract_checks=checks,
        contract_pass=all(checks.values()),
        citations=audit,
        uncited_missing_fact_review=missing_refusal,
        visible=rows,
        coverage=coverage(case, rows)
        if "evidence_groups" in case
        else any(r["source_id"] == case["source_id"] for r in rows),
    )


def open_paid_schedule(path):
    """One ledger exposure across all 50 DEV and two new cases, including early stops."""
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM authorization").fetchone():
            raise ValueError("missing study authorization")
        if db.execute(
            "SELECT 1 FROM wires WHERE stage IN ('refund_dev','refund_new') "
            "OR status != 'returned' LIMIT 1"
        ).fetchone():
            raise ValueError("prior refund dispatch or unresolved physical request")
        db.execute("CREATE TABLE IF NOT EXISTS study_opening(name TEXT PRIMARY KEY)")
        db.execute("INSERT INTO study_opening VALUES ('refund_dev_and_new')")


def require_settled_run(record):
    """A returned model request and a failed business case are independent outcomes."""
    wires = record["physical_attempts"]
    if any(w["status"] != "returned" or w["input"] is None or w["output"] is None for w in wires):
        raise ValueError("unsettled physical request; stop the study")
    status = record.get("run", {}).get("status")
    if status == "SUCCEEDED" or (status == "FAILED" and wires):
        return
    raise ValueError("incomplete run or missing dispatch evidence; stop the study")


async def refresh_roles(owner, operator, users):
    for client, expected in zip((owner, operator), users, strict=True):
        result = await api(client, "POST", "auth/refresh")
        if result["user_id"] != str(expected):
            raise ValueError("Authentication refresh changed principal")
        client.headers["Authorization"] = "Bearer " + result["access_token"]


async def assemble(settings):
    engine, factory = create_database(settings.database_url)
    app = create_app(settings)
    app.state.db_session_factory = factory
    queue = app.state.ingestion_queue = _RecordedQueue()
    components = production_retrieval_components(settings)
    components.index.ensure_collection()
    for component in (components.dense, components.sparse, components.reranker):
        await asyncio.to_thread(component.warm)
    runtime = build_agent_run_service(settings, factory, memory_writer=None)
    app.state.agent_run_service = runtime
    store = read_order_store(ROOT / "data/orders.json")
    retriever = RecordingRetriever(
        SessionScopedKnowledgeRetriever(
            session_factory=factory,
            components=components,
            rrf_k=settings.knowledge_rrf_k,
            min_rerank_score=None,
            superseded_rank_penalty=0,
        )
    )
    retriever.components = components
    registry = FixtureToolRegistry(store=store, retriever=retriever)
    registry._handlers["query_customer"] = query_customer
    runtime.tool_runtime.registry = registry
    return engine, factory, app, queue, components, runtime, store, retriever


async def group_chunks(factory, kb, uploaded):
    revisions = {u["revision"]["id"]: sid for sid, u in uploaded.items()}
    async with factory() as db:
        chunks = {
            c.chunk_id: {
                "text": c.text,
                "revision": str(c.document_revision_id),
                "source_id": revisions[str(c.document_revision_id)],
            }
            for c in await db.scalars(
                select(DocumentChunk).where(DocumentChunk.knowledge_base_id == kb)
            )
        }
    return chunks


async def exercise_cases(
    cases,
    stage,
    meter,
    args,
    owner,
    operator,
    users,
    scope,
    agent,
    store,
    runtime,
    retriever,
    settings,
    chunks,
    out,
    report,
):
    paid = meter is not None
    for case in cases:
        await refresh_roles(owner, operator, users)
        key = case["case_id"]
        record = {
            "case_id": key,
            "stage": stage,
            "expected": case,
            "answerability": case.get("answerability", "answerable"),
            "semantic_verdict": "PENDING_REVIEW" if paid else "NOT_MEASURED",
        }
        calls = []
        retriever.records = []
        with ExitStack() as stack:
            if stage == "refund_dev":
                order = store.get(case["order_fixture"]["order_id"])
                ref = order["customer_id"]
                stack.enter_context(case_scope(FixtureCaseScope(key, order["order_id"], ref)))
            else:
                ref = case["customer_ref"]
            customer = await api(
                operator,
                "POST",
                scope + "/support/customers",
                {"customer_ref": ref, "name": "合成客户"},
            )
            if paid:
                meter.task_id = key
                runtime.model_gateway_factory = lambda db, meter=meter, calls=calls: (
                    RecordingGateway(
                        SqlAlchemyModelGateway(
                            db,
                            adapter=LiteLLMProviderAdapter(client=meter),
                            credential_cipher=ProviderCredentialCipher.from_settings(settings),
                        ),
                        calls,
                    )
                )
            else:
                runtime.model_gateway_factory = lambda _, calls=calls: RecordingPrepared(
                    ContractGateway(), calls
                )
            started = time.perf_counter()
            try:
                await exercise(
                    operator,
                    owner,
                    scope,
                    agent,
                    case,
                    customer,
                    calls,
                    retriever.records,
                    chunks,
                    record,
                )
            finally:
                if paid and record.get("run", {}).get("status") != "SUCCEEDED":
                    record["semantic_verdict"] = "RUN_ERROR"
                record["end_to_end_seconds"] = round(time.perf_counter() - started, 3)
                if paid:
                    with closing(sqlite3.connect(args.ledger)) as db:
                        db.row_factory = sqlite3.Row
                        record["physical_attempts"] = [
                            dict(row)
                            for row in db.execute(
                                "SELECT * FROM wires WHERE stage=? AND task=? ORDER BY id",
                                (stage, key),
                            )
                        ]
                else:
                    record["physical_attempts"] = []
                record["calls"] = wire(calls)
                record["retrieval"] = retriever.records
                save(out / (key + ".json"), record)
                report["rows"].append(
                    {
                        k: record.get(k)
                        for k in (
                            "case_id",
                            "stage",
                            "answerability",
                            "uncited_missing_fact_review",
                            "run_id",
                            "thread_id",
                            "contract_pass",
                            "contract_checks",
                            "coverage",
                            "semantic_verdict",
                        )
                    }
                )
                save(out / "report.json", report)
            if paid:
                require_settled_run(record)


async def run(args):
    roots = [
        "apps",
        "packages",
        "migrations",
        "scripts",
        "benchmarks/evaluation/experiments/support_refund_rag",
        "benchmarks/public_rag/confirmation_meter.py",
    ]
    files = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", *roots], text=True
    ).splitlines()
    # Include edited/untracked Python used by a local contract run. A paid exposure
    # requires a reviewable immutable implementation before loading any credential.
    digest = hashlib.sha256()
    for name in sorted(path for path in files if path.endswith(".py")):
        digest.update(name.encode() + b"\0" + hashlib.sha256(Path(name).read_bytes()).digest())
    if (
        args.mode == "paid"
        and subprocess.check_output(
            ["git", "status", "--porcelain", "--", *roots], text=True
        ).strip()
    ):
        raise ValueError(
            "commit the study implementation and frozen fixtures before paid execution"
        )
    out = args.output
    out.mkdir(parents=True, exist_ok=False)
    settings = await isolated_settings(out)
    engine, factory, app, queue, components, runtime, store, retriever = await assemble(settings)
    dev, new = reviewed_cases(), read(ROOT / "new_ticket_cases.json")["cases"]
    docs = read(ROOT / "data/manifest.json")["documents"]
    transport = httpx.ASGITransport(app=app)
    options = {"transport": transport, "base_url": "https://product.local/api/v1/"}
    scheduled = {
        q["case_id"]: stage
        for stage, questions in [("refund_dev", dev), ("refund_new", new)]
        for q in questions
        if args.stage in {"all", stage}
    }
    report = {
        "schedule": scheduled,
        "mode": args.mode,
        "python_source_sha256": digest.hexdigest(),
        "code_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "db_name": settings.knowledge_qdrant_collection,
        "rows": [],
        "groups": {},
    }
    async with httpx.AsyncClient(**options) as owner, httpx.AsyncClient(**options) as operator:
        scope, users = await tenant(owner, operator)
        report["workspace_id"] = scope.split("/")[-1]
        paid = args.mode == "paid"
        route = aliyun_route() if paid else None
        credential = await api(
            owner,
            "POST",
            scope + "/provider-credentials",
            {
                "name": "isolated-refund",
                "provider": "openai-compatible",
                "secret": route.api_key if paid else "synthetic-offline-only",
                "base_url": route.base_url if paid else "https://example.test/v1",
            },
        )
        credpath = scope + "/provider-credentials/" + credential["id"]
        try:
            profile = await api(
                owner,
                "POST",
                scope + "/model-profiles",
                {
                    "provider_credential_id": credential["id"],
                    "model": MODEL,
                    "temperature": "0",
                    "max_tokens": 2048,
                    "timeout_seconds": "60",
                    "capabilities": read(ROOT / "model-route-frozen.json")["primary"][
                        "capabilities"
                    ],
                },
            )
            for stage, cases, policies in [
                ("refund_dev", dev, docs),
                (
                    "refund_new",
                    new,
                    [
                        dict(c, effective_from=d)
                        for c, d in zip(new, ["2026-10-01", "2026-09-15"], strict=True)
                    ],
                ),
            ]:
                if args.stage != "all" and args.stage != stage:
                    continue
                kb, snap, uploaded = await policy_snapshot(
                    owner, scope, policies, components, settings, queue
                )
                agent, version = await publish(owner, scope, users[0], factory, snap, profile["id"])
                report["groups"][stage] = {
                    "snapshot": snap,
                    "version": version,
                    "documents": uploaded,
                }
                chunks = await group_chunks(factory, kb, uploaded)
                meter = None
                if paid:
                    from benchmarks.public_rag.confirmation_meter import AliyunMeter

                    meter = AliyunMeter(args.ledger, stage)
                    if stage == "refund_dev":
                        save(out / "report.json", report)
                        open_paid_schedule(args.ledger)
                await exercise_cases(
                    cases,
                    stage,
                    meter,
                    args,
                    owner,
                    operator,
                    users,
                    scope,
                    agent,
                    store,
                    runtime,
                    retriever,
                    settings,
                    chunks,
                    out,
                    report,
                )
        finally:
            await api(owner, "PATCH", credpath, {"enabled": False})
            await api(
                owner, "POST", credpath + "/rotate-secret", {"secret": secrets.token_urlsafe(32)}
            )
            await engine.dispose()
    return all(r["contract_pass"] for r in report["rows"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("contract", "paid"), default="contract")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--ledger", type=Path)
    parser.add_argument("--stage", choices=("all", "refund_dev", "refund_new"), default="all")
    args = parser.parse_args()
    if args.mode == "paid" and args.stage != "all":
        parser.error("paid execution must follow the complete frozen schedule")
    if args.mode == "paid" and not (args.ledger and args.ledger.is_file()):
        parser.error("paid execution requires an initialized approved physical dispatch ledger")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    configure_windows_asyncio_policy()
    return 0 if asyncio.run(run(args)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
