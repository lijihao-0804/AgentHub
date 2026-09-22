"""Run the deterministic M4 Agent Runtime evaluation through production runtime paths."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from benchmarks.agent_runtime.metrics import (
    AgentEvaluationResult,
    RuntimeObservation,
    evaluate_results,
)
from benchmarks.agent_runtime.schema import (
    AgentEvaluationCase,
    AgentEvaluationDataset,
    ScriptStep,
    dataset_summary,
    load_dataset,
)
from packages.agent_runtime.models import (
    Agent,
    AgentKnowledgeBinding,
    AgentTool,
    Tool,
    ToolRevision,
)
from packages.agent_runtime.publish import AgentPublishService
from packages.agent_runtime.runtime import AgentRunService
from packages.control_plane.models import Organization, OrganizationMembership, User, Workspace
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.database import create_database
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.knowledge.contracts import (
    RetrievalQuery,
    RetrievalResult,
    RetrievalTrace,
    RetrievalTraceResult,
    RetrievalTraceStage,
    RetrievedEvidence,
)
from packages.knowledge.models import (
    Document,
    DocumentChunk,
    DocumentRevision,
    KnowledgeBase,
    KnowledgeSnapshot,
    KnowledgeSnapshotItem,
    RevisionIngestionStatus,
    RevisionLifecycleStatus,
)
from packages.model_gateway.contracts import ModelResponse, ModelToolCall
from packages.model_gateway.models import ModelProfile, ProviderCredential
from packages.threads import models as _thread_models  # noqa: F401
from packages.tools.builtins.calculator import calculate
from packages.tools.builtins.query_customer import query_customer
from packages.tools.builtins.search_knowledge import search_knowledge
from packages.tools.contracts import ToolDefinition, ToolExecutionContext
from packages.tools.models import Customer, Ticket
from packages.tools.registry import ToolHandler, ToolRegistry
from packages.tools.runtime import ToolRuntime

# AgentRun carries a composite foreign key to agent_threads. Register the
# thread table before SQLAlchemy flushes the benchmark's first run.

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATASET = PROJECT_ROOT / "benchmarks" / "agent_runtime" / "dataset.json"
DEFAULT_RESULT = (
    PROJECT_ROOT / "benchmarks" / "agent_runtime" / "results" / "m4-agent-runtime-v1.json"
)
DEFAULT_SUMMARY = PROJECT_ROOT / "docs" / "benchmark" / "m4-agent-runtime-baseline.md"


def _async_database_url(database_url: str) -> str:
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if database_url.startswith("postgres://"):
        return database_url.replace("postgres://", "postgresql+asyncpg://", 1)
    return database_url


def _context(user_id: UUID, organization_id: UUID, workspace_id: UUID) -> WorkspaceExecutionContext:
    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(
                request_id=f"m4e-{uuid4().hex}",
                trace_id=f"m4e-{uuid4().hex}",
                user_id=str(user_id),
            ),
            organization_id=str(organization_id),
            org_role="OWNER",
        ),
        workspace_id=str(workspace_id),
        workspace_role="DEVELOPER",
        permissions=frozenset(
            {"agent_edit", "agent_run", "tool_run", "knowledge_run", "workspace_read"}
        ),
    )


class ScriptedAgentModelGateway:
    """A provider-free model gateway that replays one validated case script."""

    def __init__(self, script: tuple[ScriptStep, ...]) -> None:
        self.script = script
        self.index = 0
        self.requests: list[Any] = []

    async def generate_resolved(self, context, plan, request) -> ModelResponse:
        del context, plan
        self.requests.append(request)
        if self.index >= len(self.script):
            return ModelResponse(content="", provider="m4e-scripted", model="m4e-scripted")
        step = self.script[self.index]
        self.index += 1
        if step.type == "FINAL":
            return ModelResponse(
                content=step.content or "", provider="m4e-scripted", model="m4e-scripted"
            )
        if step.type == "TOOL_CALL":
            calls = (ModelToolCall(step.name or "", step.arguments or {}, f"m4e-{self.index}"),)
        else:
            calls = tuple(
                ModelToolCall(name, arguments, f"m4e-{self.index}-{call_index}")
                for call_index, (name, arguments) in enumerate(step.calls)
            )
        return ModelResponse(
            content="", provider="m4e-scripted", model="m4e-scripted", tool_calls=calls
        )


class DeterministicKnowledgeRetriever:
    """Minimal retriever fixture used only behind the real search_knowledge builtin."""

    def __init__(
        self,
        *,
        workspace_id: UUID,
        snapshot_id: UUID,
        document_id: UUID,
        revision_id: UUID,
        chunk_id: str,
    ) -> None:
        self.workspace_id = workspace_id
        self.snapshot_id = snapshot_id
        self.document_id = document_id
        self.revision_id = revision_id
        self.chunk_id = chunk_id

    def _evidence(self, query: RetrievalQuery) -> RetrievedEvidence:
        return RetrievedEvidence(
            document_id=str(self.document_id),
            document_revision_id=str(self.revision_id),
            chunk_id=self.chunk_id,
            source="m4e-knowledge-fixture",
            locator={"type": "section", "section_key": "launch"},
            text=f"Synthetic knowledge evidence for {query.text}.",
            retrieval_score=1.0,
            rerank_score=1.0,
            metadata={"benchmark": "m4-agent-runtime-v1"},
        )

    async def retrieve(
        self, context: WorkspaceExecutionContext, query: RetrievalQuery
    ) -> list[RetrievedEvidence]:
        del context
        return [self._evidence(query)]

    async def retrieve_with_trace(
        self, context: WorkspaceExecutionContext, query: RetrievalQuery
    ) -> RetrievalResult:
        del context
        result = RetrievalTraceResult(chunk_id=self.chunk_id, rank=1, score=1.0)
        stage = RetrievalTraceStage(latency_ms=0.0, results=(result,))
        trace = RetrievalTrace(
            snapshot_id=query.knowledge_snapshot_id,
            dense=stage,
            sparse=stage,
            fusion=stage,
            rerank=stage,
            total_latency_ms=0.0,
        )
        return RetrievalResult(evidence=(self._evidence(query),), trace=trace)


async def _counting_handler(
    identity: str,
    counters: Counter[str],
    handler: ToolHandler,
    context: ToolExecutionContext,
    definition: ToolDefinition,
    arguments: dict[str, Any],
    session: AsyncSession | None,
) -> dict[str, Any]:
    counters[identity] += 1
    return await handler(context, definition, arguments, session)


def _registry(counters: Counter[str], retriever: DeterministicKnowledgeRetriever) -> ToolRegistry:
    async def calculator_handler(context, definition, arguments, session):
        return await _counting_handler(
            "calculator", counters, calculate, context, definition, arguments, session
        )

    async def customer_handler(context, definition, arguments, session):
        return await _counting_handler(
            "query_customer", counters, query_customer, context, definition, arguments, session
        )

    async def search_handler(context, definition, arguments, session):
        counters["search_knowledge"] += 1
        return await search_knowledge(context, definition, arguments, session, retriever=retriever)

    overrides: dict[str, ToolHandler] = {
        "calculator": calculator_handler,
        "query_customer": customer_handler,
        "search_knowledge": search_handler,
    }
    return ToolRegistry(retriever=retriever, handler_overrides=overrides)


def _tool_spec(identity: str, *, approval_policy: str) -> dict[str, Any]:
    schemas = {
        "calculator": {
            "type": "object",
            "properties": {"expression": {"type": "string"}},
            "required": ["expression"],
        },
        "query_customer": {
            "type": "object",
            "properties": {
                "customer_ref": {"type": "string"},
                "include_open_tickets": {"type": "boolean"},
            },
            "required": ["customer_ref", "include_open_tickets"],
        },
        "search_knowledge": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query", "limit"],
        },
    }
    return {
        "kind": "builtin",
        "identity": identity,
        "description": f"Synthetic M4-E {identity} read tool",
        "input_schema": schemas[identity],
        "effect": "READ",
        "risk_level": "LOW",
        "approval_policy": approval_policy,
    }


async def _seed_environment(factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
    label = uuid4().hex
    async with factory() as session:
        user = User(
            email=f"m4e-{label}@benchmark.invalid",
            normalized_email=f"m4e-{label}@benchmark.invalid",
            password_hash="benchmark-only",
        )
        session.add(user)
        await session.flush()
        organization = Organization(name=f"M4-E {label}", created_by=user.id)
        session.add(organization)
        await session.flush()
        session.add(
            OrganizationMembership(organization_id=organization.id, user_id=user.id, role="OWNER")
        )
        workspace = Workspace(organization_id=organization.id, name=f"M4-E {label}")
        session.add(workspace)
        await session.flush()
        credential = ProviderCredential(
            workspace_id=workspace.id,
            provider="fake",
            name=f"m4e-credential-{label}",
            secret="benchmark-only-secret",
        )
        session.add(credential)
        await session.flush()
        profile = ModelProfile(
            workspace_id=workspace.id,
            provider_credential_id=credential.id,
            model="m4-scripted",
            temperature=0,
            max_tokens=256,
            timeout_seconds=5,
            capabilities={"tool_calling": True, "max_context_tokens": 8192},
        )
        session.add(profile)
        await session.flush()
        agent = Agent(
            workspace_id=workspace.id,
            name=f"m4e-agent-{label}",
            system_prompt="Use only the published READ tools.",
            model_profile_id=profile.id,
        )
        session.add(agent)
        await session.flush()

        knowledge_base = KnowledgeBase(workspace_id=workspace.id, name=f"M4-E KB {label}")
        session.add(knowledge_base)
        await session.flush()
        document = Document(
            workspace_id=workspace.id,
            knowledge_base_id=knowledge_base.id,
            name="synthetic-launch-notes.txt",
        )
        session.add(document)
        await session.flush()
        revision = DocumentRevision(
            workspace_id=workspace.id,
            knowledge_base_id=knowledge_base.id,
            document_id=document.id,
            revision_number=1,
            original_filename="synthetic-launch-notes.txt",
            blob_key=f"m4e/{label}/synthetic-launch-notes.txt",
            media_type="text/plain",
            file_size=64,
            lifecycle_status=RevisionLifecycleStatus.ACTIVE,
            ingestion_status=RevisionIngestionStatus.READY,
        )
        session.add(revision)
        await session.flush()
        chunk_id = f"m4e-{label}-chunk-launch"
        chunk = DocumentChunk(
            workspace_id=workspace.id,
            knowledge_base_id=knowledge_base.id,
            document_id=document.id,
            document_revision_id=revision.id,
            chunk_id=chunk_id,
            ordinal=0,
            normalized_content_hash=canonical_json_hash("Synthetic launch readiness notes."),
            text="Synthetic launch readiness notes.",
            locator={"type": "section", "section_key": "launch"},
        )
        session.add(chunk)
        membership = {
            "snapshot_schema_version": 1,
            "knowledge_base_id": str(knowledge_base.id),
            "items": [{"document_id": str(document.id), "document_revision_id": str(revision.id)}],
        }
        snapshot = KnowledgeSnapshot(
            workspace_id=workspace.id,
            knowledge_base_id=knowledge_base.id,
            content_hash=canonical_json_hash(membership),
            snapshot_schema_version=1,
        )
        session.add(snapshot)
        await session.flush()
        session.add(
            KnowledgeSnapshotItem(
                workspace_id=workspace.id,
                snapshot_id=snapshot.id,
                knowledge_base_id=knowledge_base.id,
                document_id=document.id,
                document_revision_id=revision.id,
            )
        )
        session.add(
            AgentKnowledgeBinding(
                workspace_id=workspace.id,
                agent_id=agent.id,
                knowledge_base_id=knowledge_base.id,
                binding_mode="PINNED",
                snapshot_id=snapshot.id,
            )
        )

        customer_specs = (
            ("C-ACME", "Acme Labs", "acme@example.test", "T-ACME"),
            ("C-NORTH", "Northstar Retail", "northstar@example.test", "T-NORTH"),
        )
        for customer_ref, name, email, ticket_ref in customer_specs:
            customer = Customer(
                workspace_id=workspace.id,
                customer_ref=customer_ref,
                name=name,
                email=email,
            )
            session.add(customer)
            await session.flush()
            session.add(
                Ticket(
                    workspace_id=workspace.id,
                    customer_id=customer.id,
                    ticket_ref=ticket_ref,
                    subject="Synthetic benchmark ticket",
                    status="OPEN",
                )
            )

        tools: dict[str, Tool] = {}
        for identity in ("calculator", "query_customer", "search_knowledge"):
            tool = Tool(workspace_id=workspace.id, name=identity)
            session.add(tool)
            await session.flush()
            tools[identity] = tool

        publish_service = AgentPublishService()
        tool_bindings: dict[str, AgentTool] = {}
        versions: dict[str, Any] = {}
        seed_context = _context(user.id, organization.id, workspace.id)
        for version_number, approval_policy in ((1, "NEVER"), (2, "ALWAYS")):
            revisions: dict[str, ToolRevision] = {}
            for identity in ("calculator", "query_customer", "search_knowledge"):
                spec = _tool_spec(identity, approval_policy=approval_policy)
                tool_revision = ToolRevision(
                    workspace_id=workspace.id,
                    tool_id=tools[identity].id,
                    revision_number=version_number,
                    spec=spec,
                    spec_hash=canonical_json_hash(spec),
                    created_by=user.id,
                )
                session.add(tool_revision)
                await session.flush()
                revisions[identity] = tool_revision
                if version_number == 1:
                    binding = AgentTool(
                        workspace_id=workspace.id,
                        agent_id=agent.id,
                        tool_id=tools[identity].id,
                        tool_revision_id=tool_revision.id,
                    )
                    session.add(binding)
                    tool_bindings[identity] = binding
                else:
                    tool_bindings[identity].tool_revision_id = tool_revision.id
            await session.flush()
            published = await publish_service.publish(session, seed_context, agent.id)
            versions[approval_policy] = published.id
        await session.commit()
        return {
            "context": seed_context,
            "base_version_id": versions["NEVER"],
            "approval_version_id": versions["ALWAYS"],
            "retriever": DeterministicKnowledgeRetriever(
                workspace_id=workspace.id,
                snapshot_id=snapshot.id,
                document_id=document.id,
                revision_id=revision.id,
                chunk_id=chunk_id,
            ),
        }


async def _run_case(
    factory: async_sessionmaker[AsyncSession],
    seed: dict[str, Any],
    case: AgentEvaluationCase,
) -> RuntimeObservation:
    started = time.perf_counter()
    counters: Counter[str] = Counter()
    gateway = ScriptedAgentModelGateway(case.model_script)
    service = AgentRunService(
        factory,
        model_gateway_factory=lambda session: gateway,
        tool_runtime=ToolRuntime(
            session_factory=factory,
            registry=_registry(counters, seed["retriever"]),
        ),
    )
    version_id = (
        seed["approval_version_id"]
        if case.category == "approval_unavailable"
        else seed["base_version_id"]
    )
    stage = "AgentRunService.run"
    try:
        result = await service.run(
            seed["context"],
            agent_version_id=version_id,
            input_text=case.input,
        )
        stage = "AgentRunService.list_steps"
        steps = await service.list_steps(seed["context"], result.run_id)
        tool_sequence: list[str] = []
        for step in steps:
            if step.kind != "TOOL_EXECUTE":
                continue
            identities = step.safe_metadata.get("tool_identities", [])
            if isinstance(identities, list):
                tool_sequence.extend(str(identity) for identity in identities)
        return RuntimeObservation(
            status=result.status,
            failure_code=result.failure_code,
            tool_sequence=tuple(tool_sequence),
            model_steps=result.model_step_count,
            tool_calls=result.tool_call_count,
            final_output=result.final_output,
            handler_calls=sum(counters.values()),
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
        )
    except Exception as error:
        diagnostic_type = type(error).__name__
        print(
            f"::error title=M4 case {case.case_id}::{diagnostic_type} during {stage}",
            file=sys.stderr,
        )
        return RuntimeObservation(
            status="FAILED",
            failure_code="RUNNER_ERROR",
            tool_sequence=(),
            model_steps=len(gateway.requests),
            tool_calls=0,
            final_output=None,
            handler_calls=sum(counters.values()),
            duration_ms=round((time.perf_counter() - started) * 1000, 3),
        )


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


async def run_benchmark(
    dataset: AgentEvaluationDataset, database_url: str
) -> AgentEvaluationResult:
    engine, factory = create_database(_async_database_url(database_url))
    try:
        seed = await _seed_environment(factory)
        observations: dict[str, RuntimeObservation] = {}
        for case in dataset.cases:
            observations[case.case_id] = await _run_case(factory, seed, case)
        return evaluate_results(dataset, observations, git_commit=_git_commit())
    finally:
        await engine.dispose()


def _summary_markdown(result: AgentEvaluationResult) -> str:
    dev_pass = sum(case.split == "dev" and case.case_pass for case in result.cases)
    dev_total = sum(case.split == "dev" for case in result.cases)
    holdout_pass = sum(case.split == "holdout" and case.case_pass for case in result.cases)
    holdout_total = sum(case.split == "holdout" for case in result.cases)
    lines = [
        "# M4 Agent Runtime Evaluation Baseline",
        "",
        "Status: PASS — deterministic runtime conformance baseline.",
        "",
        "This is a deterministic AgentHub runtime conformance benchmark. It does not measure",
        "general LLM reasoning quality and does not call a public LLM.",
        "",
        f"Dataset version: `{result.dataset_version}`",
        f"Dataset hash: `{result.dataset_hash}`",
        f"Git commit: `{result.git_commit}`",
        "",
        f"Cases: {len(result.cases)}",
        f"Dev: {dev_pass} / {dev_total} PASS",
        f"Holdout: {holdout_pass} / {holdout_total} PASS",
        "",
        "## Metrics",
        "",
        "| Split | Case Pass Rate | Tool Sequence Accuracy | "
        "Terminal Status Accuracy | Failure Code Accuracy |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for split in ("dev", "holdout", "overall"):
        metrics = result.metrics(None if split == "overall" else split)
        lines.append(
            f"| {split} | {metrics['case_pass_rate']:.4f} | "
            f"{metrics['tool_sequence_accuracy']:.4f} | "
            f"{metrics['terminal_status_accuracy']:.4f} | {metrics['failure_code_accuracy']:.4f} |"
        )
    lines.extend(["", "## Category pass rate", "", "| Category | Pass rate |", "| --- | ---: |"])
    for category, rate in sorted(result.category_metrics().items()):
        lines.append(f"| {category} | {rate:.4f} |")
    lines.extend(["", "## Failure analysis", ""])
    failures = result.failure_analysis()
    lines.append("No failed cases." if not failures else json.dumps(failures, indent=2))
    lines.extend(
        [
            "",
            "The benchmark used a scripted fake ModelGateway, the real AgentRunService, real",
            "PostgreSQL persistence, the real ToolRuntime and ToolPolicy, and a deterministic",
            "retriever injected only behind the published `search_knowledge` builtin.",
        ]
    )
    return "\n".join(lines) + "\n"


def _write_outputs(result: AgentEvaluationResult, output: Path, summary: Path) -> None:
    payload = result.to_payload()
    payload["timestamp"] = datetime.now(UTC).isoformat()
    output.parent.mkdir(parents=True, exist_ok=True)
    summary.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary.write_text(_summary_markdown(result), encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--output", type=Path, default=DEFAULT_RESULT)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--validate-only", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    dataset = load_dataset(args.dataset)
    if args.validate_only:
        print(json.dumps(dataset_summary(dataset), sort_keys=True))
        return 0
    database_url = args.database_url or os.environ.get("AGENTHUB_TEST_DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("--database-url or AGENTHUB_TEST_DATABASE_URL is required")
    result = asyncio.run(run_benchmark(dataset, database_url))
    _write_outputs(result, args.output, args.summary)
    print(json.dumps(result.to_payload()["metrics"], sort_keys=True))
    return 0 if all(case.case_pass for case in result.cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["ScriptedAgentModelGateway", "run_benchmark", "main"]
