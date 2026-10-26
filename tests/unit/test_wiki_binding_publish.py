"""W2 WikiBinding: publish-time resolution, canonical hash coverage, frozen parsing."""

import sys
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from packages.agent_runtime import publish as publish_module
from packages.agent_runtime.frozen import parse_frozen_agent_spec
from packages.agent_runtime.publish import (
    AgentPublishService,
    _resolved_spec,
    _validate_retrieval_config,
)
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.tools.builtins.search_knowledge import search_knowledge
from packages.tools.contracts import (
    ToolApprovalPolicy,
    ToolDefinition,
    ToolEffect,
    ToolRisk,
)
from packages.tools.errors import ToolHandlerError
from tests.unit.test_agent_publish import _agent as _published_agent
from tests.unit.test_agent_publish import _profile
from tests.unit.test_query_wiki_tool import _context as _tool_context
from tests.unit.test_query_wiki_tool import _definition as _tool_definition

_KB_ID = UUID("00000000-0000-0000-0000-0000000000a1")
_PUBLICATION_ID = UUID("00000000-0000-0000-0000-0000000000b2")
_PUBLICATION_HASH = "c" * 64


def _publish_context() -> WorkspaceExecutionContext:
    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(request_id="wiki-binding", trace_id="wiki-binding"),
            organization_id=str(uuid4()),
            org_role="OWNER",
        ),
        workspace_id=str(uuid4()),
        workspace_role="DEVELOPER",
        permissions=frozenset({"agent_edit", "knowledge_run"}),
    )


def _agent() -> SimpleNamespace:
    return SimpleNamespace(workspace_id=uuid4(), id=uuid4())


def _base_spec_kwargs():
    profile = _profile(profile_id=uuid4(), workspace_id=uuid4())
    return {
        "agent": _published_agent(),
        "model": ((profile,), {profile.id: "deepseek"}),
        "bindings": (),
        "tools": (),
    }


def _binding(mode="PINNED"):
    return SimpleNamespace(
        workspace_id=None,
        agent_id=None,
        knowledge_base_id=_KB_ID,
        binding_mode=mode,
        wiki_publication_id=_PUBLICATION_ID if mode == "PINNED" else None,
        publication_hash=_PUBLICATION_HASH if mode == "PINNED" else None,
    )


def _compilation(status="PUBLISHED", content_hash=_PUBLICATION_HASH):
    return SimpleNamespace(
        id=_PUBLICATION_ID,
        workspace_id=uuid4(),
        knowledge_base_id=_KB_ID,
        status=status,
        content_hash=content_hash,
        published_at=datetime(2026, 10, 26, tzinfo=UTC),
        source_snapshot_id=uuid4(),
        source_snapshot_hash="d" * 64,
    )


class _FakeSession:
    def __init__(self, scalar_results=(), scalars_results=()):
        self._scalar_results = list(scalar_results)
        self._scalars_results = list(scalars_results)

    async def scalar(self, statement):
        return self._scalar_results.pop(0)

    async def scalars(self, statement):
        return self._scalars_results.pop(0) if self._scalars_results else []


class _StubWikiService:
    def __init__(self, *, revoked=False):
        self.revoked = revoked
        self.calls = []

    async def sources(self, session, context, row, require_current=False):
        self.calls.append((row, require_current))
        if self.revoked:
            raise AgentHubError(
                "WIKI_SOURCE_REVOKED", "A Wiki source has been permanently revoked."
            )
        return []


@pytest.fixture
def stub_wiki(monkeypatch):
    stub = _StubWikiService()
    monkeypatch.setattr(publish_module, "WikiService", lambda: stub)
    return stub


def test_resolved_spec_omits_wiki_binding_when_unbound():
    spec = _resolved_spec(**_base_spec_kwargs())
    assert "wiki_binding" not in spec


