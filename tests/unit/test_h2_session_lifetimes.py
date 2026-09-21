from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from importlib import import_module
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.knowledge.contracts import (
    RetrievalQuery,
    RetrievalResult,
    RetrievalTrace,
    RetrievalTraceStage,
    SparseEncoding,
    VectorSearchHit,
)
from packages.knowledge.retrieval import HybridKnowledgeRetriever
from packages.model_gateway.contracts import (
    ModelCapabilities,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelStreamEventType,
    ResolvedModelExecutionPlan,
    ResolvedModelExecutionProfile,
)
from packages.model_gateway.gateway import ModelGatewayService
from packages.model_gateway.models import ProviderCredential
from packages.tools.contracts import (
    ToolApprovalPolicy,
    ToolDefinition,
    ToolEffect,
    ToolExecutionContext,
    ToolResultStatus,
    ToolRisk,
)
from packages.tools.registry import ToolRegistry
from packages.tools.runtime import PublishedToolResolver, ToolRuntime

search_knowledge_module = import_module("packages.tools.builtins.search_knowledge")


class _SessionMarker:
    def __init__(self) -> None:
        self.active = False

    async def __aenter__(self) -> _SessionMarker:
        self.active = True
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.active = False


class _Repository:
    def __init__(self, credential: ProviderCredential) -> None:
        self.credential = credential

    async def get_provider_credential(self, _context, credential_id):
        return self.credential if credential_id == self.credential.id else None


class _Adapter:
    def __init__(self, marker: _SessionMarker) -> None:
        self.marker = marker

    async def complete(self, _profile, credential, _request) -> ModelResponse:
        assert self.marker.active is False
        return ModelResponse(
            content=credential.secret or "",
            provider=credential.provider,
            model="offline-fake",
        )

    async def stream(self, _profile, credential, _request) -> AsyncIterator[ModelStreamEvent]:
        assert self.marker.active is False
        yield ModelStreamEvent(
            event_type=ModelStreamEventType.MESSAGE_DELTA,
            message_delta=credential.secret or "",
        )
        yield ModelStreamEvent(
            event_type=ModelStreamEventType.COMPLETED,
            response=ModelResponse(
                content=credential.secret or "",
                provider=credential.provider,
                model="offline-fake",
            ),
        )


def _context() -> WorkspaceExecutionContext:
    workspace_id = uuid4()
    principal = PrincipalContext(request_id="h2-session", trace_id="h2-session")
    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=principal,
            organization_id=str(uuid4()),
            org_role="OWNER",
        ),
        workspace_id=str(workspace_id),
        workspace_role="DEVELOPER",
    )


def _plan(context: WorkspaceExecutionContext, credential: ProviderCredential):
    profile = ResolvedModelExecutionProfile(
        id=uuid4(),
        workspace_id=UUID(context.workspace_id),
        provider_credential_id=credential.id,
        provider="fake",
        model="offline-fake",
        temperature=Decimal("0"),
        max_tokens=64,
        timeout_seconds=Decimal("5"),
        capabilities=ModelCapabilities(streaming=True),
    )
    return ResolvedModelExecutionPlan(primary=profile)


def _request() -> ModelRequest:
    return ModelRequest(messages=(ModelMessage(role="user", content="hello"),))


@pytest.mark.asyncio
async def test_prepared_model_completion_runs_after_resolution_session_exits() -> None:
    context = _context()
    credential = ProviderCredential(
        id=uuid4(),
        workspace_id=UUID(context.workspace_id),
        provider="fake",
        name="test",
        secret="sentinel",
    )
    marker = _SessionMarker()
    service = ModelGatewayService(_Repository(credential), _Adapter(marker))
    plan = _plan(context, credential)

    async with marker:
        prepared = await service.prepare_resolved(context, plan, _request(), operation="generate")
    response = await prepared.generate_resolved(context, plan, _request())

    assert response.content == "sentinel"


@pytest.mark.asyncio
async def test_prepared_model_stream_runs_after_resolution_session_exits() -> None:
    context = _context()
    credential = ProviderCredential(
        id=uuid4(),
        workspace_id=UUID(context.workspace_id),
        provider="fake",
        name="test",
        secret="sentinel",
    )
    marker = _SessionMarker()
    service = ModelGatewayService(_Repository(credential), _Adapter(marker))
    plan = _plan(context, credential)

    async with marker:
        prepared = await service.prepare_resolved(context, plan, _request(), operation="stream")
    events = [event async for event in prepared.stream_resolved(context, plan, _request())]

    assert events[-1].event_type is ModelStreamEventType.COMPLETED


class _Retriever:
    def __init__(self, marker: _SessionMarker) -> None:
        self.marker = marker

    async def retrieve_with_trace(self, _context, _query) -> RetrievalResult:
        assert self.marker.active is False
        empty = RetrievalTraceStage(latency_ms=0, results=())
        return RetrievalResult(
            evidence=(),
            trace=RetrievalTrace(
                snapshot_id="snapshot",
                dense=empty,
                sparse=empty,
                fusion=empty,
                rerank=empty,
                total_latency_ms=0,
            ),
        )


