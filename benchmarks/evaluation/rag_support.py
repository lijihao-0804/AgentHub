"""Frozen DEV end-to-end knowledge tool experiment; no inline policy answers."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select

from benchmarks.evaluation.artifacts import write_json_atomic
from benchmarks.evaluation.budget import TrialBudget
from benchmarks.evaluation.formal_support import ROOT, assert_committed_code, context_for
from benchmarks.evaluation.live_pilot import ISOLATED_URL, PRICE_ID, BudgetedAdapter
from benchmarks.evaluation.reviewed_support_data import reviewed_source
from benchmarks.evaluation.support_retrieval import dev_dataset
from benchmarks.retrieval.runner import _index_corpus, _materialize_snapshot, _seed_corpus
from packages.agent_runtime.adapters.langgraph import configure_windows_asyncio_policy
from packages.agent_runtime.models import Agent, AgentRun, AgentVersion, Tool, ToolRevision
from packages.agent_runtime.runtime import AgentRunService
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.config.settings import Settings
from packages.core.database import create_database
from packages.evaluation.experiments import ExperimentService
from packages.evaluation.models import (
    EvaluationDatasetItem,
    EvaluationExperimentCaseResult,
    EvaluationExperimentRun,
)
from packages.evaluation.observations import retrieval_observation
from packages.evaluation.runner import (
    AgentRuntimeEvaluationDriver,
    ExperimentRunner,
    PreparedCaseExecution,
)
from packages.evaluation.service import EvaluationDatasetService
from packages.knowledge.composition import production_retrieval_components
from packages.knowledge.retrieval import SessionScopedKnowledgeRetriever
from packages.model_gateway.credentials import ProviderCredentialCipher
from packages.model_gateway.gateway import SqlAlchemyModelGateway
from packages.model_gateway.models import ModelProfile, ProviderCredential
from packages.tools.registry import ToolRegistry
from packages.tools.runtime import ToolRuntime

PROMPT = (
    "你是隔离测试客服。回答前必须调用 search_knowledge 检索冻结的虚构政策。"
    "仅使用实际检索结果中的事实，不猜测费用，不承诺客服已完成退款或保修。"
    "政策无依据时明确回答未知，不能把未知当作免费。"
    "每条政策事实后标注检索结果的原始 chunk_id，格式 [chunk_id]；不得编造引用。"
    "不要索要登记工单信息，本次只回答政策问题。"
)


def settings_for(collection):
    if os.environ.get("HF_HUB_OFFLINE") != "1":
        raise ValueError("CACHED_MODELS_ONLY")
    return Settings().model_copy(
        update={
            "database_url": ISOLATED_URL,
            "knowledge_qdrant_collection": collection,
        }
    )


async def prepare(source_plan: Path, output: Path):
    assert_committed_code()
    if output.exists():
        raise ValueError("RAG_PLAN_EXISTS")
    frozen = json.loads(source_plan.read_text(encoding="utf-8"))
    engine, factory = create_database(ISOLATED_URL)
    dataset = dev_dataset()
    settings = settings_for("agenthub_mi34_rag_" + uuid4().hex)
    try:
        seed_context, kb_id, chunks = await _seed_corpus(factory, dataset)
        context = await context_for(
            factory, UUID(seed_context.user_id), UUID(seed_context.workspace_id)
        )
        snapshot = await _materialize_snapshot(factory, context, kb_id)
        _index_corpus(settings, chunks)
        source_data = reviewed_source()
        source_cases = {c["case_id"]: c for c in source_data["cases"]}
        by_source = {}
        for chunk in chunks:
            by_source.setdefault(chunk.document_key, []).append(chunk.chunk.chunk_id)
        async with factory() as session:
            old_version = await session.get(AgentVersion, UUID(frozen["agent_version_ids"][1]))
            old_profile = await session.get(
                ModelProfile, UUID(old_version.resolved_spec["model"]["profile_id"])
            )
            old_credential = await session.get(
                ProviderCredential, old_profile.provider_credential_id
            )
            # Ciphertext is copied only inside the already authorized isolated DB.
            credential = ProviderCredential(
                workspace_id=UUID(context.workspace_id),
                provider=old_credential.provider,
                name="isolated-rag",
                secret_ciphertext=old_credential.secret_ciphertext,
                secret_version=old_credential.secret_version,
                base_url=old_credential.base_url,
            )
            session.add(credential)
            await session.flush()
            profile = ModelProfile(
                workspace_id=UUID(context.workspace_id),
                provider_credential_id=credential.id,
                model=old_profile.model,
                temperature=old_profile.temperature,
                max_tokens=800,
                timeout_seconds=60,
                capabilities=old_version.resolved_spec["model"]["capabilities"],
            )
            session.add(profile)
            await session.flush()
            agent = Agent(
                workspace_id=UUID(context.workspace_id),
                name="isolated-rag",
                system_prompt=PROMPT,
                model_profile_id=profile.id,
            )
            tool = Tool(workspace_id=UUID(context.workspace_id), name="search_knowledge")
            session.add_all([agent, tool])
            await session.flush()
            tool_spec = {
                "kind": "builtin",
                "identity": "search_knowledge",
                "description": "检索已冻结的客服政策并返回可引用的原始证据",
                "input_schema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
                "effect": "READ",
                "risk_level": "LOW",
                "approval_policy": "NEVER",
            }
            revision = ToolRevision(
                workspace_id=UUID(context.workspace_id),
                tool_id=tool.id,
                revision_number=1,
                spec=tool_spec,
                spec_hash=canonical_json_hash(tool_spec),
                created_by=UUID(context.user_id),
            )
            session.add(revision)
            await session.flush()
            versions = []
            for ordinal, strategy in enumerate(("DENSE", "HYBRID_RERANK")):
                spec = json.loads(json.dumps(old_version.resolved_spec))
                spec["agent_id"] = str(agent.id)
                spec["model"].update(profile_id=str(profile.id), credential_ref=str(credential.id))
                spec["prompt"] = {"prompt_version": 1, "system_prompt": PROMPT}
                spec["tools"] = [
                    {
                        "tool_revision_id": str(revision.id),
                        "tool_spec_hash": revision.spec_hash,
                        "effect": "READ",
                        "risk_level": "LOW",
                        "approval_policy": "NEVER",
                    }
                ]
                spec["retrieval"] = {
                    "knowledge_binding_mode": "PINNED",
                    "knowledge_snapshots": [
                        {
                            "snapshot_id": str(snapshot.snapshot_id),
                            "snapshot_hash": snapshot.content_hash,
                        }
                    ],
                    "retrieval_strategy": strategy,
                    "dense_top_k": 30,
                    "sparse_top_k": 30,
                    "candidate_top_k": 20,
                    "final_top_k": 5,
                }
                version = AgentVersion(
                    workspace_id=UUID(context.workspace_id),
                    agent_id=agent.id,
                    version_number=ordinal + 1,
                    spec_schema_version=1,
                    resolved_spec=spec,
                    resolved_spec_hash=canonical_json_hash(spec),
                    created_by=UUID(context.user_id),
                )
                session.add(version)
                versions.append(version)
            await session.commit()
        async with factory() as session:
            datasets = EvaluationDatasetService()
            stored_dataset = await datasets.create_dataset(
                session,
                context=context,
                name="rag-dev-24",
                description="DEV-only; assistant source labels, no human agreement claim",
            )
            items = []
            for ordinal, case in enumerate(dataset.cases):
                source = source_cases[case.case_id]
                no_answer = source["category"] == "unanswerable"
                expected = {"answer": source["reference_answer"]}
                if not no_answer:
                    expected["citations"] = [
                        id for group in source["source_ids"] for id in by_source[group]
                    ]
                items.append(
                    {
                        "case_key": case.case_id,
                        "split": "DEV",
                        "category": "NO_ANSWER" if no_answer else "KNOWLEDGE_QA",
                        "input": {"question": case.query},
                        "expected": expected,
                        "tags": ["synthetic", "rag", "assistant-reviewed"],
                        "source_provenance": {
                            "source_kind": "assistant_reviewed_synthetic",
                            "source_id": case.case_id,
                            "source_content_hash": source_data["content_hash"],
                        },
                        "ordinal": ordinal,
                    }
                )
            version = await datasets.create_version(
                session,
                context=context,
                dataset_id=stored_dataset.id,
                items=items,
                schema_version=1,
            )
            await datasets.publish_version(
                session, context=context, dataset_id=stored_dataset.id, version_id=version.id
            )
            pricing = await datasets.create_pricing_snapshot(
                session,
                context=context,
                name="peak upper",
                provider="deepseek",
                model="deepseek-flash",
                currency="CNY",
                input_price_per_1m="2",
                output_price_per_1m="8",
                cached_input_price_per_1m="2",
                effective_at=datetime(2026, 10, 2, tzinfo=UTC),
                source_note="Peak upper estimate, not invoice",
            )
            service = ExperimentService()
            experiment = await service.create_experiment(
                session,
                context=context,
                name="rag-dev-dense-v-hybrid",
                description=(
                    "Retrieval-strategy-only comparison; actual tool evidence, no inline policies"
                ),
                dataset_version_id=version.id,
                split="DEV",
                purpose="DEVELOPMENT",
                repetitions=3,
            )
            for ordinal, av in enumerate(versions):
                await service.add_variant(
                    session,
                    context=context,
                    experiment_id=experiment.id,
                    label=("DENSE", "HYBRID_RERANK")[ordinal],
                    agent_version_id=av.id,
                    pricing_snapshot_id=pricing.id,
                    ordinal=ordinal,
                    variant_metadata={
                        "driver": "rag-evidence-v1",
                        "citation_parser": "bracket-lowercase-sha256-v1",
                        "policy_delivery": "search_knowledge_only",
                    },
                )
            await service.finalize_experiment(session, context=context, experiment_id=experiment.id)
            run, _ = await service.create_run(session, context=context, experiment_id=experiment.id)
            write_json_atomic(
                output,
                {
                    "status": "FROZEN_RAG_DEV_PLAN",
                    "build_sha": experiment.build_sha,
                    "workspace_id": context.workspace_id,
                    "user_id": context.user_id,
                    "dataset_version_id": str(version.id),
                    "dataset_content_hash": version.content_hash,
                    "experiment_id": str(experiment.id),
                    "run_id": str(run.id),
                    "agent_version_ids": [str(v.id) for v in versions],
                    "collection": settings.knowledge_qdrant_collection,
                    "snapshot_id": str(snapshot.snapshot_id),
                    "snapshot_hash": snapshot.content_hash,
                    "source_content_hash": source_data["content_hash"],
                    "corpus_source_to_chunk_ids": by_source,
                    "planned_cases": 144,
                    "holdout_consumed": False,
                    "evaluator_manifest": experiment.evaluator_manifest,
                    "limitations": [
                        "8 synthetic policies",
                        "assistant labels not human",
                        "DEV-only retrieval comparison",
                        "exact-match answer score separate from semantic review",
                    ],
                },
            )
    finally:
        await engine.dispose()


class RecordingRetriever:
    def __init__(self, inner):
        self.inner = inner
        self.records = []

    async def retrieve_with_trace(self, context, query):
        result = await self.inner.retrieve_with_trace(context, query)
        self.records.append((query, result))
        return result

    async def retrieve(self, context, query):
        return list((await self.retrieve_with_trace(context, query)).evidence)


def rag_observation(output, actual, *, variant_hash):
    """Project only emitted citations and returned evidence, without label access."""
    return {
        "answer_hash": canonical_json_hash(output),
        "citation_ids": list(dict.fromkeys(re.findall(r"\[([0-9a-f]{64})\]", output))),
        "rag_evidence": {
            "version": "rag-evidence-v1",
            "retrieval_calls": [
                retrieval_observation(r, q, variant_hash=variant_hash) for q, r in actual
            ],
            "returned_chunk_ids": list(
                dict.fromkeys(e.chunk_id for _, r in actual for e in r.evidence)
            ),
            "output_hash": canonical_json_hash(output),
            "citation_parser": "bracket-lowercase-sha256-v1",
        },
    }


class RagDriver(AgentRuntimeEvaluationDriver):
    def __init__(self, runtime, contexts, recording):
        super().__init__(runtime, contexts)
        self.recording = recording

    async def prepare(self, *, run, variant, item):
        prepared = await super().prepare(run=run, variant=variant, item=item)

        async def execute():
            first = len(self.recording.records)
            result = await prepared.execute()
            actual = self.recording.records[first:]
            async with self.agent_run_service.session_factory() as session:
                stored = await session.get(AgentRun, result.agent_run_id)
                output = stored.final_output or ""
            observation = {
                **result.observation,
                **rag_observation(output, actual, variant_hash=variant.variant_hash),
            }
            return replace(result, observation=observation)

        return PreparedCaseExecution(
            prepared.agent_run_id, execute, prepared.preparation_observation
        )


async def execute(plan: Path, output: Path):
    assert_committed_code()
    if output.exists():
        raise ValueError("RAG_REPORT_EXISTS")
    frozen = json.loads(plan.read_text(encoding="utf-8"))
    checked = json.loads(plan.with_suffix(".preflight.json").read_text(encoding="utf-8"))
    if checked["status"] != "PASS" or checked["plan_hash"] != canonical_json_hash(frozen):
        raise ValueError("RAG_PREFLIGHT_REQUIRED")
    if (
        subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        != frozen["build_sha"]
    ):
        raise ValueError("FORMAL_BUILD_IDENTITY_CHANGED")
    settings = settings_for(frozen["collection"])
    engine, factory = create_database(ISOLATED_URL)
    budget = TrialBudget(
        ROOT / "live-budget.json", limit_cny=Decimal("50"), price_identity=PRICE_ID
    )
    adapter = BudgetedAdapter(budget)
    recording = RecordingRetriever(
        SessionScopedKnowledgeRetriever(
            session_factory=factory,
            components=production_retrieval_components(settings),
            rrf_k=settings.knowledge_rrf_k,
        )
    )
    runtime = AgentRunService(
        factory,
        model_gateway_factory=lambda db: SqlAlchemyModelGateway(
            db, adapter=adapter, credential_cipher=ProviderCredentialCipher.from_settings(settings)
        ),
        tool_runtime=ToolRuntime(
            session_factory=factory, registry=ToolRegistry(retriever=recording)
        ),
    )

    async def contexts(run):
        return await context_for(factory, run.created_by, run.workspace_id)

    report = {"status": "RUNNING", "plan": frozen, "cases": []}
    write_json_atomic(output, report)
    try:
        await ExperimentRunner(factory, driver=RagDriver(runtime, contexts, recording)).execute(
            run_id=UUID(frozen["run_id"]), owner="rag-" + uuid4().hex, settings=settings
        )
        async with factory() as session:
            run = await session.get(EvaluationExperimentRun, UUID(frozen["run_id"]))
            rows = await session.scalars(
                select(EvaluationExperimentCaseResult)
                .where(EvaluationExperimentCaseResult.experiment_run_id == run.id)
                .order_by(EvaluationExperimentCaseResult.created_at)
            )
            for row in rows:
                item = await session.get(EvaluationDatasetItem, row.dataset_item_id)
                report["cases"].append(
                    {
                        "id": str(row.id),
                        "case_id": item.case_key,
                        "variant_id": str(row.experiment_variant_id),
                        "repetition": row.repetition_index,
                        "status": row.status,
                        "failure_code": row.failure_code,
                        "observation": row.observation,
                        "output": (await session.get(AgentRun, row.agent_run_id)).final_output
                        if row.agent_run_id
                        else None,
                        "latency_ms": row.latency_ms,
                        "cost_amount": str(row.cost_amount)
                        if row.cost_amount is not None
                        else None,
                        "cost_currency": row.cost_currency,
                    }
                )
            report["status"] = run.status
    finally:
        report.update(
            calls=adapter.calls,
            local_failures=adapter.local_failures,
            allocated_cny=str(budget.allocated),
        )
        write_json_atomic(output, report)
        await engine.dispose()


async def preflight(plan: Path):
    """Controlled model, actual retrieval and mandatory context admission; zero paid calls."""
    from packages.model_gateway.contracts import ModelResponse, ModelToolCall

    assert_committed_code()
    frozen = json.loads(plan.read_text(encoding="utf-8"))
    if (
        subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        != frozen["build_sha"]
    ):
        raise ValueError("FORMAL_BUILD_IDENTITY_CHANGED")
    output = plan.with_suffix(".preflight.json")
    if output.exists():
        raise ValueError("PREFLIGHT_OUTPUT_EXISTS")
    settings = settings_for(frozen["collection"])
    engine, factory = create_database(ISOLATED_URL)
    recording = RecordingRetriever(
        SessionScopedKnowledgeRetriever(
            session_factory=factory,
            components=production_retrieval_components(settings),
            rrf_k=settings.knowledge_rrf_k,
        )
    )

    class Gateway:
        async def generate_resolved(self, context, resolved, request):
            if any(m.role == "tool" for m in request.messages):
                return ModelResponse(
                    content="controlled context admitted", provider="controlled", model="controlled"
                )
            question = next(m.content for m in reversed(request.messages) if m.role == "user")
            return ModelResponse(
                content="",
                provider="controlled",
                model="controlled",
                tool_calls=(ModelToolCall(name="search_knowledge", arguments={"query": question}),),
            )

    runtime = AgentRunService(
        factory,
        model_gateway_factory=lambda db: Gateway(),
        tool_runtime=ToolRuntime(
            session_factory=factory, registry=ToolRegistry(retriever=recording)
        ),
    )
    report = {
        "status": "RUNNING",
        "plan_hash": canonical_json_hash(frozen),
        "paid_calls": 0,
        "scope": "actual_retrieval_and_post_tool_context_admission",
        "cases": [],
    }
    try:
        context = await context_for(factory, UUID(frozen["user_id"]), UUID(frozen["workspace_id"]))
        async with factory() as session:
            items = list(
                await session.scalars(
                    select(EvaluationDatasetItem).where(
                        EvaluationDatasetItem.dataset_version_id
                        == UUID(frozen["dataset_version_id"])
                    )
                )
            )
        for version_id in frozen["agent_version_ids"]:
            for item in items:
                start = len(recording.records)
                result = await runtime.run(
                    context, agent_version_id=UUID(version_id), input_text=item.input["question"]
                )
                actual = recording.records[start:]
                report["cases"].append(
                    {
                        "case_id": item.case_key,
                        "agent_version_id": version_id,
                        "status": result.status,
                        "failure_code": result.failure_code,
                        "retrieval_calls": len(actual),
                        "returned_chunks": sum(len(r.evidence) for _, r in actual),
                    }
                )
        report["status"] = (
            "PASS"
            if len(report["cases"]) == 48
            and all(
                c["status"] == "SUCCEEDED"
                and c["retrieval_calls"] == 1
                and c["returned_chunks"] > 0
                for c in report["cases"]
            )
            else "FAIL"
        )
        if report["status"] != "PASS":
            raise ValueError("RAG_PREFLIGHT_FAILED")
    finally:
        write_json_atomic(output, report)
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "execute", "preflight"])
    parser.add_argument("--source-plan", type=Path)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    configure_windows_asyncio_policy()
    if args.action == "prepare":
        asyncio.run(prepare(args.source_plan, args.plan))
    elif args.action == "preflight":
        asyncio.run(preflight(args.plan))
    else:
        asyncio.run(execute(args.plan, args.output))