def test_hash_covers_wiki_binding_same_spec_different_binding_different_hash():
    base = _base_spec_kwargs()
    unbound = _resolved_spec(**base)
    first = _resolved_spec(
        **base,
        wiki_binding={
            "schema_version": 1,
            "binding_mode": "PINNED",
            "knowledge_base_id": str(_KB_ID),
            "wiki_publication_id": str(_PUBLICATION_ID),
            "publication_hash": _PUBLICATION_HASH,
        },
    )
    second = _resolved_spec(
        **base,
        wiki_binding={
            "schema_version": 1,
            "binding_mode": "PINNED",
            "knowledge_base_id": str(_KB_ID),
            "wiki_publication_id": str(uuid4()),
            "publication_hash": "e" * 64,
        },
    )
    assert "wiki_binding" not in unbound
    assert canonical_json_hash(unbound) != canonical_json_hash(first)
    assert canonical_json_hash(first) != canonical_json_hash(second)


def _spec_with_wiki_binding() -> dict:
    return _resolved_spec(
        **_base_spec_kwargs(),
        wiki_binding={
            "schema_version": 1,
            "binding_mode": "PINNED",
            "knowledge_base_id": str(_KB_ID),
            "wiki_publication_id": str(_PUBLICATION_ID),
            "publication_hash": _PUBLICATION_HASH,
        },
    )


def test_frozen_spec_round_trips_wiki_binding():
    frozen = parse_frozen_agent_spec(_spec_with_wiki_binding(), workspace_id=uuid4())
    assert frozen.wiki_binding is not None
    assert frozen.wiki_binding.knowledge_base_id == _KB_ID
    assert frozen.wiki_binding.wiki_publication_id == _PUBLICATION_ID
    assert frozen.wiki_binding.publication_hash == _PUBLICATION_HASH
    assert frozen.wiki_binding.binding_mode == "PINNED"


def test_frozen_spec_rejects_wiki_binding_without_publication_hash():
    spec = _spec_with_wiki_binding()
    spec["wiki_binding"] = {
        "schema_version": 1,
        "binding_mode": "LATEST",
        "knowledge_base_id": str(_KB_ID),
        "wiki_publication_id": str(_PUBLICATION_ID),
    }
    with pytest.raises(AgentHubError) as error:
        parse_frozen_agent_spec(spec, workspace_id=uuid4())
    assert error.value.code == "AGENT_VERSION_MODEL_BINDING_INVALID"


@pytest.mark.asyncio
async def test_resolve_wiki_binding_returns_none_without_draft(stub_wiki):
    service = AgentPublishService()
    session = _FakeSession(scalar_results=[None])
    assert await service._resolve_wiki_binding(session, _publish_context(), _agent()) is None


@pytest.mark.asyncio
async def test_resolve_wiki_binding_pinned_freezes_publication_identity(stub_wiki):
    service = AgentPublishService()
    agent = _agent()
    session = _FakeSession(
        scalar_results=[_binding(), _compilation()],
    )
    resolved = await service._resolve_wiki_binding(session, _publish_context(), agent)
    assert resolved == {
        "schema_version": 1,
        "binding_mode": "PINNED",
        "knowledge_base_id": str(_KB_ID),
        "wiki_publication_id": str(_PUBLICATION_ID),
        "publication_hash": _PUBLICATION_HASH,
    }
    assert stub_wiki.calls and stub_wiki.calls[0][1] is False


@pytest.mark.asyncio
async def test_resolve_wiki_binding_rejects_hash_mismatch(stub_wiki):
    service = AgentPublishService()
    session = _FakeSession(
        scalar_results=[_binding(), _compilation(content_hash="f" * 64)],
    )
    with pytest.raises(AgentHubError) as error:
        await service._resolve_wiki_binding(session, _publish_context(), _agent())
    assert error.value.code == "WIKI_BINDING_INVALID"


@pytest.mark.asyncio
async def test_resolve_wiki_binding_rejects_unpublished_compilation(stub_wiki):
    service = AgentPublishService()
    session = _FakeSession(
        scalar_results=[_binding(), _compilation(status="READY")],
    )
    with pytest.raises(AgentHubError) as error:
        await service._resolve_wiki_binding(session, _publish_context(), _agent())
    assert error.value.code == "WIKI_BINDING_INVALID"


