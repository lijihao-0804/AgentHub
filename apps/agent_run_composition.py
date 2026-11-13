"""Production dependencies shared by the API and the agent-run worker."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.adapters.langgraph import LangGraphCheckpointAdapter
from packages.agent_runtime.runtime import AgentRunService
from packages.approvals import ApprovalService
from packages.artifacts.recorder import ToolResultArtifactRecorder
from packages.core.config.settings import Settings
from packages.knowledge.composition import (
    onyx_section_selector_from_settings,
    production_retrieval_components,
)
from packages.knowledge.retrieval import SessionScopedKnowledgeRetriever
from packages.mcp.runtime import McpActionExecutor, McpToolExecutor
from packages.memory.contracts import MemoryWriteQueue
from packages.memory.store import SqlAlchemyMemoryStore
from packages.model_gateway.credentials import ProviderCredentialCipher
from packages.observability import ProductionTraceSink
from packages.threads.context import SqlAlchemyThreadContextProvider
from packages.tools.actions import ActionRuntime
from packages.tools.audit import SqlAlchemyToolAuditSink
from packages.tools.registry import ToolRegistry
from packages.tools.runtime import ToolRuntime


def build_agent_run_service(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    memory_writer: MemoryWriteQueue,
) -> AgentRunService:
    trace_sink = ProductionTraceSink()
    retriever = SessionScopedKnowledgeRetriever(
        session_factory=session_factory,
        components=production_retrieval_components(settings),
        rrf_k=settings.knowledge_rrf_k,
        min_rerank_score=settings.knowledge_min_rerank_score,
        superseded_rank_penalty=settings.knowledge_superseded_rank_penalty,
        trace_sink=trace_sink,
    )
    # Governed reads and approved writes resolve the same MCP connection.
    mcp_executor = McpToolExecutor(session_factory, settings=settings)
    return AgentRunService(
        session_factory,
        credential_cipher=ProviderCredentialCipher.from_settings(settings),
        tool_runtime=ToolRuntime(
            session_factory=session_factory,
            registry=ToolRegistry(
                retriever=retriever,
                section_selector=onyx_section_selector_from_settings(settings),
            ),
            audit_sink=SqlAlchemyToolAuditSink(session_factory),
            trace_sink=trace_sink,
            mcp_handler=mcp_executor.execute_read,
        ),
        trace_sink=trace_sink,
        approval_service=ApprovalService(
            session_factory, ttl_seconds=settings.approval_ttl_seconds
        ),
        action_runtime=ActionRuntime(
            session_factory=session_factory, mcp_executor=McpActionExecutor(mcp_executor)
        ),
        checkpoint_adapter=LangGraphCheckpointAdapter(settings.database_url),
        thread_context_provider=SqlAlchemyThreadContextProvider(session_factory),
        memory_selector=SqlAlchemyMemoryStore(session_factory),
        memory_writer=memory_writer,
        artifact_recorder=ToolResultArtifactRecorder(session_factory),
        stream_grace_seconds=settings.run_stream_grace_seconds,
    )
