"""Celery tasks for durable evaluation execution and reconciliation."""

from __future__ import annotations

import asyncio
import secrets
from uuid import UUID

from sqlalchemy import select

from apps.worker.celery_app import celery_app
from packages.agent_runtime.adapters.langgraph import LangGraphCheckpointAdapter
from packages.agent_runtime.runtime import AgentRunService
from packages.approvals import ApprovalService
from packages.control_plane.services import TenantService
from packages.core.config.settings import get_settings
from packages.core.database import create_database
from packages.core.execution_context.models import PrincipalContext
from packages.evaluation.approval import EvaluationApprovalActorProvider
from packages.evaluation.judge import (
    AnswerQualityJudge,
    ModelGatewayJudgeClient,
    judge_profile_from_manifest,
)
from packages.evaluation.models import EvaluationExperiment, EvaluationExperimentRun
from packages.evaluation.queue import CeleryExperimentRunQueue
from packages.evaluation.runner import (
    AgentRuntimeEvaluationDriver,
    ExperimentRunner,
    reconcile_experiment_runs,
)
from packages.knowledge.composition import production_retrieval_components
from packages.knowledge.retrieval import SessionScopedKnowledgeRetriever
from packages.mcp.runtime import McpActionExecutor, McpToolExecutor
from packages.memory.store import SqlAlchemyMemoryStore
from packages.model_gateway.credentials import ProviderCredentialCipher
from packages.model_gateway.gateway import SqlAlchemyModelGateway
from packages.observability import ProductionTraceSink
from packages.tools.actions import ActionRuntime
from packages.tools.audit import SqlAlchemyToolAuditSink
from packages.tools.registry import ToolRegistry
from packages.tools.runtime import ToolRuntime


@celery_app.task(
    bind=True,
    name="agenthub.execute_experiment_run",
    ignore_result=True,
    acks_late=True,
    reject_on_worker_lost=True,
)
def execute_experiment_run(_task, run_id: str) -> None:
    try:
        parsed_run_id = UUID(run_id)
    except ValueError:
        return
    asyncio.run(_execute_experiment_run(parsed_run_id))


async def _execute_experiment_run(run_id: UUID) -> None:
    settings = get_settings()
    engine, factory = create_database(settings.database_url)
    try:
        judge = await _build_judge(settings, factory, run_id)
        driver = await _build_driver(settings, factory, judge=judge)
        trace_sink = ProductionTraceSink()
        await ExperimentRunner(factory, driver=driver, trace_sink=trace_sink).execute(
            run_id=run_id,
            owner=secrets.token_urlsafe(24),
            settings=settings,
        )
    finally:
        await engine.dispose()


class _SessionScopedJudgeClient:
    """Judge transport that opens a short-lived session per call.

    The gateway is session-scoped, and a run can take minutes; holding one session
    open across the whole run just to be able to judge at the end would keep a
    connection idle for the duration.  Credentials stay where they are -- the
    gateway decrypts the ``provider_credentials`` row itself.
    """

    def __init__(self, factory, settings, context, model_profile_id: UUID) -> None:
        self.factory = factory
        self.settings = settings
        self.context = context
        self.model_profile_id = model_profile_id

    async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        async with self.factory() as session:
            gateway = SqlAlchemyModelGateway(
                session,
                credential_cipher=ProviderCredentialCipher.from_settings(self.settings),
                trace_sink=ProductionTraceSink(),
            )
            client = ModelGatewayJudgeClient(gateway, self.context, self.model_profile_id)
            return await client.complete(system_prompt=system_prompt, user_prompt=user_prompt)


