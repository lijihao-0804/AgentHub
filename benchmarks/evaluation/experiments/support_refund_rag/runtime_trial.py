"""Published, budgeted support DEV runs and request-preserving B diagnostics.

prepare and admission never initialize a generation client. execute is explicitly
opt-in and uses the production ExperimentRunner, Thread and AgentRun services.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from benchmarks.evaluation.experiments.support_refund_rag.fixture_tools import (
    FixtureCaseScope,
    FixtureToolRegistry,
    case_scope,
    fixture_tool_specs,
    read_order_store,
)
from benchmarks.evaluation.experiments.support_refund_rag.materialize import _exclusive_lock
from packages.agent_runtime.context_budget import (
    ApproxTokenEstimator,
    ContextBudgetConfig,
    ContextBudgetPolicy,
)
from packages.agent_runtime.frozen import parse_frozen_agent_spec
from packages.agent_runtime.models import (
    AgentKnowledgeBinding,
    AgentTool,
    AgentVersion,
    Tool,
    ToolRevision,
)
from packages.agent_runtime.publish import AgentPublishService
from packages.agent_runtime.rag_evidence import project_rag_evidence_payload
from packages.agent_runtime.runtime import AgentRunService
from packages.agent_runtime.runtime_projection import (
    _RUNTIME_POLICY,
    _bounded_tool_result,
    _categorize_messages,
)
from packages.agent_templates.support import SUPPORT_TEMPLATE
from packages.control_plane.services import TenantService
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.config.settings import Settings
from packages.core.database import create_database
from packages.core.execution_context.models import PrincipalContext
from packages.evaluation.experiments import ExperimentService
from packages.evaluation.observations import observation_identity
from packages.evaluation.runner import (
    CaseExecutionObservation,
    ExperimentRunner,
    PreparedCaseExecution,
)
from packages.evaluation.service import EvaluationDatasetService
from packages.knowledge.composition import production_retrieval_components
from packages.knowledge.retrieval import SessionScopedKnowledgeRetriever
from packages.model_gateway.adapters.litellm import LiteLLMProviderAdapter
from packages.model_gateway.configured_route import load_route
from packages.model_gateway.contracts import (
    ModelMessage,
    ModelToolCall,
    ModelToolDefinition,
)
from packages.model_gateway.credentials import (
    ProviderCredentialCipher,
    encrypt_provider_credential_secret,
)
from packages.model_gateway.gateway import SqlAlchemyModelGateway
from packages.model_gateway.models import ModelProfile, ProviderCredential
from packages.threads.context import SqlAlchemyThreadContextProvider
from packages.threads.service import ThreadService
from packages.tools.runtime import PublishedToolResolver, ToolRuntime

ROOT = Path(__file__).resolve().parent
PRIVATE = ROOT / "private"
PREPARED = PRIVATE / "prepared.json"
MODEL = "deepseek-v4.1-flash-1017"


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as out:
        json.dump(value, out, ensure_ascii=False, indent=2, default=str)
        out.write("\n")
        out.flush()
        os.fsync(out.fileno())
    os.replace(temporary, path)


def environment():
    state = read(PRIVATE / "s1/state.json")
    if state.get("phase") != "retrieval_complete":
        raise ValueError("S1_REAL_RETRIEVAL_NOT_COMPLETE")
    settings = Settings().model_copy(update=state["frozen_settings"])
    return state, settings, create_database(settings.database_url)


async def context_for(factory, state):
    ids = state["identities"]
    async with factory() as db:
        access = await TenantService().get_workspace_access(
            db,
            principal=PrincipalContext(
                user_id=ids["user_id"], request_id=uuid4().hex, trace_id=uuid4().hex
            ),
            workspace_id=UUID(ids["workspace_id"]),
        )
        return access.context


def code_commit():
    dirty = subprocess.check_output(
        [
            "git",
            "status",
            "--porcelain",
            "--",
            "packages",
            "apps",
            "benchmarks/evaluation/experiments/support_refund_rag",
        ],
        text=True,
    )
    if dirty.strip():
        raise ValueError("COMMIT_EXPERIMENT_IMPLEMENTATION_BEFORE_FORMAL_PUBLICATION")
    return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()


def source_chunks():
    mapping = read(PRIVATE / "s1/source-map.json")
    state = read(PRIVATE / "s1/state.json")
    result = {}
    for source_id, doc in mapping["documents"].items():
        for chunk in doc["chunks"]:
            result[chunk["chunk_id"]] = {
                **chunk,
                "source_id": source_id,
                "document_id": doc["document_id"],
                "document_revision_id": doc["document_revision_id"],
                "snapshot_id": state["knowledge"]["snapshot_id"],
                "knowledge_base_id": state["knowledge"]["knowledge_base_id"],
                "document_name": doc["filename"],
                "effective_date": doc["stored_lifecycle"]["effective_date"],
                "superseded": bool(doc["stored_lifecycle"]["superseded_by_document_id"]),
            }
    return result


def visible(messages, chunks):
    rows = []
    for message in messages:
        if message.role != "tool" or message.name != "search_knowledge":
            continue
        payload = json.loads(message.content or "{}")
        for record in payload.get("data", {}).get("results", []):
            chunk = chunks.get(record.get("chunk_id"))
            if chunk is None or record.get("snippet", "") not in chunk["text"]:
                raise ValueError("CITATION_MAPPING_BROKEN")
            if record.get("document_revision_id") != chunk["document_revision_id"]:
                raise ValueError("CITATION_REVISION_DRIFT")
            rows.append({**record, "source_id": chunk["source_id"]})
    return rows


def coverage(question, rows):
    return any(
        all(
            any(
                row["source_id"] == e["source_id"] and e["quote"] in row.get("snippet", "")
                for row in rows
            )
            for e in group["all_of"]
        )
        for group in question["evidence_groups"]
    )


async def prepare():
    build = code_commit()
    if PREPARED.exists():
        raise ValueError("PREPARED_TRIAL_ALREADY_EXISTS")
    if read(ROOT / "material-audit.json")["status"] != "PASS":
        raise ValueError("MATERIAL_AUDIT_REQUIRED")
    state, settings, (engine, factory) = environment()
    route = load_route(ROOT / "model-route-frozen.json")
    if route.primary.model != MODEL or route.fallbacks:
        raise ValueError("SINGLE_ALIYUN_MODEL_REQUIRED")
    questions = read(ROOT / "data/cases.json")["questions"]
    protocol, prices, chunks = (
        read(ROOT / "protocol.json"),
        read(ROOT / "pricing-frozen.json"),
        source_chunks(),
    )
    try:
        ctx = await context_for(factory, state)
        ws, actor = UUID(ctx.workspace_id), UUID(ctx.user_id)
        endpoint = route.primary
        async with factory() as db:
            credential = ProviderCredential(
                workspace_id=ws,
                provider=endpoint.provider,
                name="support-refund-sole-aliyun",
                base_url=endpoint.base_url,
            )
            encrypt_provider_credential_secret(
                credential,
                endpoint.api_key,
                cipher=ProviderCredentialCipher.from_settings(settings),
            )
            db.add(credential)
            await db.flush()
            profile = ModelProfile(
                workspace_id=ws,
                provider_credential_id=credential.id,
                model=MODEL,
                temperature=endpoint.temperature,
                max_tokens=endpoint.max_tokens,
                timeout_seconds=endpoint.timeout_seconds,
                capabilities=endpoint.capabilities,
                fallback_profile_id=None,
            )
            db.add(profile)
            await db.flush()
            specs = [
                *fixture_tool_specs(),
                {
                    "kind": "builtin",
                    "identity": "search_knowledge",
                    "description": "Search the pinned fictional policy snapshot.",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 6},
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                    "effect": "READ",
                    "risk_level": "LOW",
                    "approval_policy": "NEVER",
                },
            ]
            bindings = []
            for spec in specs:
                tool = Tool(workspace_id=ws, name=spec["identity"])
                db.add(tool)
                await db.flush()
                revision = ToolRevision(
                    workspace_id=ws,
                    tool_id=tool.id,
                    revision_number=1,
                    spec=spec,
                    spec_hash=canonical_json_hash(spec),
                    created_by=actor,
                )
                db.add(revision)
                await db.flush()
                bindings.append((tool.id, revision.id))
            await db.commit()
            runtime = {
                key: protocol["runtime_target"][key]
                for key in (
                    "max_steps",
                    "max_tool_calls",
                    "max_identical_calls",
                    "max_parallel_reads",
                    "context_budget",
                )
            }
            publisher = AgentPublishService(settings=settings)
            agent = await publisher.create_draft(
                db,
                ctx,
                name="support-refund-rag-frozen",
                system_prompt=SUPPORT_TEMPLATE.system_prompt,
                model_profile_id=profile.id,
                model_retry_policy={"max_attempts": 1},
                runtime_config=runtime,
                retrieval_config={
                    key: value
                    for key, value in state["retrieval_config"].items()
                    if key not in {"min_rerank_score", "superseded_rank_penalty"}
                },
            )
            db.add(
                AgentKnowledgeBinding(
                    workspace_id=ws,
                    agent_id=agent.id,
                    knowledge_base_id=UUID(state["knowledge"]["knowledge_base_id"]),
                    binding_mode="PINNED",
                    snapshot_id=UUID(state["knowledge"]["snapshot_id"]),
                )
            )
            for tid, rid in bindings:
                db.add(
                    AgentTool(workspace_id=ws, agent_id=agent.id, tool_id=tid, tool_revision_id=rid)
                )
            await db.commit()
            version = await publisher.publish(db, ctx, agent.id)
            stored = await db.get(AgentVersion, version.id)
            frozen = parse_frozen_agent_spec(stored.resolved_spec, workspace_id=ws)
            if (
                frozen.model_plan.fallbacks
                or frozen.wiki_binding
                or any(frozen.runtime["memory"].values())
            ):
                raise ValueError("EXPERIMENT_ISOLATION_DRIFT")
            ds = EvaluationDatasetService()
            dataset = await ds.create_dataset(
                db,
                context=ctx,
                name="support-refund-fictional-DEV-50",
                description="Assistant reviewed synthetic policies; not human gold or HOLDOUT.",
            )
            price = await ds.create_pricing_snapshot(
                db,
                context=ctx,
                name="Aliyun Beijing peak conservative 20261026",
                provider=endpoint.provider,
                model=MODEL,
                currency="CNY",
                input_price_per_1m=prices["input_per_1m"],
                output_price_per_1m=prices["output_per_1m"],
                cached_input_price_per_1m=prices["cached_input_per_1m"],
                effective_at=datetime(2026, 10, 26, tzinfo=UTC),
                source_note=prices["source_url"] + "; peak upper estimate, not invoice",
            )
            subsets = {
                "master": questions,
                "smoke": [q for q in questions if q["smoke_selected"]],
                "remaining": [q for q in questions if not q["smoke_selected"]],
            }
            experiments = {}
            for phase, subset in subsets.items():
                items = []
                for ordinal, q in enumerate(subset):
                    citations = sorted(
                        {
                            cid
                            for group in q["evidence_groups"]
                            for e in group["all_of"]
                            for cid, c in chunks.items()
                            if c["source_id"] == e["source_id"] and e["quote"] in c["text"]
                        }
                    )
                    items.append(
                        {
                            "case_key": q["case_id"],
                            "split": "DEV",
                            "category": "KNOWLEDGE_QA",
                            "input": {"question": q["input"]},
                            "expected": {
                                "answer": "；".join(q["key_facts"]),
                                "citations": citations,
                            },
                            "tags": [q["bucket"], "assistant-reviewed"],
                            "ordinal": ordinal,
                            "source_provenance": {
                                "source_kind": "fictional",
                                "source_id": q["case_id"],
                                "review_status": "assistant-reviewed",
                                "evidence_groups": q["evidence_groups"],
                            },
                        }
                    )
                dv = await ds.create_version(db, context=ctx, dataset_id=dataset.id, items=items)
                await ds.publish_version(db, context=ctx, dataset_id=dataset.id, version_id=dv.id)
                entry = {
                    "dataset_version_id": str(dv.id),
                    "case_ids": [q["case_id"] for q in subset],
                }
                if phase != "master":
                    service = ExperimentService()
                    exp = await service.create_experiment(
                        db,
                        context=ctx,
                        name="support-refund-" + phase,
                        description=(
                            "A production runtime; B request-preserving diagnostic sidecar. "
                            "Semantic scoring remains assistant-reviewed."
                        ),
                        dataset_version_id=dv.id,
                        split="DEV",
                        purpose="DEVELOPMENT",
                        repetitions=1,
                    )
                    await service.add_variant(
                        db,
                        context=ctx,
                        experiment_id=exp.id,
                        label="A",
                        agent_version_id=version.id,
                        pricing_snapshot_id=price.id,
                        ordinal=0,
                        variant_metadata={
                            "driver": "support-refund-rag-v1",
                            "B": "controlled observation replay",
                        },
                    )
                    await service.finalize_experiment(db, context=ctx, experiment_id=exp.id)
                    entry["experiment_id"] = str(exp.id)
                experiments[phase] = entry
            save(
                PREPARED,
                {
                    "build_sha": build,
                    "agent_id": str(agent.id),
                    "agent_version_id": str(version.id),
                    "resolved_spec": stored.resolved_spec,
                    "resolved_spec_hash": version.resolved_spec_hash,
                    "pricing_snapshot_id": str(price.id),
                    "experiments": experiments,
                    "model_dispatches": 0,
                    "B_scope": "controlled diagnostic sidecar, not a second production variant",
                },
            )
        print(json.dumps({"status": "PUBLISHED", "cases": 50, "model_dispatches": 0}))
    finally:
        await engine.dispose()


async def admission():
    prepared = read(PREPARED)
    state, settings, (engine, factory) = environment()
    chunks = source_chunks()
    try:
        ctx = await context_for(factory, state)
        frozen = parse_frozen_agent_spec(
            prepared["resolved_spec"], workspace_id=UUID(ctx.workspace_id)
        )
        budget = ContextBudgetPolicy(
            frozen, ContextBudgetConfig(**frozen.runtime["context_budget"])
        )
        retriever = SessionScopedKnowledgeRetriever(
            session_factory=factory,
            components=production_retrieval_components(settings),
            rrf_k=settings.knowledge_rrf_k,
        )
        store = read_order_store(ROOT / "data/orders.json")
        runtime = ToolRuntime(
            session_factory=factory, registry=FixtureToolRegistry(store=store, retriever=retriever)
        )
        async with factory() as db:
            defs = await PublishedToolResolver(db).list_definitions(
                workspace_id=UUID(ctx.workspace_id),
                agent_version_id=UUID(prepared["agent_version_id"]),
            )
        tools = tuple(ModelToolDefinition(d.identity, d.input_schema, d.description) for d in defs)
        results = []
        for q in read(ROOT / "data/cases.json")["questions"]:
            order = store.get(q["order_fixture"]["order_id"])
            scope = FixtureCaseScope(
                q["case_id"], order["order_id"], order["customer_id"], q["business_time"]
            )
            messages = [
                ModelMessage("system", _RUNTIME_POLICY),
                ModelMessage("system", frozen.system_prompt),
                ModelMessage("user", q["input"]),
            ]
            raw, observations = {}, {}
            with case_scope(scope):
                for identity, args in [
                    ("get_order", {"order_id": scope.order_id}),
                    ("get_refund_status", {"order_id": scope.order_id}),
                    ("query_customer", {"customer_ref": scope.customer_id}),
                    ("search_knowledge", {"query": q["input"], "limit": 6}),
                ]:
                    cid = "admission-" + identity
                    result = await runtime.execute(
                        context=ctx,
                        agent_version_id=prepared["agent_version_id"],
                        tool_identity=identity,
                        arguments=args,
                        tool_call_id=cid,
                    )
                    if result.status.value != "SUCCESS":
                        raise ValueError(
                            "GOVERNED_READ_FAILED:" + identity + ":" + str(result.error_code)
                        )
                    raw[identity] = asdict(result)
                    if identity == "search_knowledge":
                        payload = project_rag_evidence_payload(
                            {"status": "SUCCESS", "trust": "UNTRUSTED", "data": result.data},
                            frozen.runtime["context_budget"]["max_retrieval_tokens"],
                            ApproxTokenEstimator(),
                        )
                        if payload is None:
                            raise ValueError("KNOWLEDGE_PROJECTION_REJECTED")
                    else:
                        payload = _bounded_tool_result(
                            result,
                            max_tool_result_tokens=frozen.runtime["context_budget"][
                                "max_tool_result_tokens"
                            ],
                        )
                    observations[identity] = payload
                    messages.extend(
                        [
                            ModelMessage(
                                "assistant", tool_calls=(ModelToolCall(identity, args, cid),)
                            ),
                            ModelMessage(
                                "tool",
                                json.dumps(payload, ensure_ascii=False),
                                name=identity,
                                tool_call_id=cid,
                            ),
                        ]
                    )
            admitted = budget.admit(_categorize_messages(messages), tool_definitions=tools)
            rows = visible(admitted.messages, chunks)
            record = {
                "case_id": q["case_id"],
                "answerability": q["answerability"],
                "gold_complete": coverage(q, rows),
                "raw_tools": raw,
                "observations": observations,
                "model_view": asdict(admitted),
                "citation_identity_valid": True,
            }
            save(PRIVATE / "admission" / (q["case_id"] + ".json"), record)
            results.append(
                {
                    k: record[k]
                    for k in (
                        "case_id",
                        "answerability",
                        "gold_complete",
                        "citation_identity_valid",
                    )
                }
            )
        save(
            ROOT / "admission-summary.json",
            {
                "status": "PASS",
                "scope": (
                    "governed READ and final-budget replay; zero model calls, not actual A queries"
                ),
                "cases": results,
                "model_dispatches": 0,
            },
        )
        print(
            json.dumps(
                {"status": "GOVERNED_ADMISSION_REPLAY_COMPLETE", "cases": 50, "model_dispatches": 0}
            )
        )
    finally:
        await engine.dispose()


class Meter:
    """Physical dispatch reservation, persisted before the network call."""

    def __init__(self):
        self.path = PRIVATE / "attempt-ledger.json"
        self.data = read(self.path) if self.path.exists() else {"attempts": [], "stopped": False}
        self.case_id, self.arm = None, None

    def flush(self):
        save(self.path, self.data)

    async def acompletion(self, **kwargs):
        import litellm

        attempts = self.data["attempts"]
        limit = 3 if self.arm == "A" else 1
        if (
            self.data["stopped"]
            or len(attempts) >= 200
            or sum(a["case_id"] == self.case_id and a["arm"] == self.arm for a in attempts) >= limit
        ):
            self.data.update(stopped=True, stop_reason="PHYSICAL_DISPATCH_LIMIT")
            self.flush()
            raise ValueError("PHYSICAL_DISPATCH_LIMIT")
        if (
            kwargs["model"] != "openai/" + MODEL
            or kwargs["max_tokens"] != 2048
            or kwargs["num_retries"] != 0
            or kwargs.get("stream")
        ):
            raise ValueError("FROZEN_PROVIDER_REQUEST_DRIFT")
        reserve = Decimal("0.081920")
        if reserve * (len(attempts) + 1) > Decimal("20"):
            raise ValueError("CNY_BUDGET_EXHAUSTED")
        attempt = {
            "case_id": self.case_id,
            "arm": self.arm,
            "status": "DISPATCHED",
            "reserved_cny": str(reserve),
            "at": datetime.now(UTC).isoformat(),
            "actual_model": None,
            "usage": None,
            "cost_estimate_cny": None,
        }
        attempts.append(attempt)
        self.flush()
        started = time.perf_counter()
        try:
            response = await litellm.acompletion(**kwargs)
            attempt["actual_model"] = response.model
            usage = response.usage.model_dump() if response.usage else None
            attempt.update(status="RECEIVED", usage=usage)
            if response.model != MODEL:
                raise ValueError("ACTUAL_MODEL_IDENTITY_DRIFT")
            if (
                not usage
                or usage.get("prompt_tokens") is None
                or usage.get("completion_tokens") is None
            ):
                raise ValueError("USAGE_UNKNOWN")
            if usage["prompt_tokens"] > 32768 or usage["completion_tokens"] > 2048:
                raise ValueError("BILLING_BOUND_EXCEEDED")
            attempt["cost_estimate_cny"] = str(
                (Decimal(usage["prompt_tokens"]) * 2 + Decimal(usage["completion_tokens"]) * 8)
                / 1_000_000
            )
            return response
        except BaseException as error:
            attempt.update(
                status="ERROR_KNOWN_RESPONSE"
                if getattr(error, "status_code", None)
                else "UNKNOWN_OUTCOME",
                error_type=type(error).__name__,
                http_status=getattr(error, "status_code", None),
            )
            self.data.update(
                stopped=True, stop_reason=attempt["status"] + ":" + type(error).__name__
            )
            raise
        finally:
            attempt["latency_ms"] = round((time.perf_counter() - started) * 1000)
            self.flush()


class RecordingPrepared:
    def __init__(self, inner, calls):
        self.inner, self.calls = inner, calls

    async def generate_resolved(self, context, plan, request):
        record = {"request": request, "response": None}
        self.calls.append(record)
        response = await self.inner.generate_resolved(context, plan, request)
        record["response"] = response
        return response


class RecordingGateway:
    def __init__(self, inner, calls):
        self.inner, self.calls = inner, calls

    async def prepare_resolved(self, *args, **kwargs):
        return RecordingPrepared(await self.inner.prepare_resolved(*args, **kwargs), self.calls)


def replay_request(original, question, chunks, budget):
    # Use only a complete source scheme's raw snapshot chunks. Keep adjacent
    # context, dates, applicability and injection text; never inject gold facts.
    source_ids = {e["source_id"] for e in question["evidence_groups"][0]["all_of"]}
    selected = [c for c in chunks.values() if c["source_id"] in source_ids]
    records = [
        {
            k: c[k]
            for k in (
                "chunk_id",
                "document_id",
                "document_revision_id",
                "locator",
                "document_name",
                "effective_date",
                "superseded",
            )
        }
        | {"snippet": c["text"]}
        for c in selected
    ]
    refs = [
        {k: c[k] for k in ("snapshot_id", "knowledge_base_id", "document_revision_id", "chunk_id")}
        for c in selected
    ]
    payload = {
        "status": "SUCCESS",
        "trust": "UNTRUSTED",
        "data": {"results": records, "evidence_refs": refs},
    }
    messages = list(original.messages)
    indices = [
        i for i, m in enumerate(messages) if m.role == "tool" and m.name == "search_knowledge"
    ]
    if not indices:
        raise ValueError("B_UNCONSTRUCTABLE_NO_KNOWLEDGE_OBSERVATION")
    # Preserve every message and tool-call ID. Distribute source records once
    # across the first knowledge observation; other searches get empty evidence.
    for n, i in enumerate(indices):
        source = (
            payload
            if n == 0
            else {
                "status": "SUCCESS",
                "trust": "UNTRUSTED",
                "data": {"results": [], "evidence_refs": []},
            }
        )
        projected = project_rag_evidence_payload(
            source, budget.config.max_retrieval_tokens, ApproxTokenEstimator()
        )
        if projected is None:
            raise ValueError("B_SOURCE_PROJECTION_FAILED")
        messages[i] = replace(messages[i], content=json.dumps(projected, ensure_ascii=False))
    admitted = budget.admit(_categorize_messages(messages), tool_definitions=original.tools)
    # A/B must preserve business results, system prompt and message structure.
    if len(admitted.messages) != len(original.messages):
        raise ValueError("B_MESSAGE_STRUCTURE_CHANGED_BY_ADMISSION")
    for i, (old, new) in enumerate(zip(original.messages, admitted.messages, strict=True)):
        if i not in indices and old != new:
            raise ValueError("B_NON_KNOWLEDGE_MESSAGE_CHANGED")
    return replace(original, messages=admitted.messages, tools=admitted.tool_definitions)


async def execute(phase):
    prepared, admission_report = read(PREPARED), read(ROOT / "admission-summary.json")
    if admission_report["status"] != "PASS" or code_commit() != prepared["build_sha"]:
        raise ValueError("FROZEN_PREFLIGHT_OR_BUILD_DRIFT")
    phase_path = PRIVATE / (phase + "-execution.json")
    if phase_path.exists():
        raise ValueError("PHASE_ALREADY_ATTEMPTED_NO_AUTOMATIC_RETRY")
    if phase == "remaining" and read(PRIVATE / "smoke-execution.json").get("status") != "COMPLETE":
        raise ValueError("SMOKE_ACCEPTANCE_REQUIRED")
    if phase == "remaining" and read(PRIVATE / "smoke-review.json").get("status") != "PASS":
        raise ValueError("SMOKE_SEMANTIC_AND_CITATION_REVIEW_REQUIRED")
    state, settings, (engine, factory) = environment()
    chunks, meter = source_chunks(), Meter()
    questions = {q["case_id"]: q for q in read(ROOT / "data/cases.json")["questions"]}
    records = {}
    save(phase_path, {"status": "RUNNING", "records": records})
    try:
        ctx = await context_for(factory, state)
        frozen = parse_frozen_agent_spec(
            prepared["resolved_spec"], workspace_id=UUID(ctx.workspace_id)
        )
        budget = ContextBudgetPolicy(
            frozen, ContextBudgetConfig(**frozen.runtime["context_budget"])
        )
        components = production_retrieval_components(settings)
        for component in (components.dense, components.sparse, components.reranker):
            await asyncio.to_thread(component.warm)
        store = read_order_store(ROOT / "data/orders.json")
        retriever = SessionScopedKnowledgeRetriever(
            session_factory=factory, components=components, rrf_k=settings.knowledge_rrf_k
        )
        adapter = LiteLLMProviderAdapter(client=meter)
        calls = []

        def gateway(db):
            return RecordingGateway(
                SqlAlchemyModelGateway(
                    db,
                    adapter=adapter,
                    credential_cipher=ProviderCredentialCipher.from_settings(settings),
                ),
                calls,
            )

        runtime = AgentRunService(
            factory,
            model_gateway_factory=gateway,
            tool_runtime=ToolRuntime(
                session_factory=factory,
                registry=FixtureToolRegistry(store=store, retriever=retriever),
            ),
            thread_context_provider=SqlAlchemyThreadContextProvider(factory),
        )
        threads = ThreadService(factory, run_service=runtime)

        class Driver:
            async def prepare(self, *, run, variant, item):
                if meter.data["stopped"]:

                    async def skipped():
                        return CaseExecutionObservation(
                            observation={
                                **observation_identity("runtime", {}),
                                "status": "NOT_ATTEMPTED",
                            },
                            failure_code="NOT_ATTEMPTED_AFTER_STOP",
                            aggregate_billing_complete=False,
                        )

                    return PreparedCaseExecution(None, skipped)
                q = questions[item.case_key]
                order = store.get(q["order_fixture"]["order_id"])
                scope = FixtureCaseScope(
                    q["case_id"], order["order_id"], order["customer_id"], q["business_time"]
                )
                thread = await threads.create_thread(
                    ctx,
                    agent_id=UUID(prepared["agent_id"]),
                    title=q["case_id"],
                    kind=SUPPORT_TEMPLATE.thread_kind,
                )
                turn = await threads.open_turn(ctx, thread.id, user_input=q["input"])
                agent_run = await runtime.prepare_run(
                    ctx,
                    agent_version_id=variant.agent_version_id,
                    input_text=q["input"],
                    thread_id=thread.id,
                    thread_turn_id=turn.id,
                )
                await threads.attach_run(ctx, turn_id=turn.id, run_id=agent_run.id)

                async def perform():
                    calls.clear()
                    meter.case_id, meter.arm = q["case_id"], "A"
                    start = len(meter.data["attempts"])
                    with case_scope(scope):
                        a = await runtime.execute_prepared_run(ctx, run_id=agent_run.id)
                    rec = {
                        "case_id": q["case_id"],
                        "thread_id": str(thread.id),
                        "agent_run_id": str(a.run_id),
                        "A_status": a.status,
                        "A_failure_code": a.failure_code,
                        "A_answer": a.final_output,
                        "B_status": "NOT_ATTEMPTED",
                        "B_answer": None,
                        "semantic_review": "PENDING",
                    }
                    save(
                        PRIVATE / "requests" / (q["case_id"] + "-A.json"),
                        [
                            {
                                "request": asdict(c["request"]),
                                "response": asdict(c["response"]) if c["response"] else None,
                            }
                            for c in calls
                        ],
                    )
                    try:
                        final = calls[-1] if calls else None
                        if (
                            a.status != "SUCCEEDED"
                            or final is None
                            or final["response"] is None
                            or final["response"].tool_calls
                        ):
                            raise ValueError("A_NO_LEGAL_FINAL_REQUEST")
                        names = {m.name for m in final["request"].messages if m.role == "tool"}
                        if not {"get_order", "search_knowledge"}.issubset(names):
                            raise ValueError("A_REQUIRED_TOOL_CHAIN_MISSING")
                        if order.get("refund") and "get_refund_status" not in names:
                            raise ValueError("A_REFUND_LOOKUP_MISSING")
                        view = visible(final["request"].messages, chunks)
                        rec["A_gold_complete"] = coverage(q, view)
                        cited = re.findall(r"\b[0-9a-f]{64}\b", a.final_output or "")
                        if not cited:
                            raise ValueError("A_POLICY_CITATIONS_MISSING")
                        if any(cid not in {r["chunk_id"] for r in view} for cid in cited):
                            raise ValueError("A_CITATION_ID_NOT_VISIBLE")
                        b_req = replay_request(final["request"], q, chunks, budget)
                        b_view = visible(b_req.messages, chunks)
                        rec["B_gold_complete"] = coverage(q, b_view)
                        if not rec["B_gold_complete"]:
                            raise ValueError("B_SUFFICIENT_SOURCE_NOT_ADMITTED")
                        meter.arm = "B"
                        save(PRIVATE / "requests" / (q["case_id"] + "-B.json"), asdict(b_req))
                        async with factory() as db:
                            b = await SqlAlchemyModelGateway(
                                db,
                                adapter=adapter,
                                credential_cipher=ProviderCredentialCipher.from_settings(settings),
                            ).generate_resolved(ctx, frozen.model_plan, b_req)
                        if b.tool_calls:
                            raise ValueError("B_DID_NOT_RETURN_FINAL_ANSWER")
                        rec.update(B_status="RECEIVED", B_answer=b.content)
                    except Exception as error:
                        meter.data.update(stopped=True, stop_reason=str(error))
                        meter.flush()
                        rec["stop_reason"] = str(error)
                        if rec["B_status"] == "NOT_ATTEMPTED":
                            rec["B_status"] = "UNCONSTRUCTABLE"
                    records[q["case_id"]] = rec
                    save(
                        phase_path,
                        {
                            "status": "STOPPED" if meter.data["stopped"] else "RUNNING",
                            "records": records,
                        },
                    )
                    attempts = meter.data["attempts"][start:]
                    known = all(a.get("usage") for a in attempts)
                    usage = [a["usage"] for a in attempts if a.get("usage")]
                    return CaseExecutionObservation(
                        observation={
                            **observation_identity("runtime", {"status": "agent_run"}),
                            "status": a.status,
                            "observed_agent_status": a.status,
                            "output_hash": canonical_json_hash(a.final_output or ""),
                            "diagnostic_B_status": rec["B_status"],
                        },
                        agent_run_id=a.run_id,
                        input_tokens=sum(u["prompt_tokens"] for u in usage) if known else None,
                        output_tokens=sum(u["completion_tokens"] for u in usage) if known else None,
                        total_tokens=sum(u["total_tokens"] for u in usage) if known else None,
                        aggregate_billing_complete=known,
                        failure_code=a.failure_code
                        or ("DIAGNOSTIC_STOP" if meter.data["stopped"] else None),
                    )

                return PreparedCaseExecution(agent_run.id, perform)

        exp = prepared["experiments"][phase]
        async with factory() as db:
            run, _ = await ExperimentService().create_run(
                db, context=ctx, experiment_id=UUID(exp["experiment_id"])
            )
        await ExperimentRunner(factory, driver=Driver()).execute(
            run_id=run.id, owner="support-refund-" + uuid4().hex, settings=settings
        )
        for cid in exp["case_ids"]:
            records.setdefault(
                cid, {"case_id": cid, "A_status": "NOT_ATTEMPTED", "B_status": "NOT_ATTEMPTED"}
            )
        final_status = "STOPPED" if meter.data["stopped"] else "COMPLETE"
        save(
            phase_path,
            {
                "status": final_status,
                "formal_experiment_run_id": str(run.id),
                "records": records,
                "dispatches": len(meter.data["attempts"]),
                "stop_reason": meter.data.get("stop_reason"),
            },
        )
        print(
            json.dumps(
                {
                    "status": final_status,
                    "case_records": len(records),
                    "physical_dispatches": len(meter.data["attempts"]),
                }
            )
        )
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "admission", "smoke", "remaining"))
    parser.add_argument("--capture-private-content", action="store_true")
    args = parser.parse_args()
    if args.phase != "prepare" and not args.capture_private_content:
        parser.error("explicit --capture-private-content required for isolated diagnostic requests")
    with _exclusive_lock():
        asyncio.run(
            prepare()
            if args.phase == "prepare"
            else admission()
            if args.phase == "admission"
            else execute(args.phase)
        )


if __name__ == "__main__":
    main()
