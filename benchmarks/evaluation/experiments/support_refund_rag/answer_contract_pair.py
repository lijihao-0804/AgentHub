"""One frozen support task on its own real product database, index, thread and tool state."""

import hashlib
import json
import secrets
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from sqlalchemy import event

from benchmarks.public_rag.confirmation_meter import text_counter
from packages.agent_runtime.models import Tool
from packages.knowledge.adapters.qdrant import QdrantVectorIndex
from packages.knowledge.composition import production_retrieval_components
from packages.model_gateway.adapters.litellm import LiteLLMProviderAdapter
from packages.model_gateway.credentials import ProviderCredentialCipher
from packages.model_gateway.gateway import SqlAlchemyModelGateway
from packages.tools.builtins import query_customer
from packages.tools.models import Ticket
from scripts.aliyun_api_smoke import aliyun_route

from .fixture_tools import (
    FIXED_BUSINESS_TIME,
    FixtureCaseScope,
    FixtureToolRegistry,
    case_scope,
    read_order_store,
)
from .product_acceptance import assemble, group_chunks
from .product_setup import api, isolated_settings, policy_snapshot, publish, tenant
from .runtime_trial import MODEL, ROOT, RecordingGateway, RecordingPrepared, read, save, wire


@contextmanager
def fixed_catalog_order(scope):
    """Publication orders by tool UUID. Seed catalog keys in the PR19 NEW-PICKUP wire order."""
    names = (
        "search_knowledge",
        "create_ticket",
        "get_order",
        "query_customer",
        "get_refund_status",
    )
    workspace = UUID(scope.split("/")[-1])

    def assign_id(mapper, connection, tool):
        if tool.workspace_id == workspace:
            identity = tool.name.split("-", 1)[0]
            tool.id = UUID(int=names.index(identity) + 1)

    event.listen(Tool, "before_insert", assign_id)
    try:
        yield
    finally:
        event.remove(Tool, "before_insert", assign_id)


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def validate(case):
    if not all(case.get(k) for k in ("case_id", "input", "customer_ref", "documents")):
        raise ValueError("incomplete frozen scenario")
    docs = case["documents"]
    ids = {d["source_id"] for d in docs}
    if len(ids) != len(docs) or any(not d["source"] or not d["effective_from"] for d in docs):
        raise ValueError("incomplete or repeated frozen policy")
    order = case["order"]
    if order is not None and (
        not order["order_id"] or order["customer_id"] != case["customer_ref"]
    ):
        raise ValueError("order does not belong to scoped customer")
    if case.get("decision") not in (None, "approve", "deny"):
        raise ValueError("unsupported frozen approval decision")
    if case.get("decision") and not case.get("subject"):
        raise ValueError("approved or denied write requires a frozen subject")
    if len(case.get("tickets", [])) >= 50:
        raise ValueError("seeded tickets exceed single audited readback page")
    allowed = {"search_knowledge", "query_customer", "get_order", "get_refund_status"}
    if not set(case["required_reads"]) <= allowed:
        raise ValueError("unknown required read")


def verify_version(version, snapshot, arm):
    spec = version["resolved_spec"]
    expected = {
        "model": {
            "model": MODEL,
            "temperature": 0,
            "max_tokens": 2048,
            "retry_policy": {"max_attempts": 1},
            "fallback_profiles": [],
        },
        "runtime": {
            "max_steps": 4,
            "max_tool_calls": 12,
            "max_parallel_reads": 3,
            "context_budget": read(ROOT / "protocol.json")["runtime_target"]["context_budget"],
        },
        "retrieval": {
            "dense_top_k": 30,
            "sparse_top_k": 30,
            "candidate_top_k": 20,
            "final_top_k": 6,
            "embedding_model": "BAAI/bge-m3",
            "reranker_model": "BAAI/bge-reranker-v2-m3",
            "retrieval_strategy": "HYBRID_RERANK",
            "knowledge_snapshots": [
                {"snapshot_id": snapshot["id"], "snapshot_hash": snapshot["content_hash"]}
            ],
        },
    }
    if (
        spec["runtime"].get("support_answer_policy")
        != ("support_facts_v1" if arm == "candidate" else None)
        or any(
            spec[section].get(k) != value
            for section, fields in expected.items()
            for k, value in fields.items()
        )
        or len(spec["tools"]) != 5
    ):
        raise ValueError("published support version differs from frozen settings")


