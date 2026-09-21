"""Execute an agent run in the worker instead of inside the API request.

Until this task existed, ``apps/worker/tasks/`` held only reconciliation:
knowledge, approvals and evaluation all had a worker, but a *run* was executed
by the HTTP request that asked for it. That made the request the unit of
execution, which is why a client disconnect had to abort the run.

Executing here inverts it. The API prepares the run row and dispatches; the
worker executes and writes the event stream to ``agent_run_events``; the API
follows that log and serves SSE from it. No connection owns the run, so no
disconnect can end it, and any API process can serve any run's stream because
the log -- not a socket, and not a message broker -- is the delivery path.

Two deliberate decisions:

* **A queue message is not an authorization.** The message carries identifiers
  only. Permissions are re-derived here from the database through the same
  ``TenantService.get_workspace_access`` the API uses, so a forged or stale
  message cannot widen what the run may do -- and a membership revoked between
  dispatch and execution is honoured, not ignored.
* **The run is opened detached.** In the API a hub with no subscribers means
  everyone has left and the run should be aborted; here it is the normal state,
  because nobody is watching the worker.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

from sqlalchemy import select

from apps.worker.celery_app import celery_app
from packages.agent_runtime.adapters.langgraph import LangGraphCheckpointAdapter
from packages.agent_runtime.models import AgentRun
from packages.agent_runtime.runtime import AgentRunService
from packages.approvals import ApprovalService
from packages.artifacts.recorder import ToolResultArtifactRecorder
from packages.control_plane.services import TenantService
from packages.core.config.settings import Settings, get_settings
from packages.core.database import create_database
from packages.core.execution_context.models import PrincipalContext
from packages.knowledge.composition import production_retrieval_components
from packages.knowledge.retrieval import SessionScopedKnowledgeRetriever
from packages.mcp.runtime import McpActionExecutor, McpToolExecutor
from packages.model_gateway.credentials import ProviderCredentialCipher
from packages.observability import ProductionTraceSink
from packages.threads.context import SqlAlchemyThreadContextProvider
from packages.tools.actions import ActionRuntime
from packages.tools.audit import SqlAlchemyToolAuditSink
from packages.tools.registry import ToolRegistry
from packages.tools.runtime import ToolRuntime


@celery_app.task(
    bind=True,
    name="agenthub.execute_agent_run",
    ignore_result=True,
    acks_late=True,
    reject_on_worker_lost=True,
)
def execute_agent_run(_task, workspace_id: str, run_id: str, request_id: str) -> None:
    try:
        parsed_workspace_id = UUID(workspace_id)
        parsed_run_id = UUID(run_id)
    except ValueError:
        return
    asyncio.run(
        _execute_agent_run(
            get_settings(),
            workspace_id=parsed_workspace_id,
            run_id=parsed_run_id,
            request_id=request_id,
        )
    )


async def _execute_agent_run(
    settings: Settings,
    *,
    workspace_id: UUID,
    run_id: UUID,
    request_id: str,
) -> None:
    engine, factory = create_database(settings.database_url)
    try:
        async with factory() as session:
            run = await session.scalar(
                select(AgentRun).where(
                    AgentRun.workspace_id == workspace_id, AgentRun.id == run_id
                )
            )
            if run is None or run.status != "RUNNING":
                # Already executed, cancelled, or reconciled away. Re-running
                # would duplicate side effects that the run may already have
                # committed, so the safe response to an ambiguous redelivery is
                # to do nothing.
                return
            agent_version_id = run.agent_version_id
            input_text = run.input_text
            principal = PrincipalContext(
                request_id=request_id,
                trace_id=request_id,
                user_id=str(run.created_by),
            )
            context = (
                await TenantService().get_workspace_access(
                    session, principal=principal, workspace_id=workspace_id
                )
            ).context
            # Detach with its attributes already loaded: the run object outlives
            # this session, and a lazy refresh on a closed session would fail.
            session.expunge(run)

        service = _build_service(settings, factory)
        hub = await service.open_stream(
            context,
            agent_version_id=agent_version_id,
            input_text=input_text,
            prepared_run=run,
            detached=True,
        )
        await hub.wait_closed()
    finally:
        await engine.dispose()


def _build_service(settings: Settings, factory) -> AgentRunService:
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
    # The worker runs the same published agent the API would, so it must reach
    # the same remote tools under the same governance.
    mcp_executor = McpToolExecutor(factory, settings=settings)
    return AgentRunService(
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
        approval_service=ApprovalService(factory, ttl_seconds=settings.approval_ttl_seconds),
        action_runtime=ActionRuntime(
            session_factory=factory, mcp_executor=McpActionExecutor(mcp_executor)
        ),
        checkpoint_adapter=LangGraphCheckpointAdapter(settings.database_url),
        # Composed exactly as the API composes it. A run must not behave
        # differently -- lose its thread history, stop recording artifacts --
        # merely because of which process picked it up.
        thread_context_provider=SqlAlchemyThreadContextProvider(factory),
        artifact_recorder=ToolResultArtifactRecorder(factory),
    )


__all__ = ["execute_agent_run"]
