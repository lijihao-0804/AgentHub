"""Production composition for AgentRun execution dependencies."""

from __future__ import annotations

from fastapi import FastAPI, Request

from packages.agent_runtime.adapters.langgraph import LangGraphCheckpointAdapter
from packages.agent_runtime.runtime import AgentRunService
from packages.approvals import ApprovalService
from packages.artifacts.recorder import ToolResultArtifactRecorder
from packages.core.errors.exceptions import AgentHubError
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


def get_production_agent_run_service(request: Request) -> AgentRunService:
    app: FastAPI = request.app
    factory = getattr(app.state, "db_session_factory", None)
    if factory is None:
        raise AgentHubError("DATABASE_NOT_CONFIGURED", "Database access is not configured.", 503)
    service = getattr(app.state, "agent_run_service", None)
    if service is None:
        settings = app.state.settings
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
        # One executor serves both sides of the approval boundary: the same
        # connection lookup and the same single call, read one way for a READ
        # and another for a WRITE.
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
            approval_service=ApprovalService(factory, ttl_seconds=settings.approval_ttl_seconds),
            action_runtime=ActionRuntime(
                session_factory=factory, mcp_executor=McpActionExecutor(mcp_executor)
            ),
            checkpoint_adapter=LangGraphCheckpointAdapter(settings.database_url),
            # The work layer's two adapters. Without them the runtime behaves
            # exactly as it did before threads existed, which is what keeps a
            # Playground run unchanged.
            thread_context_provider=SqlAlchemyThreadContextProvider(factory),
            artifact_recorder=ToolResultArtifactRecorder(factory),
        )
        app.state.agent_run_service = service
    return service


__all__ = ["get_production_agent_run_service"]