@pytest.mark.asyncio
async def test_resolve_wiki_binding_rejects_revoked_publication(stub_wiki):
    service = AgentPublishService()
    stub_wiki.revoked = True
    session = _FakeSession(
        scalar_results=[_binding(), _compilation()],
    )
    with pytest.raises(AgentHubError) as error:
        await service._resolve_wiki_binding(session, _publish_context(), _agent())
    assert error.value.code == "WIKI_SOURCE_REVOKED"


@pytest.mark.asyncio
async def test_resolve_wiki_binding_latest_resolves_and_skips_revoked(stub_wiki):
    service = AgentPublishService()
    revoked = _compilation()
    live = _compilation(content_hash="1" * 64)
    live.id = uuid4()
    calls = {"count": 0}

    async def sources(session, context, row, require_current=False):
        calls["count"] += 1
        if row is revoked:
            raise AgentHubError(
                "WIKI_SOURCE_REVOKED", "A Wiki source has been permanently revoked."
            )
        return []

    stub_wiki.sources = sources
    session = _FakeSession(
        scalar_results=[_binding(mode="LATEST")],
        scalars_results=[[revoked, live]],
    )
    resolved = await service._resolve_wiki_binding(session, _publish_context(), _agent())
    assert resolved["binding_mode"] == "LATEST"
    assert resolved["wiki_publication_id"] == str(live.id)
    assert resolved["publication_hash"] == "1" * 64
    # The winner is re-checked once more after selection (defense in depth).
    assert calls["count"] == 3


@pytest.mark.asyncio
async def test_resolve_wiki_binding_latest_fails_without_publishable(stub_wiki):
    service = AgentPublishService()
    stub_wiki.revoked = True
    session = _FakeSession(
        scalar_results=[_binding(mode="LATEST")],
        scalars_results=[[_compilation()]],
    )
    with pytest.raises(AgentHubError) as error:
        await service._resolve_wiki_binding(session, _publish_context(), _agent())
    assert error.value.code == "WIKI_BINDING_UNRESOLVED"


@pytest.mark.asyncio
async def test_resolve_wiki_binding_latest_keyset_scans_past_revoked_first_ten(stub_wiki):
    service = AgentPublishService()
    revoked = [_compilation() for _ in range(10)]
    for revision, publication in enumerate(revoked):
        publication.id = uuid4()
        publication.published_at = datetime(2026, 10, 26, 12, 0, revision, tzinfo=UTC)
    live = _compilation(content_hash="1" * 64)
    live.id = uuid4()
    live.published_at = datetime(2026, 10, 26, 11, 59, tzinfo=UTC)
    revoked_ids = {publication.id for publication in revoked}
    batch_sizes = []

    async def sources(_session, _context, row, require_current=False):
        assert require_current is False
        if row.id in revoked_ids:
            raise AgentHubError(
                "WIKI_SOURCE_REVOKED", "A Wiki source has been permanently revoked."
            )
        return []

    async def scalars(statement):
        batch_sizes.append(statement._limit)
        return revoked if len(batch_sizes) == 1 else [live]

    stub_wiki.sources = sources
    session = _FakeSession(scalar_results=[_binding(mode="LATEST")])
    session.scalars = scalars

    resolved = await service._resolve_wiki_binding(session, _publish_context(), _agent())

    assert resolved["binding_mode"] == "LATEST"
    assert resolved["wiki_publication_id"] == str(live.id)
    assert batch_sizes == [10, 10]