async def _build_judge(settings, factory, run_id: UUID) -> AnswerQualityJudge | None:
    """Rebuild the judge this run's experiment was frozen with, if it has one.

    The judge identity comes from the experiment's manifest, not from re-resolving a
    model profile: the profile may have been edited since the experiment was created,
    and an experiment scored by a judge other than the one it was frozen with is not
    the experiment it claims to be.
    """

    async with factory() as session:
        run = await session.scalar(
            select(EvaluationExperimentRun).where(EvaluationExperimentRun.id == run_id)
        )
        if run is None:
            return None
        experiment = await session.scalar(
            select(EvaluationExperiment).where(
                EvaluationExperiment.workspace_id == run.workspace_id,
                EvaluationExperiment.id == run.experiment_id,
            )
        )
        profile = judge_profile_from_manifest(
            experiment.evaluator_manifest if experiment is not None else None
        )
        if profile is None:
            return None
        principal = PrincipalContext(
            request_id=f"evaluation:{run.id}",
            trace_id=f"evaluation:{run.id}",
            user_id=str(run.created_by),
        )
        context = (
            await TenantService().get_workspace_access(
                session, principal=principal, workspace_id=run.workspace_id
            )
        ).context
    client = _SessionScopedJudgeClient(factory, settings, context, UUID(profile.profile_id))
    return AnswerQualityJudge(profile, client)


async def _build_driver(settings, factory, *, judge: AnswerQualityJudge | None = None):
    trace_sink = ProductionTraceSink()
    components = production_retrieval_components(settings)
    retriever = SessionScopedKnowledgeRetriever(
        session_factory=factory,
        components=components,
        rrf_k=settings.knowledge_rrf_k,
        min_rerank_score=settings.knowledge_min_rerank_score,
        superseded_rank_penalty=settings.knowledge_superseded_rank_penalty,
        trace_sink=trace_sink,
    )
    approval_service = ApprovalService(factory, ttl_seconds=settings.approval_ttl_seconds)
    approval_actor_provider = EvaluationApprovalActorProvider(factory)
    # An evaluation run executes the same published agent the API does, so it
    # has to be able to reach the same remote tools, under the same governance.
    mcp_executor = McpToolExecutor(factory, settings=settings)
    service = AgentRunService(
        factory,
        credential_cipher=ProviderCredentialCipher.from_settings(settings),
        tool_runtime=ToolRuntime(
            session_factory=factory,
            registry=ToolRegistry(retriever=retriever),
            audit_sink=SqlAlchemyToolAuditSink(factory),
            trace_sink=trace_sink,
            mcp_handler=mcp_executor.execute_read,
        ),
        trace_sink=trace_sink,
        approval_service=approval_service,
        action_runtime=ActionRuntime(
            session_factory=factory, mcp_executor=McpActionExecutor(mcp_executor)
        ),
        checkpoint_adapter=LangGraphCheckpointAdapter(settings.database_url),
        # Evaluation cases use the same published memory policy as production.
        # Each AgentRun freezes its selected memory ids and hashes; no writer is
        # configured, so evaluation cannot train the memory store from its own output.
        memory_selector=SqlAlchemyMemoryStore(factory),
    )

    async def context_factory(run):
        async with factory() as session:
            principal = PrincipalContext(
                request_id=f"evaluation:{run.id}",
                trace_id=f"evaluation:{run.id}",
                user_id=str(run.created_by),
            )
            return (
                await TenantService().get_workspace_access(
                    session,
                    principal=principal,
                    workspace_id=run.workspace_id,
                )
            ).context

    return AgentRuntimeEvaluationDriver(
        service,
        context_factory,
        retriever=retriever,
        approval_context_factory=approval_actor_provider.context_for,
        judge=judge,
    )


@celery_app.task(
    bind=True,
    name="agenthub.reconcile_evaluation_runs",
    ignore_result=True,
    acks_late=True,
    reject_on_worker_lost=True,
)
def reconcile_evaluation_runs(_task) -> None:
    asyncio.run(_reconcile_evaluation_runs())


async def _reconcile_evaluation_runs() -> None:
    settings = get_settings()
    engine, factory = create_database(settings.database_url)
    try:
        await reconcile_experiment_runs(
            factory,
            queue=CeleryExperimentRunQueue(celery_app),
            settings=settings,
        )
    finally:
        await engine.dispose()


__all__ = ["execute_experiment_run", "reconcile_evaluation_runs"]