class CheckpointGateway(RecordingGateway):
    def __init__(self, inner, calls, checkpoint):
        super().__init__(inner, calls)
        self.checkpoint = checkpoint

    async def prepare_resolved(self, *args, **kwargs):
        prepared = await super().prepare_resolved(*args, **kwargs)
        return CheckpointPrepared(prepared, self.checkpoint)


class CheckpointPrepared:
    def __init__(self, prepared, checkpoint):
        self.prepared = prepared
        self.checkpoint = checkpoint

    async def generate_resolved(self, *args, **kwargs):
        try:
            return await self.prepared.generate_resolved(*args, **kwargs)
        finally:
            self.checkpoint()


class LimitedMeter:
    def __init__(self, inner):
        self.inner = inner
        self.receipts = []
        self.count = 0

    async def acompletion(self, **kwargs):
        if self.count >= 4:
            raise RuntimeError("frozen four-request ceiling reached")
        self.count += 1
        response = await self.inner.acompletion(**kwargs)
        usage = response.usage
        self.receipts.append(
            dict(
                provider_id=response.id,
                model=response.model,
                input=usage.prompt_tokens,
                output=usage.completion_tokens,
            )
        )
        return response


def request_audit(calls, chunks):
    count = text_counter().count_serialized_text
    records = []
    for call in calls:
        evidence = []
        for message in call["request"].messages:
            if message.role != "tool" or message.name != "search_knowledge":
                continue
            payload = json.loads(message.content)
            for row in (payload.get("data") or {}).get("results", []):
                chunk = chunks[row["chunk_id"]]
                if (
                    row["document_revision_id"] != chunk["revision"]
                    or row["snippet"] not in chunk["text"]
                ):
                    raise ValueError("delivered snippet differs from stored document revision")
                evidence.append(
                    {
                        **row,
                        "source_id": chunk["source_id"],
                        "start_in_chunk": chunk["text"].index(row["snippet"]),
                    }
                )
        messages = call["request"].messages
        records.append(
            {
                "support_system_extension_tokens": sum(
                    count(msg.content) - count(msg.content.split("\n\nSupport facts v1.", 1)[0])
                    for msg in messages
                    if msg.role == "system" and "\n\nSupport facts v1." in msg.content
                ),
                "observation_field_tokens": sum(
                    count(
                        json.dumps(
                            json.loads(msg.content)["observation"],
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    )
                    for msg in messages
                    if msg.role == "tool" and "observation" in json.loads(msg.content)
                ),
                "evidence": [
                    {**row, "snippet_sha256": hashlib.sha256(row["snippet"].encode()).hexdigest()}
                    for row in evidence
                ],
                "message_content_tokens": [
                    {"role": msg.role, "name": msg.name, "tokens": count(msg.content or "")}
                    for msg in messages
                ],
                "tool_schema_serialized_tokens": count(
                    json.dumps(wire(call["request"].tools), ensure_ascii=False, sort_keys=True)
                ),
            }
        )
    return records


async def run_case(case, arm, *, meter, output, scripted_gateway=None):
    """The caller claims the task durably before entry. Each task also claims its artifact once."""
    validate(case)
    if arm not in {"baseline", "candidate"} or (meter is None) == (scripted_gateway is None):
        raise ValueError("select a frozen arm and exactly one metered or scripted gateway")
    out = Path(output)
    out.mkdir(parents=True, exist_ok=True)
    (out / "first-task.claim").touch(exist_ok=False)
    calls = []
    record = {
        "case_id": case["case_id"],
        "arm": arm,
        "terminal_known": False,
        "case_sha256": digest(case),
        "policy_content_sha256": digest(case["documents"]),
        "business_time": FIXED_BUSINESS_TIME,
    }
    save(out / "run.json", record)
    settings = await isolated_settings(out)
    # Share immutable encoder weights, while every task owns its index collection.
    encoders = production_retrieval_components(
        settings.model_copy(update={"knowledge_qdrant_collection": "support_pair_encoder_cache"})
    )
    index = QdrantVectorIndex(
        url=settings.qdrant_url,
        collection_name=settings.knowledge_qdrant_collection,
        dense_vector_size=settings.knowledge_dense_vector_size,
        timeout_seconds=settings.knowledge_qdrant_timeout_seconds,
    )
    engine, factory, app, queue, components, runtime, _, retriever = await assemble(
        settings, components=replace(encoders, index=index)
    )
    transport = httpx.ASGITransport(app=app)
    options = {"transport": transport, "base_url": "https://product.local/api/v1/"}
    counter = LimitedMeter(meter) if meter is not None else None
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(**options) as owner, httpx.AsyncClient(**options) as operator:
            scope, users = await tenant(owner, operator)
            record.update(database=settings.knowledge_qdrant_collection, workspace=scope)
            route = aliyun_route() if counter else None
            credential = await api(
                owner,
                "POST",
                scope + "/provider-credentials",
                {
                    "name": "support-pair",
                    "provider": "openai-compatible",
                    "secret": route.api_key if route else "offline-only",
                    "base_url": route.base_url if route else "https://example.test/v1",
                },
            )
            path = scope + "/provider-credentials/" + credential["id"]
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
                kb, snap, uploaded = await policy_snapshot(
                    owner, scope, case["documents"], components, settings, queue
                )
                with fixed_catalog_order(scope):
                    agent, version = await publish(
                        owner,
                        scope,
                        users[0],
                        factory,
                        snap,
                        profile["id"],
                        **(
                            {"support_answer_policy": "support_facts_v1"}
                            if arm == "candidate"
                            else {}
                        ),
                    )
                verify_version(version, snap, arm)
                record.update(snapshot=snap, version=version, documents=uploaded)
                chunks = await group_chunks(factory, kb, uploaded)
                order = case["order"]
                # Never expose the historical order store to a new case.
                store = {}
                if order is not None:
                    fixture = out / "order.json"
                    save(
                        fixture,
                        {
                            "fictional": True,
                            "orders": [order],
                            "business_clock": {"business_time_anchor": FIXED_BUSINESS_TIME},
                        },
                    )
                    store = read_order_store(fixture)
                registry = FixtureToolRegistry(store=store, retriever=retriever)
                registry._handlers["query_customer"] = query_customer
                runtime.tool_runtime.registry = registry

                def checkpoint():
                    record.update(
                        calls=wire(calls),
                        request_audit=request_audit(calls, chunks),
                        retrieval=retriever.records,
                        physical_receipts=counter.receipts if counter else [],
                    )
                    save(out / "run.json", record)

                if counter:
                    runtime.model_gateway_factory = lambda db: CheckpointGateway(
                        SqlAlchemyModelGateway(
                            db,
                            adapter=LiteLLMProviderAdapter(client=counter),
                            credential_cipher=ProviderCredentialCipher.from_settings(settings),
                        ),
                        calls,
                        checkpoint,
                    )
                else:
                    runtime.model_gateway_factory = lambda _: RecordingPrepared(
                        scripted_gateway, calls
                    )
                ref = case["customer_ref"]
                customer = await api(
                    operator,
                    "POST",
                    scope + "/support/customers",
                    {"name": "合成客户", **case.get("customer", {}), "customer_ref": ref},
                )
                record["customer_id"] = customer["id"]
                async with factory() as session:
                    for ticket in case.get("tickets", []):
                        session.add(
                            Ticket(
                                workspace_id=UUID(scope.split("/")[-1]),
                                customer_id=UUID(customer["id"]),
                                **ticket,
                            )
                        )
                    await session.commit()
                with case_scope(
                    FixtureCaseScope(
                        case["case_id"],
                        order["order_id"] if order else "absent-order",
                        ref,
                        support_answer_policy="support_facts_v1" if arm == "candidate" else None,
                    )
                ):
                    await execute_turn(case, owner, operator, scope, customer, agent, record, out)
                record["request_audit"] = request_audit(calls, chunks)
                if counter:
                    totals = tuple(sum(r[k] for r in counter.receipts) for k in ("input", "output"))
                    if (
                        not counter.receipts
                        or counter.count != len(counter.receipts)
                        or totals
                        != (
                            record["run"]["total_input_tokens"],
                            record["run"]["total_output_tokens"],
                        )
                    ):
                        record["terminal_known"] = False
            finally:
                await api(owner, "PATCH", path, {"enabled": False})
                await api(
                    owner, "POST", path + "/rotate-secret", {"secret": secrets.token_urlsafe(32)}
                )
    except BaseException:
        record["terminal_known"] = False
        raise
    finally:
        record.update(
            calls=wire(calls),
            retrieval=retriever.records,
            physical_receipts=counter.receipts if counter else [],
            elapsed_seconds=time.perf_counter() - started,
        )
        save(out / "run.json", record)
        await engine.dispose()
        index.close()
    return record


async def execute_turn(case, owner, operator, scope, customer, agent, record, out):
    tickets = scope + "/support/customers/" + customer["id"] + "/tickets"
    before = record["tickets_before"] = await api(operator, "GET", tickets)
    fields = ("ticket_ref", "subject", "status")

    def project(rows):
        return sorted(tuple(row[k] for k in fields) for row in rows)

    if project(before["items"]) != project(case.get("tickets", [])):
        raise ValueError("initial product ticket state differs from frozen fixture")
    thread = await api(
        operator, "POST", agent + "/threads", {"kind": "support", "title": case["case_id"]}
    )
    turns = scope + "/threads/" + thread["id"] + "/turns"
    record.update(thread_id=thread["id"], client_token=uuid4().hex)
    save(out / "run.json", record)
    submitted = await api(
        operator,
        "POST",
        turns,
        {"input_text": case["input"], "client_token": record["client_token"]},
    )
    record["run_id"] = submitted["run_id"]
    save(out / "run.json", record)
    path = scope + "/agent-runs/" + submitted["run_id"]
    approvals = record["approvals_before"] = await api(owner, "GET", path + "/approvals")
    save(out / "run.json", record)
    if len(approvals) == 1 and case.get("decision"):
        approval = approvals[0]
        args = approval["canonical_arguments"]
        if (
            approval["tool_identity"] == "create_ticket"
            and args.get("customer_ref") == case["customer_ref"]
            and args.get("subject") == case["subject"]
            and approval["decision_status"] == "PENDING"
            and approval["execution_status"] == "NOT_STARTED"
        ):
            record["decision"] = await api(
                owner, "POST", scope + "/approvals/" + approval["id"] + "/" + case["decision"]
            )
    record["approvals_after"] = await api(owner, "GET", path + "/approvals")
    run = record["run"] = await api(operator, "GET", path)
    (turn,) = await api(operator, "GET", turns)
    record["turn"] = turn
    record["steps"] = await api(operator, "GET", path + "/steps")
    record["tickets_after"] = await api(operator, "GET", tickets)
    record["ticket_details"] = [
        await api(operator, "GET", scope + "/support/tickets/" + t["id"])
        for t in record["tickets_after"]["items"]
        if t["id"] not in {s["id"] for s in before["items"]}
    ]
    record["terminal_known"] = (
        run["status"] in {"SUCCEEDED", "FAILED"}
        and turn["agent_run_id"] == run["id"]
        and turn["final_output"] == run["final_output"]
        and all(
            a["decision_status"] != "PENDING"
            and a["execution_status"] not in {"CLAIMED", "UNKNOWN_OUTCOME"}
            for a in record["approvals_after"]
        )
    )


async def recover_case(case, arm, *, output, wires):
    """Read-only reconciliation; an incomplete or inconsistent chain keeps the study stopped."""
    from sqlalchemy import select, text
    from sqlalchemy.exc import MultipleResultsFound, NoResultFound

    from apps.api.schemas.agent_runs import AgentRunResponse, RunStepResponse
    from apps.api.schemas.approvals import ApprovalResponse
    from apps.api.schemas.support import TicketRecord
    from packages.agent_runtime.models import AgentRun, RunStep
    from packages.approvals.models import Approval
    from packages.core.database import create_database
    from packages.threads.models import ThreadTurn

    from .product_continuation import retained_settings

    out = Path(output)
    record = dict(read(out / "run.json"), terminal_known=False)
    settings = retained_settings(out, record["database"])
    engine, factory = create_database(settings.database_url)
    try:
        if record["case_sha256"] != digest(case) or record["arm"] != arm:
            raise ValueError("case or arm drift")
        async with factory() as db:
            await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            ws = UUID(record["workspace"].split("/")[-1])
            thread = UUID(record["thread_id"])
            turn = (
                await db.scalars(
                    select(ThreadTurn).where(
                        ThreadTurn.workspace_id == ws,
                        ThreadTurn.thread_id == thread,
                    )
                )
            ).one()
            run = await db.get(AgentRun, turn.agent_run_id)
            if not (
                run
                and run.workspace_id == ws
                and run.thread_id == thread
                and run.thread_turn_id == turn.id
                and turn.client_token == record["client_token"]
                and turn.user_input == case["input"]
                and run.status in {"SUCCEEDED", "FAILED"}
                and str(run.id) == record.get("run_id", str(run.id))
                and not run.resume_pending
                and not run.waiting_approval_id
                and not run.execution_owner
                and run.resolved_spec_hash == record["version"]["resolved_spec_hash"]
            ):
                raise ValueError("unsettled persisted turn, run, lease or version")
            attempts = record["physical_receipts"]
            if not (
                0 < len(wires) <= 4
                and len(wires)
                == len(attempts)
                == len(record["calls"])
                == len(record["request_audit"])
                and all(
                    w["status"] == "returned"
                    and all(w[k] == a[k] for k in ("provider_id", "input", "output"))
                    for w, a in zip(wires, attempts, strict=True)
                )
                and (run.total_input_tokens, run.total_output_tokens)
                == tuple(sum(w[k] for w in wires) for k in ("input", "output"))
            ):
                raise ValueError("wire, request audit or persisted usage mismatch")
            approvals = list(await db.scalars(select(Approval).where(Approval.run_id == run.id)))
            if any(
                a.decision_status == "PENDING"
                or a.execution_status in {"CLAIMED", "UNKNOWN_OUTCOME"}
                for a in approvals
            ):
                raise ValueError("approval or external execution remains unresolved")
            rows = list(
                await db.scalars(
                    select(Ticket).where(
                        Ticket.workspace_id == ws, Ticket.customer_id == UUID(record["customer_id"])
                    )
                )
            )
            steps = list(
                await db.scalars(
                    select(RunStep)
                    .where(RunStep.agent_run_id == run.id)
                    .order_by(RunStep.sequence_number)
                )
            )
            if [s.status for s in steps if s.kind == "FINISH"] != [run.status]:
                raise ValueError("terminal step mismatch")
            if approvals and "approvals_before" not in record:
                raise ValueError("missing original predecision observation")
            receipts = []
            for a in approvals:
                if a.tool_identity != "create_ticket":
                    continue
                receipt = dict(
                    approval_id=str(a.id),
                    decision_status=a.decision_status,
                    execution_status=a.execution_status,
                )
                if a.execution_status == "SUCCEEDED":
                    (ticket,) = [
                        t
                        for t in rows
                        if str(t.id) == (a.safe_result or {}).get("ticket_id")
                        and t.idempotency_key == a.idempotency_key
                    ]
                    receipt.update(ticket_id=str(ticket.id), ticket_ref=ticket.ticket_ref)
                receipts.append(receipt)
            tickets = [TicketRecord.model_validate(t).model_dump(mode="json") for t in rows]
            before = {t["id"] for t in record["tickets_before"]["items"]}
            record.update(
                run_id=str(run.id),
                run=AgentRunResponse.model_validate(run).model_dump(mode="json"),
                steps=[RunStepResponse.model_validate(s).model_dump(mode="json") for s in steps],
                approvals_after=[
                    ApprovalResponse.model_validate(a).model_dump(mode="json") for a in approvals
                ],
                turn=dict(
                    id=str(turn.id),
                    thread_id=str(thread),
                    sequence=turn.sequence,
                    user_input=turn.user_input,
                    created_at=turn.created_at.isoformat(),
                    agent_run_id=str(run.id),
                    status=run.status,
                    final_output=run.final_output,
                    failure_code=run.failure_code,
                    ticket_requests=receipts,
                ),
                tickets_after=dict(items=tickets, total=len(tickets)),
                ticket_details=[t for t in tickets if t["id"] not in before],
                terminal_known=True,
            )
    except (LookupError, ValueError, MultipleResultsFound, NoResultFound) as exc:
        record["reconciliation_failure"] = str(exc)
    finally:
        await engine.dispose()
    return record
