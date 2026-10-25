"""W3 fixed resolution: run freezing, replay validation, experiment freezing."""

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from packages.agent_runtime import runtime as runtime_module
from packages.agent_runtime.frozen import FrozenWikiBinding
from packages.agent_runtime.runtime import (
    AgentRunExecutionOverrides,
    AgentRunService,
    FrozenAgentSpec,
)
from packages.core.errors.exceptions import AgentHubError
from packages.evaluation.experiments import ExperimentService
from packages.evaluation.reproducibility import normalize_wiki_publications, variant_hash
from packages.model_gateway.contracts import (
    ModelCapabilities,
    ResolvedModelExecutionPlan,
    ResolvedModelExecutionProfile,
    RetryPolicy,
)
from tests.unit.test_wiki_binding_publish import _FakeSession, _StubWikiService

pytestmark = pytest.mark.asyncio

_WS = UUID("00000000-0000-0000-0000-0000000000w1".replace("w", "1"))
_KB = UUID("00000000-0000-0000-0000-0000000000a1")
_PUB = UUID("00000000-0000-0000-0000-0000000000b2")
_HASH = "c" * 64


def _context():
    from packages.core.execution_context.models import (
        OrganizationContext,
        PrincipalContext,
        WorkspaceExecutionContext,
    )

    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(request_id="wiki-run", trace_id="wiki-run"),
            organization_id=str(uuid4()),
            org_role="OWNER",
        ),
        workspace_id=str(_WS),
        workspace_role="DEVELOPER",
        permissions=frozenset({"agent_run"}),
    )


def _spec(binding: FrozenWikiBinding | None) -> FrozenAgentSpec:
    profile = ResolvedModelExecutionProfile(
        id=uuid4(),
        workspace_id=_WS,
        provider_credential_id=uuid4(),
        provider="deepseek",
        model="deepseek-chat",
        temperature=0.1,
        max_tokens=2000,
        timeout_seconds=30,
        capabilities=ModelCapabilities(),
    )
    return FrozenAgentSpec(
        model_plan=ResolvedModelExecutionPlan(profile, (), RetryPolicy(max_attempts=1)),
        system_prompt="Answer from context.",
        prompt_version=1,
        runtime={},
        wiki_binding=binding,
    )


def _binding(mode="PINNED"):
    return FrozenWikiBinding(
        knowledge_base_id=_KB,
        binding_mode=mode,
        wiki_publication_id=_PUB,
        publication_hash=_HASH,
    )


def _publication(status="PUBLISHED", content_hash=_HASH):
    return SimpleNamespace(
        id=_PUB,
        workspace_id=_WS,
        knowledge_base_id=_KB,
        status=status,
        content_hash=content_hash,
    )


@pytest.fixture
def stub_wiki(monkeypatch):
    stub = _StubWikiService()
    monkeypatch.setattr(runtime_module, "WikiService", lambda: stub)
    return stub


async def test_resolve_returns_empty_without_binding(stub_wiki):
    result = await AgentRunService._resolve_effective_wiki_publications(
        _FakeSession(), _context(), _spec(None)
    )
    assert result == []


async def test_resolve_freezes_pinned_publication(stub_wiki):
    session = _FakeSession(scalar_results=[_publication()])
    result = await AgentRunService._resolve_effective_wiki_publications(
        session, _context(), _spec(_binding())
    )
    assert result == [
        {
            "knowledge_base_id": str(_KB),
            "binding_mode": "PINNED",
            "wiki_publication_id": str(_PUB),
            "publication_hash": _HASH,
        }
    ]


@pytest.mark.parametrize(
    ("status", "content_hash"),
    [("READY", _HASH), ("PUBLISHED", "d" * 64)],
)
async def test_resolve_rejects_integrity_drift(stub_wiki, status, content_hash):
    session = _FakeSession(scalar_results=[_publication(status, content_hash)])
    with pytest.raises(AgentHubError) as error:
        await AgentRunService._resolve_effective_wiki_publications(
            session, _context(), _spec(_binding())
        )
    assert error.value.code == "AGENT_VERSION_INTEGRITY_ERROR"


async def test_new_run_refused_when_publication_revoked(stub_wiki):
    stub_wiki.revoked = True
    session = _FakeSession(scalar_results=[_publication()])
    with pytest.raises(AgentHubError) as error:
        await AgentRunService._resolve_effective_wiki_publications(
            session, _context(), _spec(_binding())
        )
    assert error.value.code == "WIKI_PUBLICATION_REVOKED"


def _overrides(*publications):
    return AgentRunExecutionOverrides.for_evaluation([], wiki_publications=list(publications))


async def test_frozen_wiki_replay_accepts_matching_overrides(stub_wiki):
    frozen = {
        "knowledge_base_id": str(_KB),
        "binding_mode": "PINNED",
        "wiki_publication_id": str(_PUB),
        "publication_hash": _HASH,
    }
    session = _FakeSession(scalar_results=[_publication()])
    result = await AgentRunService._validate_frozen_execution_wiki(
        session, _context(), _spec(_binding()), _overrides(frozen)
    )
    assert result == [frozen]


async def test_frozen_wiki_replay_rejects_drifted_overrides(stub_wiki):
    drifted = {
        "knowledge_base_id": str(_KB),
        "binding_mode": "PINNED",
        "wiki_publication_id": str(_PUB),
        "publication_hash": "d" * 64,
    }
    with pytest.raises(AgentHubError) as error:
        await AgentRunService._validate_frozen_execution_wiki(
            _FakeSession(), _context(), _spec(_binding()), _overrides(drifted)
        )
    assert error.value.code == "EXPERIMENT_REPRODUCIBILITY_VIOLATION"