@pytest.mark.asyncio
async def test_publish_rejects_query_wiki_without_a_wiki_binding(
    stub_wiki,
) -> None:
    service = AgentPublishService()
    agent = _agent()
    query_wiki_spec = {
        "kind": "builtin",
        "identity": "query_wiki",
        "description": "Query the bound Wiki.",
        "input_schema": {"type": "object"},
        "effect": "READ",
        "risk_level": "LOW",
        "approval_policy": "NEVER",
        "timeout_seconds": 30,
    }
    tool = SimpleNamespace(id=uuid4(), enabled=True)
    revision = SimpleNamespace(
        id=uuid4(),
        spec=query_wiki_spec,
        spec_hash=canonical_json_hash(query_wiki_spec),
    )
    binding = SimpleNamespace(tool_id=tool.id, tool_revision_id=revision.id)
    session = _FakeSession(scalar_results=[tool, revision])
    session.scalars = lambda statement: _as_awaitable([binding])

    with pytest.raises(AgentHubError) as error:
        await service._resolve_tools(
            session,
            _publish_context(),
            agent,
            wiki_bound=False,
        )
    assert error.value.code == "WIKI_BINDING_REQUIRED"


async def _as_awaitable(value):
    return value


@pytest.mark.asyncio
async def test_query_wiki_rejects_frozen_model_drift_before_db_or_inference():
    from packages.tools.builtins.query_wiki import query_wiki

    class _CurrentComponents:
        dense = SimpleNamespace(model_name="models/current-embedding")
        sparse = SimpleNamespace(model_name="models/current-embedding")
        reranker = SimpleNamespace(model_name="models/current-reranker")

    class _CurrentRetriever:
        components = _CurrentComponents()

        async def retrieve(self, *_args, **_kwargs):
            raise AssertionError("drifted retriever must not run")

    accesses = 0

    def session_factory():
        nonlocal accesses
        accesses += 1
        raise AssertionError("drifted query must fail before opening a DB session")

    with pytest.raises(ToolHandlerError) as error:
        await query_wiki(
            _tool_context(),
            _tool_definition(
                retrieval_config={
                    "embedding_model": "models/frozen-embedding",
                    "reranker_model": "models/frozen-reranker",
                }
            ),
            {"query": "retention"},
            session_factory,
            retriever=_CurrentRetriever(),
        )
    assert error.value.code == "KNOWLEDGE_MODEL_IDENTITY_DRIFT"
    assert accesses == 0


@pytest.mark.asyncio
async def test_search_knowledge_allows_matching_identity(monkeypatch):
    class _CurrentComponents:
        dense = SimpleNamespace(model_name="models/current-embedding")
        sparse = SimpleNamespace(model_name="models/current-embedding")
        reranker = SimpleNamespace(model_name="models/current-reranker")

    class _CurrentRetriever:
        components = _CurrentComponents()

    search_definition = ToolDefinition(
        identity="search_knowledge",
        revision_id=uuid4(),
        spec_hash="spec-hash",
        description="search",
        input_schema={},
        effect=ToolEffect.READ,
        risk_level=ToolRisk.LOW,
        approval_policy=ToolApprovalPolicy.NEVER,
        timeout_seconds=5,
        retrieval_config={
            "embedding_model": "models/current-embedding",
            "reranker_model": "models/current-reranker",
        },
    )

    @asynccontextmanager
    async def session_factory():
        yield None

    async def snapshot_queries(*_args, **_kwargs):
        return []

    search_module = sys.modules["packages.tools.builtins.search_knowledge"]
    monkeypatch.setattr(search_module, "_snapshot_queries", snapshot_queries)
    result = await search_knowledge(
        _tool_context(),
        search_definition,
        {"query": "retention"},
        session_factory,
        retriever=_CurrentRetriever(),
    )
    assert result["results"] == []


def test_explicit_retrieval_models_must_match_the_deployed_components() -> None:
    settings = SimpleNamespace(
        knowledge_embedding_model="models/deployed-embedding",
        knowledge_reranker_model="models/deployed-reranker",
    )
    for key in ("embedding_model", "reranker_model"):
        with pytest.raises(AgentHubError) as error:
            _validate_retrieval_config({key: "models/other"}, settings=settings)
        assert error.value.code == "INVALID_AGENT_CONFIG"

    frozen = _validate_retrieval_config({}, settings=settings)
    assert frozen["embedding_model"] == "models/deployed-embedding"
    assert frozen["reranker_model"] == "models/deployed-reranker"
