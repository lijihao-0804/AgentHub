"""Provider endpoint freezing and dispatch-time binding checks."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest

from packages.agent_runtime import publish as publish_module
from packages.agent_runtime.frozen import parse_frozen_agent_spec
from packages.agent_runtime.models import Agent
from packages.agent_runtime.publish import AgentPublishService, _resolved_spec
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.model_gateway.contracts import (
    PROVIDER_EGRESS_POLICY_VERSION,
    CapabilityRequirements,
    ModelCapabilities,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ProviderEgress,
    ResolvedModelExecutionPlan,
    ResolvedModelExecutionProfile,
    RetryPolicy,
)
from packages.model_gateway.errors import ModelGatewayError, ModelGatewayErrorCode
from packages.model_gateway.gateway import ModelGatewayService
from packages.model_gateway.models import ProviderCredential
from packages.model_gateway.profile_resolution import ResolvedModelProfile
from packages.wiki.contracts import freeze_plan, thaw_plan


def _context(workspace_id: UUID) -> WorkspaceExecutionContext:
    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(request_id="egress-binding", trace_id="egress-binding"),
            organization_id=str(uuid4()),
            org_role="OWNER",
        ),
        workspace_id=str(workspace_id),
        workspace_role="DEVELOPER",
        permissions=frozenset({"agent_edit", "workspace_read"}),
    )


def _agent(*, workspace_id: UUID, model_profile_id: UUID, runtime_config=None) -> Agent:
    return Agent(
        id=uuid4(),
        workspace_id=workspace_id,
        name="Egress binding test",
        system_prompt="Answer using frozen model identities.",
        prompt_version=1,
        model_profile_id=model_profile_id,
        knowledge_binding_mode="PINNED",
        model_retry_policy={"max_attempts": 1},
        retrieval_config={},
        runtime_config=runtime_config or {},
    )


def _resolved_profile(
    *,
    workspace_id: UUID,
    credential_id: UUID,
    profile_id: UUID | None = None,
    model: str = "offline-model",
) -> ResolvedModelProfile:
    return ResolvedModelProfile(
        id=profile_id or uuid4(),
        workspace_id=workspace_id,
        provider_credential_id=credential_id,
        model=model,
        temperature=Decimal("0"),
        max_tokens=128,
        timeout_seconds=Decimal("10"),
        fallback_profile_id=None,
        capabilities=ModelCapabilities(
            tool_calling=True,
            streaming=True,
            structured_output=True,
            vision=True,
            max_context_tokens=64_000,
        ),
    )


def _credential(
    *,
    workspace_id: UUID,
    provider: str = "offline",
    base_url: str | None,
    secret: str = "not-for-logs",
) -> ProviderCredential:
    return ProviderCredential(
        id=uuid4(),
        workspace_id=workspace_id,
        provider=provider,
        name="offline credential",
        secret=secret,
        base_url=base_url,
        enabled=True,
    )


def _egress(base_url: str | None) -> ProviderEgress:
    return ProviderEgress(
        policy_version=PROVIDER_EGRESS_POLICY_VERSION,
        base_url=base_url,
    )


def _legacy_spec(schema_version: int) -> tuple[Agent, dict[str, Any]]:
    workspace_id = uuid4()
    credential_id = uuid4()
    profile = _resolved_profile(
        workspace_id=workspace_id,
        credential_id=credential_id,
    )
    runtime_config: dict[str, Any] = {}
    if schema_version >= 3:
        runtime_config["context_policy"] = {"policy_version": "adaptive_context_v1"}
    if schema_version >= 4:
        runtime_config["history_policy"] = "deterministic_history_v1"
    if schema_version >= 5:
        runtime_config["summary_policy"] = {
            "enabled": True,
            "policy_version": "thread_summary_policy_v1",
            "prompt_version": "summary_candidate_zh_v1",
            "validator_version": "summary_mechanical_v1",
            "tokenizer_id": "approx_language_v1",
            "tokenizer_version": 1,
            "soft_trigger_percent": 70,
            "hard_trigger_percent": 85,
            "target_percent": 55,
        }
    agent = _agent(
        workspace_id=workspace_id,
        model_profile_id=profile.id,
        runtime_config=runtime_config,
    )
    resolved = _resolved_spec(
        agent=agent,
        model=((profile,), {profile.id: "offline"}),
        bindings=(),
        tools=(),
    )
    resolved["spec_schema_version"] = schema_version
    return agent, resolved


def _publication_model(
    *,
    workspace_id: UUID,
    primary_url: str | None,
    fallback_url: str | None = "https://fallback.example/v1",
) -> tuple[
    tuple[ResolvedModelProfile, ...],
    dict[UUID, str],
    dict[UUID, ProviderEgress],
]:
    primary = _resolved_profile(
        workspace_id=workspace_id,
        credential_id=uuid4(),
        model="primary",
    )
    fallback = _resolved_profile(
        workspace_id=workspace_id,
        credential_id=uuid4(),
        model="fallback",
    )
    return (
        (primary, fallback),
        {primary.id: "offline", fallback.id: "offline"},
        {
            primary.id: _egress(primary_url),
            fallback.id: _egress(fallback_url),
        },
    )


@pytest.mark.asyncio
async def test_publish_resolution_freezes_normalized_primary_and_explicit_null_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id = uuid4()
    primary_credential = _credential(
        workspace_id=workspace_id,
        base_url="HTTPS://Provider.Example.:443/v1/",
        secret="primary-secret",
    )
    fallback_credential = _credential(
        workspace_id=workspace_id,
        base_url=None,
        secret="fallback-secret",
    )
    primary = _resolved_profile(
        workspace_id=workspace_id,
        credential_id=primary_credential.id,
        model="primary",
    )
    fallback = _resolved_profile(
        workspace_id=workspace_id,
        credential_id=fallback_credential.id,
        model="fallback",
    )

    class Repository:
        def __init__(self, _session) -> None:
            self.credentials = {
                primary_credential.id: primary_credential,
                fallback_credential.id: fallback_credential,
            }

        async def get_provider_credential(self, _context, credential_id):
            return self.credentials[credential_id]

    class Resolver:
        def __init__(self, _repository) -> None:
            pass

        async def resolve_chain(self, _context, _profile_id, *, required_capabilities):
            del required_capabilities
            return primary, fallback

    class Session:
        async def scalars(self, _statement):
            return []

    monkeypatch.setattr(publish_module, "SqlAlchemyModelGatewayRepository", Repository)
    monkeypatch.setattr(publish_module, "ModelProfileResolver", Resolver)
    agent = _agent(workspace_id=workspace_id, model_profile_id=primary.id)
    service = object.__new__(AgentPublishService)

    resolution = await service._resolve_model(Session(), _context(workspace_id), agent)

    async def resolve_model(_session, _context, _agent):
        return resolution

    async def resolve_knowledge(_session, _context, _agent):
        return ()

    async def resolve_wiki_binding(_session, _context, _agent):
        return None

    async def resolve_tools(_session, _context, _agent, *, wiki_bound):
        del wiki_bound
        return ()

    service._resolve_model = resolve_model
    service._resolve_knowledge = resolve_knowledge
    service._resolve_wiki_binding = resolve_wiki_binding
    service._resolve_tools = resolve_tools
    resolved, resolved_hash = await service._resolve_draft_spec(
        Session(),
        _context(workspace_id),
        agent,
    )

    assert resolved["spec_schema_version"] == 2
    assert resolved_hash == canonical_json_hash(resolved)
    assert resolved["model"]["provider_egress"] == {
        "policy_version": "provider_egress_v1",
        "base_url": "https://provider.example/v1",
    }
    assert resolved["model"]["fallback_profiles"][0]["provider_egress"] == {
        "policy_version": "provider_egress_v1",
        "base_url": None,
    }
    assert "primary-secret" not in str(resolved)
    assert "fallback-secret" not in str(resolved)

    changed_egress = deepcopy(resolution)
    changed_egress[2][primary.id] = _egress("https://changed.example/v1")
    changed = _resolved_spec(
        agent=agent,
        model=changed_egress,
        bindings=(),
        tools=(),
    )
    assert canonical_json_hash(resolved) != canonical_json_hash(changed)

    frozen = parse_frozen_agent_spec(resolved, workspace_id=workspace_id)
    assert frozen.model_plan.primary.provider_egress == _egress(
        "https://provider.example/v1"
    )
    assert frozen.model_plan.fallbacks[0].provider_egress == _egress(None)


@pytest.mark.parametrize("schema_version", [1, 2, 3, 4, 5])
def test_legacy_specs_parse_without_backfilling_egress_or_changing_hash(
    schema_version: int,
) -> None:
    agent, resolved = _legacy_spec(schema_version)
    original = deepcopy(resolved)
    original_hash = canonical_json_hash(resolved)

    assert "provider_egress" not in resolved["model"]
    frozen = parse_frozen_agent_spec(resolved, workspace_id=agent.workspace_id)

    assert frozen.model_plan.primary.provider_egress is None
    assert resolved == original
    assert canonical_json_hash(resolved) == original_hash


@pytest.mark.parametrize(
    "provider_egress",
    [
        None,
        [],
        {"policy_version": "provider_egress_v2", "base_url": "https://offline.example/v1"},
        {"policy_version": "provider_egress_v1"},
        {
            "policy_version": "provider_egress_v1",
            "base_url": "https://offline.example/v1",
            "unknown": True,
        },
        {
            "policy_version": "provider_egress_v1",
            "base_url": "https://user:pass@offline.example/v1",
        },
    ],
)
def test_illegal_or_unknown_provider_egress_mappings_fail_closed(provider_egress) -> None:
    agent, resolved = _legacy_spec(2)
    resolved["model"]["provider_egress"] = provider_egress

    with pytest.raises(AgentHubError) as raised:
        parse_frozen_agent_spec(resolved, workspace_id=agent.workspace_id)

    assert raised.value.code == "AGENT_VERSION_MODEL_BINDING_INVALID"


def test_plan_serialization_omits_legacy_none_and_round_trips_new_egress() -> None:
    workspace_id = uuid4()
    legacy_profile = ResolvedModelExecutionProfile(
        id=uuid4(),
        workspace_id=workspace_id,
        provider_credential_id=uuid4(),
        provider="offline",
        model="legacy",
        temperature=Decimal("0"),
        max_tokens=128,
        timeout_seconds=Decimal("10"),
        capabilities=ModelCapabilities(),
    )
    legacy = freeze_plan(ResolvedModelExecutionPlan(primary=legacy_profile))
    assert "provider_egress" not in legacy["primary"]
    assert thaw_plan(legacy).primary.provider_egress is None

    current_profile = ResolvedModelExecutionProfile(
        id=uuid4(),
        workspace_id=workspace_id,
        provider_credential_id=uuid4(),
        provider="offline",
        model="current",
        temperature=Decimal("0"),
        max_tokens=128,
        timeout_seconds=Decimal("10"),
        capabilities=ModelCapabilities(),
        provider_egress=_egress("https://offline.example/v1/"),
    )
    current = freeze_plan(ResolvedModelExecutionPlan(primary=current_profile))
    assert current["primary"]["provider_egress"] == {
        "policy_version": "provider_egress_v1",
        "base_url": "https://offline.example/v1",
    }
    assert thaw_plan(current).primary.provider_egress == _egress(
        "https://offline.example/v1"
    )


class _Repository:
    def __init__(self, credentials: list[ProviderCredential]) -> None:
        self.credentials = {credential.id: credential for credential in credentials}

    async def get_provider_credential(self, context, credential_id):
        credential = self.credentials.get(credential_id)
        if credential is None or str(credential.workspace_id) != context.workspace_id:
            return None
        return credential


class _Adapter:
    def __init__(self, *, error: ModelGatewayError | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str, str | None]] = []

    async def complete(self, profile, credential, request) -> ModelResponse:
        del request
        self.calls.append((profile.model, credential.secret, credential.base_url))
        if self.error is not None:
            raise self.error
        return ModelResponse(
            content="offline",
            provider=credential.provider,
            model=profile.model,
        )


def _frozen_profile(
    *,
    workspace_id: UUID,
    credential_id: UUID,
    model: str,
    base_url: str | None,
    provider: str = "offline",
    vision: bool = True,
) -> ResolvedModelExecutionProfile:
    return ResolvedModelExecutionProfile(
        id=uuid4(),
        workspace_id=workspace_id,
        provider_credential_id=credential_id,
        provider=provider,
        model=model,
        temperature=Decimal("0"),
        max_tokens=128,
        timeout_seconds=Decimal("10"),
        capabilities=ModelCapabilities(
            tool_calling=True,
            streaming=True,
            vision=vision,
            max_context_tokens=8_192,
        ),
        provider_egress=_egress(base_url),
    )


def _request(*, required: frozenset[str] = frozenset()) -> ModelRequest:
    return ModelRequest(
        messages=(ModelMessage(role="user", content="hello"),),
        required_capabilities=CapabilityRequirements(required=required),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "current_base_url",
    [
        "https://changed.example/v1",
        "https://user:pass@changed.example/v1",
    ],
)
async def test_endpoint_change_is_rejected_before_dispatch(
    current_base_url: str,
) -> None:
    workspace_id = uuid4()
    credential = _credential(
        workspace_id=workspace_id,
        base_url=current_base_url,
    )
    profile = _frozen_profile(
        workspace_id=workspace_id,
        credential_id=credential.id,
        model="offline",
        base_url="https://frozen.example/v1",
    )
    adapter = _Adapter()
    gateway = ModelGatewayService(_Repository([credential]), adapter)

    with pytest.raises(ModelGatewayError) as raised:
        await gateway.generate_resolved(
            _context(workspace_id),
            ResolvedModelExecutionPlan(primary=profile),
            _request(),
        )

    assert raised.value.code is ModelGatewayErrorCode.AGENT_VERSION_MODEL_BINDING_INVALID
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_legacy_profile_does_not_invent_an_endpoint_binding() -> None:
    workspace_id = uuid4()
    credential = _credential(
        workspace_id=workspace_id,
        base_url="https://credential.example/v1",
    )
    profile = ResolvedModelExecutionProfile(
        id=uuid4(),
        workspace_id=workspace_id,
        provider_credential_id=credential.id,
        provider="offline",
        model="legacy",
        temperature=Decimal("0"),
        max_tokens=128,
        timeout_seconds=Decimal("10"),
        capabilities=ModelCapabilities(streaming=True),
    )
    adapter = _Adapter()
    gateway = ModelGatewayService(_Repository([credential]), adapter)

    response = await gateway.generate_resolved(
        _context(workspace_id),
        ResolvedModelExecutionPlan(primary=profile),
        _request(),
    )

    assert response.content == "offline"
    assert len(adapter.calls) == 1
    assert profile.provider_egress is None


@pytest.mark.asyncio
async def test_equivalent_normalized_url_and_secret_rotation_are_allowed() -> None:
    workspace_id = uuid4()
    credential = _credential(
        workspace_id=workspace_id,
        base_url="https://PROVIDER.example:443/v1/",
        secret="rotated-secret",
    )
    profile = _frozen_profile(
        workspace_id=workspace_id,
        credential_id=credential.id,
        model="offline",
        base_url="https://provider.example/v1",
    )
    adapter = _Adapter()
    gateway = ModelGatewayService(_Repository([credential]), adapter)

    response = await gateway.generate_resolved(
        _context(workspace_id),
        ResolvedModelExecutionPlan(primary=profile, retry_policy=RetryPolicy()),
        _request(),
    )

    assert response.content == "offline"
    assert adapter.calls == [
        ("offline", "rotated-secret", "https://PROVIDER.example:443/v1/")
    ]


@pytest.mark.asyncio
async def test_explicit_null_endpoint_binds_current_null_and_rejects_a_new_url() -> None:
    workspace_id = uuid4()
    null_credential = _credential(
        workspace_id=workspace_id,
        provider="deepseek",
        base_url=None,
    )
    null_profile = _frozen_profile(
        workspace_id=workspace_id,
        credential_id=null_credential.id,
        model="offline",
        base_url=None,
        provider="deepseek",
    )
    adapter = _Adapter()
    gateway = ModelGatewayService(_Repository([null_credential]), adapter)
    await gateway.generate_resolved(
        _context(workspace_id),
        ResolvedModelExecutionPlan(primary=null_profile),
        _request(),
    )
    assert len(adapter.calls) == 1

    moved_credential = _credential(
        workspace_id=workspace_id,
        provider="deepseek",
        base_url="https://now-configured.example/v1",
    )
    moved_profile = _frozen_profile(
        workspace_id=workspace_id,
        credential_id=moved_credential.id,
        model="offline",
        base_url=None,
        provider="deepseek",
    )
    moved_adapter = _Adapter()
    moved_gateway = ModelGatewayService(_Repository([moved_credential]), moved_adapter)

    with pytest.raises(ModelGatewayError) as raised:
        await moved_gateway.generate_resolved(
            _context(workspace_id),
            ResolvedModelExecutionPlan(primary=moved_profile),
            _request(),
        )

    assert raised.value.code is ModelGatewayErrorCode.AGENT_VERSION_MODEL_BINDING_INVALID
    assert moved_adapter.calls == []


@pytest.mark.asyncio
async def test_null_policy_does_not_bypass_provider_identity_gate() -> None:
    workspace_id = uuid4()
    credential = _credential(
        workspace_id=workspace_id,
        provider="unknown-provider",
        base_url=None,
    )
    profile = _frozen_profile(
        workspace_id=workspace_id,
        credential_id=credential.id,
        model="offline",
        base_url=None,
        provider="deepseek",
    )
    adapter = _Adapter()
    gateway = ModelGatewayService(_Repository([credential]), adapter)

    with pytest.raises(ModelGatewayError) as raised:
        await gateway.generate_resolved(
            _context(workspace_id),
            ResolvedModelExecutionPlan(primary=profile),
            _request(),
        )

    assert raised.value.code is ModelGatewayErrorCode.AGENT_VERSION_MODEL_BINDING_INVALID
    assert adapter.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_index", [0, 1])
async def test_primary_and_fallback_endpoints_are_both_checked(changed_index: int) -> None:
    workspace_id = uuid4()
    primary_credential = _credential(
        workspace_id=workspace_id,
        base_url="https://primary.example/v1",
    )
    fallback_credential = _credential(
        workspace_id=workspace_id,
        base_url="https://fallback.example/v1",
    )
    if changed_index == 0:
        primary_credential.base_url = "https://primary-changed.example/v1"
    else:
        fallback_credential.base_url = "https://fallback-changed.example/v1"
    primary = _frozen_profile(
        workspace_id=workspace_id,
        credential_id=primary_credential.id,
        model="primary",
        base_url="https://primary.example/v1",
    )
    fallback = _frozen_profile(
        workspace_id=workspace_id,
        credential_id=fallback_credential.id,
        model="fallback",
        base_url="https://fallback.example/v1",
    )
    adapter = _Adapter()
    gateway = ModelGatewayService(
        _Repository([primary_credential, fallback_credential]),
        adapter,
    )

    with pytest.raises(ModelGatewayError) as raised:
        await gateway.generate_resolved(
            _context(workspace_id),
            ResolvedModelExecutionPlan(primary=primary, fallbacks=(fallback,)),
            _request(),
        )

    assert raised.value.code is ModelGatewayErrorCode.AGENT_VERSION_MODEL_BINDING_INVALID
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_capability_gate_still_precedes_endpoint_binding_check() -> None:
    workspace_id = uuid4()
    credential = _credential(
        workspace_id=workspace_id,
        base_url="https://changed.example/v1",
    )
    profile = _frozen_profile(
        workspace_id=workspace_id,
        credential_id=credential.id,
        model="offline",
        base_url="https://frozen.example/v1",
        vision=False,
    )
    adapter = _Adapter()
    gateway = ModelGatewayService(_Repository([credential]), adapter)

    with pytest.raises(ModelGatewayError) as raised:
        await gateway.generate_resolved(
            _context(workspace_id),
            ResolvedModelExecutionPlan(primary=profile),
            _request(required=frozenset({"vision"})),
        )

    assert raised.value.code is ModelGatewayErrorCode.MODEL_CAPABILITY_MISMATCH
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_ordinary_model_error_keeps_its_original_code_after_binding_check() -> None:
    workspace_id = uuid4()
    credential = _credential(
        workspace_id=workspace_id,
        base_url="https://provider.example/v1",
    )
    profile = _frozen_profile(
        workspace_id=workspace_id,
        credential_id=credential.id,
        model="offline",
        base_url="https://provider.example/v1",
    )
    adapter = _Adapter(error=ModelGatewayError(ModelGatewayErrorCode.MODEL_TIMEOUT))
    gateway = ModelGatewayService(_Repository([credential]), adapter)

    with pytest.raises(ModelGatewayError) as raised:
        await gateway.generate_resolved(
            _context(workspace_id),
            ResolvedModelExecutionPlan(primary=profile),
            _request(),
        )

    assert raised.value.code is ModelGatewayErrorCode.MODEL_TIMEOUT
    assert len(adapter.calls) == 1