async def test_frozen_wiki_replay_rejects_missing_override(stub_wiki):
    with pytest.raises(AgentHubError) as error:
        await AgentRunService._validate_frozen_execution_wiki(
            _FakeSession(), _context(), _spec(_binding()), _overrides()
        )
    assert error.value.code == "EXPERIMENT_REPRODUCIBILITY_VIOLATION"


async def test_frozen_wiki_replay_rejects_override_without_binding(stub_wiki):
    frozen = {
        "knowledge_base_id": str(_KB),
        "binding_mode": "PINNED",
        "wiki_publication_id": str(_PUB),
        "publication_hash": _HASH,
    }
    with pytest.raises(AgentHubError) as error:
        await AgentRunService._validate_frozen_execution_wiki(
            _FakeSession(), _context(), _spec(None), _overrides(frozen)
        )
    assert error.value.code == "EXPERIMENT_REPRODUCIBILITY_VIOLATION"


class _SessionFactory:
    def __init__(self, results):
        self._results = list(results)

    def __call__(self):
        return _SessionContext(self._results)


class _SessionContext:
    def __init__(self, results):
        self._results = results

    async def __aenter__(self):
        return _FakeSession(scalar_results=self._results)

    async def __aexit__(self, *exc):
        return False


async def test_replay_integrity_check_accepts_intact_and_empty():
    service = AgentRunService(_SessionFactory([]))
    run = SimpleNamespace(workspace_id=_WS, effective_wiki_publications=[])
    assert await service._frozen_wiki_publications_intact(run) is True
    run = SimpleNamespace(
        workspace_id=_WS,
        effective_wiki_publications=[
            {
                "knowledge_base_id": str(_KB),
                "binding_mode": "PINNED",
                "wiki_publication_id": str(_PUB),
                "publication_hash": _HASH,
            }
        ],
    )
    factory = _SessionFactory([_publication()])
    service = AgentRunService(factory)
    assert await service._frozen_wiki_publications_intact(run) is True


async def test_replay_integrity_check_detects_lost_publication():
    factory = _SessionFactory([None])
    service = AgentRunService(factory)
    run = SimpleNamespace(
        workspace_id=_WS,
        effective_wiki_publications=[
            {
                "knowledge_base_id": str(_KB),
                "binding_mode": "PINNED",
                "wiki_publication_id": str(_PUB),
                "publication_hash": _HASH,
            }
        ],
    )
    assert await service._frozen_wiki_publications_intact(run) is False


async def test_experiment_freeze_wiki_publications():
    service = ExperimentService()
    session = _FakeSession(
        scalar_results=[_publication()],
    )
    frozen = await service._freeze_wiki_publications(
        session,
        _WS,
        {
            "wiki_binding": {
                "schema_version": 1,
                "binding_mode": "PINNED",
                "knowledge_base_id": str(_KB),
                "wiki_publication_id": str(_PUB),
                "publication_hash": _HASH,
            }
        },
    )
    assert frozen == [
        {
            "knowledge_base_id": str(_KB),
            "binding_mode": "PINNED",
            "wiki_publication_id": str(_PUB),
            "publication_hash": _HASH,
        }
    ]


async def test_experiment_freeze_wiki_rejects_drifted_publication():
    service = ExperimentService()
    session = _FakeSession(scalar_results=[_publication(content_hash="d" * 64)])
    with pytest.raises(AgentHubError) as error:
        await service._freeze_wiki_publications(
            session,
            _WS,
            {
                "wiki_binding": {
                    "schema_version": 1,
                    "binding_mode": "PINNED",
                    "knowledge_base_id": str(_KB),
                    "wiki_publication_id": str(_PUB),
                    "publication_hash": _HASH,
                }
            },
        )
    assert error.value.code == "EVALUATION_WIKI_PUBLICATION_INVALID"


async def test_experiment_freeze_wiki_empty_without_binding():
    service = ExperimentService()
    assert await service._freeze_wiki_publications(_FakeSession(), _WS, {}) == []


def test_variant_hash_wiki_conditional_and_stable():
    base = dict(
        agent_version_id=str(uuid4()),
        resolved_spec_hash="a" * 64,
        effective_knowledge_snapshots=[],
        pricing_snapshot_id=str(uuid4()),
        pricing_snapshot_hash="b" * 64,
        variant_metadata={},
    )
    without_wiki = variant_hash(**base)
    with_empty_wiki = variant_hash(**base, effective_wiki_publications=[])
    assert without_wiki == with_empty_wiki
    with_wiki = variant_hash(
        **base,
        effective_wiki_publications=[
            {
                "knowledge_base_id": str(_KB),
                "binding_mode": "PINNED",
                "wiki_publication_id": str(_PUB),
                "publication_hash": _HASH,
            }
        ],
    )
    assert with_wiki != without_wiki


def test_normalize_wiki_publications_is_strict():
    good = [
        {
            "knowledge_base_id": str(_KB),
            "binding_mode": "PINNED",
            "wiki_publication_id": str(_PUB),
            "publication_hash": _HASH,
        }
    ]
    assert normalize_wiki_publications(good) == good
    with pytest.raises(AgentHubError):
        normalize_wiki_publications([{**good[0], "binding_mode": "OTHER"}])
    with pytest.raises(AgentHubError):
        normalize_wiki_publications(
            [{key: value for key, value in good[0].items() if key != "publication_hash"}]
        )
    with pytest.raises(AgentHubError):
        normalize_wiki_publications("nope")
