"""W2 WikiBinding: publish-time resolution, canonical hash coverage, frozen parsing."""

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from packages.agent_runtime import publish as publish_module
from packages.agent_runtime.frozen import parse_frozen_agent_spec
from packages.agent_runtime.publish import AgentPublishService, _resolved_spec
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from tests.unit.test_agent_publish import _agent as _published_agent
from tests.unit.test_agent_publish import _profile

_KB_ID = UUID("00000000-0000-0000-0000-0000000000a1")
_PUBLICATION_ID = UUID("00000000-0000-0000-0000-0000000000b2")
_PUBLICATION_HASH = "c" * 64


def _context() -> WorkspaceExecutionContext:
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
        return self._scalars_results.pop(0)


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
    assert await service._resolve_wiki_binding(session, _context(), _agent()) is None


@pytest.mark.asyncio
async def test_resolve_wiki_binding_pinned_freezes_publication_identity(stub_wiki):
    service = AgentPublishService()
    agent = _agent()
    session = _FakeSession(
        scalar_results=[_binding(), _compilation()],
    )
    resolved = await service._resolve_wiki_binding(session, _context(), agent)
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
        await service._resolve_wiki_binding(session, _context(), _agent())
    assert error.value.code == "WIKI_BINDING_INVALID"


@pytest.mark.asyncio
async def test_resolve_wiki_binding_rejects_unpublished_compilation(stub_wiki):
    service = AgentPublishService()
    session = _FakeSession(
        scalar_results=[_binding(), _compilation(status="READY")],
    )
    with pytest.raises(AgentHubError) as error:
        await service._resolve_wiki_binding(session, _context(), _agent())
    assert error.value.code == "WIKI_BINDING_INVALID"


@pytest.mark.asyncio
async def test_resolve_wiki_binding_rejects_revoked_publication(stub_wiki):
    service = AgentPublishService()
    stub_wiki.revoked = True
    session = _FakeSession(
        scalar_results=[_binding(), _compilation()],
    )
    with pytest.raises(AgentHubError) as error:
        await service._resolve_wiki_binding(session, _context(), _agent())
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
    resolved = await service._resolve_wiki_binding(session, _context(), _agent())
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
        await service._resolve_wiki_binding(session, _context(), _agent())
    assert error.value.code == "WIKI_BINDING_UNRESOLVED"