@pytest.mark.asyncio
async def test_search_knowledge_closes_snapshot_session_before_retrieval(monkeypatch) -> None:
    marker = _SessionMarker()

    class _Factory:
        def __call__(self):
            return marker

    async def fake_snapshot_queries(*_args, **_kwargs):
        return [
            RetrievalQuery(
                text="hello",
                knowledge_base_id=str(uuid4()),
                knowledge_snapshot_id=str(uuid4()),
                dense_top_k=1,
                sparse_top_k=1,
                candidate_top_k=1,
                final_top_k=1,
            )
        ]

    monkeypatch.setattr(search_knowledge_module, "_snapshot_queries", fake_snapshot_queries)
    definition = ToolDefinition(
        identity="search_knowledge",
        revision_id=uuid4(),
        spec_hash="x",
        description="search",
        input_schema={},
        effect=ToolEffect.READ,
        risk_level=ToolRisk.LOW,
        approval_policy=ToolApprovalPolicy.NEVER,
        timeout_seconds=5,
        snapshot_refs=({"snapshot_id": str(uuid4()), "snapshot_hash": "hash"},),
        retrieval_config={"knowledge_binding_mode": "PINNED"},
    )
    context = _context()
    execution_context = ToolExecutionContext(
        workspace_context=context,
        agent_version_id=uuid4(),
        tool_call_id="call",
    )

    result = await search_knowledge_module.search_knowledge(
        execution_context,
        definition,
        {"query": "hello", "limit": 1},
        _Factory(),
        retriever=_Retriever(marker),
    )

    assert result["results"] == []


@pytest.mark.asyncio
async def test_search_external_stages_do_not_see_tool_runtime_session(monkeypatch) -> None:
    marker = _SessionMarker()

    class _Factory:
        def __call__(self):
            return marker

    class _ExternalComponents:
        dimension = 2

        def _assert_closed(self) -> None:
            assert marker.active is False

        def embed_query(self, _text):
            self._assert_closed()
            return (1.0, 0.0)

        def encode_query(self, _text) -> SparseEncoding:
            self._assert_closed()
            return SparseEncoding(indices=(1,), values=(1.0,))

        def dense_search(self, _query, *, scope, limit):
            del scope, limit
            self._assert_closed()
            return (VectorSearchHit("point-1", 0.9, {"chunk_id": "chunk-1"}),)

        def sparse_search(self, _query, *, scope, limit):
            del scope, limit
            self._assert_closed()
            return (VectorSearchHit("point-1", 0.8, {"chunk_id": "chunk-1"}),)

        def rerank(self, _query, _candidates):
            self._assert_closed()
            return (1.0,)

    components = _ExternalComponents()
    retriever = HybridKnowledgeRetriever(
        session_factory=_Factory(),
        dense_embedder=components,
        sparse_encoder=components,
        reranker=components,
        vector_index=components,
    )

    async def fake_scope(_session, _context, _query):
        return UUID(_context.workspace_id), uuid4(), (str(uuid4()),)

    async def fake_load_chunks(_session, **_kwargs):
        return {
            "chunk-1": (
                SimpleNamespace(
                    chunk_id="chunk-1",
                    text="evidence",
                    locator={},
                    ordinal=0,
                    normalized_content_hash="hash",
                ),
                SimpleNamespace(
                    id=uuid4(),
                    name="document.md",
                    effective_date=None,
                    superseded_by_document_id=None,
                ),
                SimpleNamespace(id=uuid4()),
            )
        }

    monkeypatch.setattr(retriever, "_snapshot_scope", fake_scope)
    monkeypatch.setattr(retriever, "_load_chunks", fake_load_chunks)
    async def fake_snapshot_queries(*_args, **_kwargs):
        return [
            RetrievalQuery(
                text="hello",
                knowledge_base_id=str(uuid4()),
                knowledge_snapshot_id=str(uuid4()),
                dense_top_k=1,
                sparse_top_k=1,
                candidate_top_k=1,
                final_top_k=1,
            )
        ]

    monkeypatch.setattr(search_knowledge_module, "_snapshot_queries", fake_snapshot_queries)
    definition = ToolDefinition(
        identity="search_knowledge",
        revision_id=uuid4(),
        spec_hash="x",
        description="search",
        input_schema={},
        effect=ToolEffect.READ,
        risk_level=ToolRisk.LOW,
        approval_policy=ToolApprovalPolicy.NEVER,
        timeout_seconds=5,
        snapshot_refs=({"snapshot_id": str(uuid4()), "snapshot_hash": "hash"},),
        retrieval_config={"knowledge_binding_mode": "PINNED"},
    )
    execution_context = ToolExecutionContext(
        workspace_context=_context(),
        agent_version_id=uuid4(),
        tool_call_id="call",
    )

    result = await search_knowledge_module.search_knowledge(
        execution_context,
        definition,
        {"query": "hello", "limit": 1},
        _Factory(),
        retriever=retriever,
    )

    assert result["results"][0]["chunk_id"] == "chunk-1"


@pytest.mark.asyncio
async def test_tool_runtime_closes_resolution_session_before_handler(monkeypatch) -> None:
    marker = _SessionMarker()

    class _Factory:
        def __call__(self):
            return marker

    definition = ToolDefinition(
        identity="calculator",
        revision_id=uuid4(),
        spec_hash="x",
        description="calculator",
        input_schema={"type": "object", "properties": {"expression": {"type": "string"}}},
        effect=ToolEffect.READ,
        risk_level=ToolRisk.LOW,
        approval_policy=ToolApprovalPolicy.NEVER,
        timeout_seconds=5,
    )

    async def resolve(_resolver, **_kwargs):
        return definition

    async def handler(_context, _definition, _arguments, session_factory):
        assert session_factory is not None
        assert marker.active is False
        return {"ok": True}

    monkeypatch.setattr(PublishedToolResolver, "resolve", resolve)
    context = _context().model_copy(update={"permissions": frozenset({"tool_run"})})
    runtime = ToolRuntime(
        session_factory=_Factory(),
        registry=ToolRegistry(handler_overrides={"calculator": handler}),
    )

    result = await runtime.execute(
        context=context,
        agent_version_id=uuid4(),
        tool_identity="calculator",
        arguments={"expression": "1"},
        tool_call_id="call",
    )

    assert result.status is ToolResultStatus.SUCCESS
