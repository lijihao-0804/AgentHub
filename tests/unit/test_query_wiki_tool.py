"""W4 native query_wiki tool: fail-closed binding, envelope output, registration."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from packages.agent_runtime.context_budget import ContextCategory
from packages.agent_runtime.runtime import _categorize_messages
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.model_gateway.contracts import ModelMessage
from packages.tools.contracts import (
    ToolApprovalPolicy,
    ToolDefinition,
    ToolEffect,
    ToolExecutionContext,
    ToolRisk,
    ToolSourceKind,
)
from packages.tools.errors import ToolHandlerError
from packages.tools.registry import ToolRegistry
from packages.tools.runtime import PublishedToolResolver, ToolRuntime

pytestmark = pytest.mark.asyncio


def _query_wiki_module():
    """The tool module object; the package re-export shadows the submodule attribute."""

    import sys

    return sys.modules["packages.tools.builtins.query_wiki"]


_WS = str(UUID("00000000-0000-0000-0000-0000000000a1"))
_KB = "00000000-0000-0000-0000-0000000000b2"
_PUB = "00000000-0000-0000-0000-0000000000c3"
_SNAP = "00000000-0000-0000-0000-0000000000d4"
_HASH = "e" * 64


def _context(*, permissions=None) -> ToolExecutionContext:
    workspace = WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(request_id="wiki-tool", trace_id="wiki-tool"),
            organization_id=str(uuid4()),
            org_role="OWNER",
        ),
        workspace_id=_WS,
        workspace_role="DEVELOPER",
        permissions=frozenset(permissions if permissions is not None else {"tool_run"}),
    )
    return ToolExecutionContext(
        workspace_context=workspace,
        agent_version_id=uuid4(),
        tool_call_id="call-1",
    )


def _definition(**overrides) -> ToolDefinition:
    values = dict(
        identity="query_wiki",
        revision_id=uuid4(),
        spec_hash="spec-hash",
        description="Query the bound Wiki publication.",
        input_schema={"type": "object"},
        effect=ToolEffect.READ,
        risk_level=ToolRisk.LOW,
        approval_policy=ToolApprovalPolicy.NEVER,
        timeout_seconds=5,
        retrieval_config={"context_budget_bytes": 8_000},
        source_kind=ToolSourceKind.BUILTIN,
        wiki_binding={
            "knowledge_base_id": _KB,
            "binding_mode": "PINNED",
            "wiki_publication_id": _PUB,
            "publication_hash": _HASH,
        },
    )
    values.update(overrides)
    return ToolDefinition(**values)


def _compilation(*, status="PUBLISHED", content_hash=_HASH):
    return SimpleNamespace(
        id=UUID(_PUB),
        workspace_id=UUID(_WS),
        knowledge_base_id=UUID(_KB),
        status=status,
        content_hash=content_hash,
        source_snapshot_id=UUID(_SNAP),
    )


def _session_factory(scalar_results):
    @asynccontextmanager
    async def factory():
        async def scalar(_statement):
            return scalar_results.pop(0)

        yield SimpleNamespace(scalar=scalar)

    return factory


def _next(results):
    return results.pop(0)


def _wiki_claim(**overrides):
    values = {
        "origin": "WIKI_DERIVED",
        "page_key": "page-1",
        "topic_key": "retention",
        "quote": "Records are retained for seven years.",
        "chunk_id": "chunk-1",
        "document_id": str(uuid4()),
        "document_revision_id": str(uuid4()),
        "locator": {"page": 2},
        "predicate": "retention_period",
        "value": "7 years",
        "condition": None,
        "conflict": False,
        "conflict_peers": [],
        "conflict_origin": "BASIC_OR_MODEL",
        "source": "wiki_claim",
        "retrieval_score": None,
        "rerank_score": None,
        "publication_id": _PUB,
        "publication_hash": _HASH,
        "wiki_page_id": "page-1",
        "source_revisions": ["rev-1"],
    }
    values.update(overrides)
    return values


def _raw_evidence(**overrides):
    values = {
        "origin": "RAW",
        "quote": "Raw retrieval passage.",
        "chunk_id": "chunk-9",
        "document_id": str(uuid4()),
        "document_revision_id": str(uuid4()),
        "locator": {"page": 3},
        "predicate": None,
        "value": None,
        "condition": None,
        "conflict": False,
        "source": "raw_retrieval",
        "retrieval_score": 0.9,
        "rerank_score": 0.8,
    }
    values.update(overrides)
    return values


async def test_handler_requires_a_frozen_binding():
    from packages.tools.builtins.query_wiki import query_wiki

    with pytest.raises(ToolHandlerError) as error:
        await query_wiki(
            _context(),
            _definition(wiki_binding=None),
            {"query": "retention"},
            _session_factory([]),
        )
    assert error.value.code == "WIKI_BINDING_INVALID"


async def test_handler_rejects_malformed_binding():
    from packages.tools.builtins.query_wiki import query_wiki

    with pytest.raises(ToolHandlerError) as error:
        await query_wiki(
            _context(),
            _definition(
                wiki_binding={
                    "knowledge_base_id": "not-a-uuid",
                    "binding_mode": "PINNED",
                    "wiki_publication_id": _PUB,
                    "publication_hash": _HASH,
                }
            ),
            {"query": "retention"},
            _session_factory([]),
        )
    assert error.value.code == "WIKI_BINDING_INVALID"


@pytest.mark.parametrize(
    ("status", "content_hash"),
    [("READY", _HASH), ("PUBLISHED", "f" * 64)],
)
async def test_handler_fails_closed_on_binding_drift(status, content_hash):
    from packages.tools.builtins.query_wiki import query_wiki

    with pytest.raises(ToolHandlerError) as error:
        await query_wiki(
            _context(),
            _definition(),
            {"query": "retention"},
            _session_factory([_compilation(status=status, content_hash=content_hash)]),
        )
    assert error.value.code == "WIKI_BINDING_INVALID"


async def test_handler_maps_revocation_to_wiki_binding_invalid(monkeypatch):
    query_wiki_module = _query_wiki_module()

    async def revoked_query_wiki(*_args, **_kwargs):
        raise AgentHubError("WIKI_SOURCE_REVOKED", "A Wiki source has been permanently revoked.")

    monkeypatch.setattr(query_wiki_module, "_query_wiki", revoked_query_wiki)
    from packages.tools.builtins.query_wiki import query_wiki

    with pytest.raises(ToolHandlerError) as error:
        await query_wiki(
            _context(),
            _definition(),
            {"query": "retention"},
            _session_factory([_compilation()]),
        )
    assert error.value.code == "WIKI_BINDING_INVALID"


async def test_handler_projects_envelope_output(monkeypatch):
    captured = {}

    async def fake_query_wiki(session, context, kb_id, compilation_id, snapshot_id, text, **kwargs):
        captured.update(
            {
                "kb_id": kb_id,
                "compilation_id": compilation_id,
                "snapshot_id": snapshot_id,
                "text": text,
                "mode": kwargs.get("mode"),
                "intent": kwargs.get("intent"),
                "require_current": kwargs.get("require_current"),
                "permissions": frozenset(context.permissions),
                "retriever": kwargs.get("raw_retriever"),
            }
        )
        return {
            "route": "HYBRID",
            "insufficient_evidence": False,
            "supplemental_required": False,
            "conflicts": [],
            "evidence": [_wiki_claim(), _raw_evidence()],
        }

    query_wiki_module = _query_wiki_module()

    monkeypatch.setattr(query_wiki_module, "_query_wiki", fake_query_wiki)
    from packages.tools.builtins.query_wiki import query_wiki

    retriever = object()
    result = await query_wiki(
        _context(permissions={"tool_run", "agent_run"}),
        _definition(),
        {"query": "retention policy", "mode": "hybrid", "intent": "fact"},
        _session_factory([_compilation()]),
        retriever=retriever,
    )
    assert captured["kb_id"] == UUID(_KB)
    assert captured["compilation_id"] == UUID(_PUB)
    assert captured["snapshot_id"] == UUID(_SNAP)
    assert captured["mode"] == "HYBRID"
    assert captured["intent"] == "fact"
    assert captured["require_current"] is False
    assert captured["retriever"] is retriever
    # The wiki layer's permission guard is bridged for this internal call only.
    assert "knowledge_run" in captured["permissions"]
    assert captured["permissions"] <= {"tool_run", "agent_run", "knowledge_run"}

    assert result["route"] == "HYBRID"
    assert result["binding"] == {
        "knowledge_base_id": _KB,
        "wiki_publication_id": _PUB,
        "publication_hash": _HASH,
        "source_snapshot_id": _SNAP,
    }
    assert len(result["results"]) == len(result["evidence_refs"]) == 2
    derived, raw = result["results"]
    assert derived["origin"] == "WIKI_DERIVED"
    assert derived["publication_id"] == _PUB
    assert derived["publication_hash"] == _HASH
    assert derived["wiki_page_id"] == "page-1"
    assert derived["source_revisions"] == ["rev-1"]
    assert derived["snippet"].startswith("Records are retained")
    assert raw["origin"] == "RAW"
    assert "publication_hash" not in raw
    for reference in result["evidence_refs"]:
        assert set(reference) == {
            "snapshot_id",
            "knowledge_base_id",
            "document_revision_id",
            "chunk_id",
        }
        assert reference["snapshot_id"] == _SNAP


async def test_handler_rejects_incomplete_wiki_envelope(monkeypatch):
    async def fake_query_wiki(*_args, **_kwargs):
        return {
            "route": "WIKI",
            "evidence": [_wiki_claim(publication_hash=None)],
        }

    query_wiki_module = _query_wiki_module()

    monkeypatch.setattr(query_wiki_module, "_query_wiki", fake_query_wiki)
    from packages.tools.builtins.query_wiki import query_wiki

    with pytest.raises(ToolHandlerError) as error:
        await query_wiki(
            _context(),
            _definition(),
            {"query": "retention"},
            _session_factory([_compilation()]),
        )
    assert error.value.code == "WIKI_ENVELOPE_INVALID"


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"query": ""},
        {"query": "x" * 4001},
        {"query": "retention", "mode": "TELEPORT"},
        {"query": "retention", "intent": "gossip"},
    ],
)
async def test_handler_rejects_invalid_arguments(arguments):
    from packages.tools.builtins.query_wiki import query_wiki

    with pytest.raises(ToolHandlerError) as error:
        await query_wiki(_context(), _definition(), arguments, _session_factory([]))
    assert error.value.code == "WIKI_QUERY_INVALID_ARGUMENT"


async def test_tool_runtime_refuses_without_tool_run_permission():
    runtime = ToolRuntime(session_factory=_session_factory([]))
    with pytest.raises(AgentHubError) as error:
        await runtime.execute(
            context=_context(permissions={"workspace_read"}).workspace_context,
            agent_version_id=uuid4(),
            tool_identity="query_wiki",
            arguments={"query": "retention"},
            tool_call_id="call-1",
        )
    assert error.value.code == "FORBIDDEN"


async def test_registry_resolves_query_wiki():
    assert ToolRegistry().resolve("query_wiki") is not None


def test_resolver_wiki_binding_from_spec():
    binding = PublishedToolResolver._wiki_binding(
        {
            "wiki_binding": {
                "schema_version": 1,
                "binding_mode": "PINNED",
                "knowledge_base_id": _KB,
                "wiki_publication_id": _PUB,
                "publication_hash": _HASH,
            }
        },
        None,
    )
    assert binding == {
        "knowledge_base_id": _KB,
        "binding_mode": "PINNED",
        "wiki_publication_id": _PUB,
        "publication_hash": _HASH,
    }


def test_resolver_wiki_binding_detects_run_drift():
    with pytest.raises(AgentHubError) as error:
        PublishedToolResolver._wiki_binding(
            {
                "wiki_binding": {
                    "schema_version": 1,
                    "binding_mode": "PINNED",
                    "knowledge_base_id": _KB,
                    "wiki_publication_id": _PUB,
                    "publication_hash": _HASH,
                }
            },
            (
                {
                    "knowledge_base_id": _KB,
                    "binding_mode": "PINNED",
                    "wiki_publication_id": _PUB,
                    "publication_hash": "f" * 64,
                },
            ),
        )
    assert error.value.code == "TOOL_REVISION_INTEGRITY_ERROR"


def test_resolver_wiki_binding_absent():
    assert PublishedToolResolver._wiki_binding({}, None) is None
    with pytest.raises(AgentHubError):
        PublishedToolResolver._wiki_binding({}, ({},))


def test_query_wiki_messages_categorize_as_rag_evidence():
    message = ModelMessage("tool", json.dumps({"results": []}), "query_wiki", "call-1")
    categorized = _categorize_messages(
        [
            ModelMessage("system", "policy"),
            ModelMessage("system", "prompt"),
            message,
        ]
    )
    assert categorized[-1].category is ContextCategory.RAG_EVIDENCE


async def test_observation_registers_wiki_evidence_into_private_registry():
    from tests.unit.test_runtime_rag_evidence_projection import (
        _graph,
        _observation_state,
    )

    graph = _graph()
    definition = _definition()
    payload = {
        "binding": {"wiki_publication_id": _PUB, "publication_hash": _HASH},
        "route": "WIKI",
        "insufficient_evidence": False,
        "supplemental_required": False,
        "conflict_count": 0,
        "results": [
            {
                "origin": "WIKI_DERIVED",
                "chunk_id": "chunk-1",
                "document_id": "doc-1",
                "document_revision_id": "rev-1",
                "locator": {"page": 2},
                "quote": "Records are retained for seven years.",
                "snippet": "Records are retained for seven years.",
                "conflict": False,
                "publication_id": _PUB,
                "publication_hash": _HASH,
                "wiki_page_id": "page-1",
                "source_revisions": ["rev-1"],
            }
        ],
        "evidence_refs": [
            {
                "snapshot_id": _SNAP,
                "knowledge_base_id": _KB,
                "document_revision_id": "rev-1",
                "chunk_id": "chunk-1",
            }
        ],
    }
    state = _observation_state(graph, definition=definition, payload={"data": payload})
    result = await graph.observation(state)
    message = result["messages"][-1]
    assert message.name == "query_wiki"
    view = json.loads(message.content)
    item = view["results"][0]
    assert item["id"] == "R00001E1"
    assert item["origin"] == "WIKI_DERIVED"
    assert item["wiki_page_id"] == "page-1"
    assert "publication_hash" not in json.dumps(view)
    registry = result["rag_evidence_registry"]
    assert registry["R00001E1"]["record"]["publication_hash"] == _HASH
    assert registry["R00001E1"]["reference"]["snapshot_id"] == _SNAP


async def test_observation_fails_closed_on_incomplete_wiki_record():
    from tests.unit.test_runtime_rag_evidence_projection import (
        _graph,
        _observation_state,
    )

    graph = _graph()
    definition = _definition()
    payload = {
        "route": "WIKI",
        "results": [
            {
                "origin": "WIKI_DERIVED",
                "chunk_id": "chunk-1",
                "document_id": "doc-1",
                "document_revision_id": "rev-1",
                "locator": {"page": 2},
                "quote": "quote",
                "snippet": "quote",
                # publication_hash missing: registration must refuse.
                "wiki_page_id": "page-1",
                "source_revisions": ["rev-1"],
            }
        ],
        "evidence_refs": [
            {
                "snapshot_id": _SNAP,
                "knowledge_base_id": _KB,
                "document_revision_id": "rev-1",
                "chunk_id": "chunk-1",
            }
        ],
    }
    state = _observation_state(graph, definition=definition, payload={"data": payload})
    result = await graph.observation(state)
    view = json.loads(result["messages"][-1].content)
    assert view["evidence_projection"] == "FAILED"
    # The registry stays present but empty: nothing was registered.
    assert result["rag_evidence_registry"] == {}


async def test_observation_budget_clamps_wiki_evidence():
    from tests.unit.test_runtime_rag_evidence_projection import (
        _graph,
        _observation_state,
    )

    graph = _graph(
        runtime_context_budget={
            "reserved_output_tokens": 1,
            "max_retrieval_tokens": 300,
            "max_tool_result_tokens": 4_000,
        }
    )
    definition = _definition(retrieval_config={})
    payload = {
        "route": "WIKI",
        "results": [
            {
                "origin": "WIKI_DERIVED",
                "chunk_id": "chunk-1",
                "document_id": "doc-1",
                "document_revision_id": "rev-1",
                "locator": {"page": 2},
                "quote": "x" * 5_000,
                "snippet": "x" * 5_000,
                "conflict": False,
                "publication_id": _PUB,
                "publication_hash": _HASH,
                "wiki_page_id": "page-1",
                "source_revisions": ["rev-1"],
            }
        ],
        "evidence_refs": [
            {
                "snapshot_id": _SNAP,
                "knowledge_base_id": _KB,
                "document_revision_id": "rev-1",
                "chunk_id": "chunk-1",
            }
        ],
    }
    state = _observation_state(graph, definition=definition, payload={"data": payload})
    result = await graph.observation(state)
    view = json.loads(result["messages"][-1].content)
    assert view["results"] == []
    assert view["truncated"] is True
