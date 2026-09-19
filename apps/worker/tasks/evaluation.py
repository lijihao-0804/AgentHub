"""Celery tasks for durable evaluation execution and reconciliation."""

from __future__ import annotations

import asyncio
import secrets
from uuid import UUID

from apps.worker.celery_app import celery_app
from packages.agent_runtime.adapters.langgraph import LangGraphCheckpointAdapter
from packages.agent_runtime.runtime import AgentRunService
from packages.approvals import ApprovalService
from packages.control_plane.services import TenantService
from packages.core.config.settings import get_settings
from packages.core.database import create_database
from packages.core.execution_context.models import PrincipalContext
from packages.evaluation.queue import CeleryExperimentRunQueue
from packages.evaluation.runner import (
    AgentRuntimeEvaluationDriver,
    DeterministicEvaluationDriver,
    ExperimentRunner,
    reconcile_experiment_runs,
)
from packages.knowledge.composition import production_retrieval_components
from packages.knowledge.retrieval import SessionScopedKnowledgeRetriever
from packages.model_gateway.credentials import ProviderCredentialCipher
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
        driver = await _build_driver(settings, factory)
        await ExperimentRunner(factory, driver=driver).execute(
            run_id=run_id,
            owner=secrets.token_urlsafe(24),
            settings=settings,
        )
    finally:
        await engine.dispose()


async def _build_driver(settings, factory):
    if settings.testing:
        return DeterministicEvaluationDriver()
    trace_sink = ProductionTraceSink()
    components = production_retrieval_components(settings)
    retriever = SessionScopedKnowledgeRetriever(
        session_factory=factory,
        components=components,
        rrf_k=settings.knowledge_rrf_k,
        trace_sink=trace_sink,
    )
    approval_service = ApprovalService(factory, ttl_seconds=settings.approval_ttl_seconds)
    service = AgentRunService(
        factory,
        credential_cipher=ProviderCredentialCipher.from_settings(settings),
        tool_runtime=ToolRuntime(
            session_factory=factory,
            registry=ToolRegistry(retriever=retriever),
            audit_sink=SqlAlchemyToolAuditSink(factory),
            trace_sink=trace_sink,
        ),
        trace_sink=trace_sink,
        approval_service=approval_service,
        action_runtime=ActionRuntime(session_factory=factory),
        checkpoint_adapter=LangGraphCheckpointAdapter(settings.database_url),
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

    return AgentRuntimeEvaluationDriver(service, context_factory, retriever=retriever)


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
