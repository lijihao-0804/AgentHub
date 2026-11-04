"""Formal judge endpoint freezing and manifest compatibility."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from apps.api.routes import evaluation as evaluation_routes
from packages.control_plane.rbac import EVALUATION_MANAGE
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.evaluation.judge import (
    JUDGE_MANIFEST_KEY,
    FrozenJudgeEnsemble,
    freeze_judge_profile,
    judge_ensemble_from_manifest,
    judge_profile_from_manifest,
    judge_profile_to_plan,
)
from packages.evaluation.reproducibility import default_evaluator_manifest
from packages.model_gateway.contracts import (
    PROVIDER_EGRESS_POLICY_VERSION,
    ModelMessage,
    ModelRequest,
    ProviderEgress,
)
from packages.model_gateway.errors import ModelGatewayError, ModelGatewayErrorCode
from packages.model_gateway.gateway import ModelGatewayService
from packages.model_gateway.models import ProviderCredential

_PROFILE_ID = UUID("21111111-1111-1111-1111-111111111111")
_CREDENTIAL_ID = UUID("22222222-2222-2222-2222-222222222222")


def _resolved_profile(model: str = "judge-model") -> SimpleNamespace:
    return SimpleNamespace(
        id=_PROFILE_ID,
        workspace_id=uuid4(),
        provider_credential_id=_CREDENTIAL_ID,
        model=model,
        temperature=Decimal("0"),
        max_tokens=512,
        timeout_seconds=Decimal("30"),
    )


def _egress(base_url: str | None) -> ProviderEgress:
    return ProviderEgress(
        policy_version=PROVIDER_EGRESS_POLICY_VERSION,
        base_url=base_url,
    )


def _frozen(
    *,
    base_url: str | None = None,
    with_egress: bool = True,
    model: str = "judge-model",
    provider: str = "openai-compatible",
):
    return freeze_judge_profile(
        _resolved_profile(model),
        provider=provider,
        provider_egress=_egress(base_url) if with_egress else None,
    )


def test_new_judge_endpoint_is_in_the_existing_identity_hash() -> None:
    legacy = _frozen(with_egress=False)
    endpoint = _frozen(base_url="HTTPS://Judge.Example.:443/v1/")
    other_endpoint = _frozen(base_url="https://other.example/v1")
    explicit_null = _frozen(base_url=None)

    assert "provider_egress" not in legacy.projection()
    assert endpoint.projection()["provider_egress"] == {
        "policy_version": "provider_egress_v1",
        "base_url": "https://judge.example/v1",
    }
    assert explicit_null.projection()["provider_egress"] == {
        "policy_version": "provider_egress_v1",
        "base_url": None,
    }
    assert legacy.evaluator_version != endpoint.evaluator_version
    assert endpoint.evaluator_version != other_endpoint.evaluator_version
    assert legacy.evaluator_version != explicit_null.evaluator_version


@pytest.mark.parametrize("base_url", ["https://judge.example/v1", None])
def test_frozen_judge_manifest_round_trips_provider_egress(
    base_url: str | None,
) -> None:
    profile = _frozen(base_url=base_url)
    manifest = default_evaluator_manifest(profile)
    original = deepcopy(manifest)

    rebuilt = judge_profile_from_manifest(manifest)

    assert rebuilt == profile
    assert rebuilt is not None
    assert rebuilt.provider_egress == _egress(base_url)
    assert manifest == original
    assert manifest["evaluator_versions"][JUDGE_MANIFEST_KEY] == profile.evaluator_version


def test_legacy_judge_projection_omits_the_field_and_stays_unchanged() -> None:
    profile = _frozen(with_egress=False)
    manifest = default_evaluator_manifest(profile)
    original = deepcopy(manifest)
    original_version = manifest["evaluator_versions"][JUDGE_MANIFEST_KEY]

    rebuilt = judge_profile_from_manifest(manifest)

    assert "provider_egress" not in profile.projection()
    assert rebuilt == profile
    assert rebuilt is not None
    assert rebuilt.provider_egress is None
    assert manifest == original
    assert manifest["evaluator_versions"][JUDGE_MANIFEST_KEY] == original_version
    plan = judge_profile_to_plan(profile, _resolved_profile().workspace_id)
    assert plan.primary.provider_egress is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda projection: projection.pop("base_url"),
        lambda projection: projection.update(extra=True),
        lambda projection: projection.update(policy_version="provider_egress_v2"),
        lambda projection: projection.update(base_url="https://user:pass@judge.example/v1"),
        lambda projection: projection.update(base_url=None),
    ],
)
def test_invalid_or_unknown_judge_egress_fails_closed(mutate) -> None:
    manifest = default_evaluator_manifest(_frozen(base_url="https://judge.example/v1"))
    mutate(manifest["judge_profile"]["provider_egress"])

    with pytest.raises(AgentHubError) as raised:
        judge_profile_from_manifest(manifest)

    assert raised.value.code == "EVALUATION_JUDGE_MANIFEST_INVALID"


def test_judge_ensemble_members_round_trip_provider_egress() -> None:
    first = _frozen(base_url="https://first.example/v1", model="first")
    second = _frozen(base_url=None, model="second")
    ensemble = FrozenJudgeEnsemble((first, second), strategy="SINGLE")
    manifest = default_evaluator_manifest(judge_ensemble=ensemble)

    rebuilt = judge_ensemble_from_manifest(manifest)

    assert rebuilt == ensemble
    assert rebuilt is not None
    assert rebuilt.members[0].provider_egress == _egress("https://first.example/v1")
    assert rebuilt.members[1].provider_egress == _egress(None)


class _Repository:
    def __init__(self, credential: ProviderCredential) -> None:
        self.credential = credential

    async def get_provider_credential(self, context, credential_id):
        if (
            credential_id == self.credential.id
            and context.workspace_id == str(self.credential.workspace_id)
        ):
            return self.credential
        return None


class _Adapter:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, profile, credential, request):
        del profile, credential, request
        self.calls += 1
        raise AssertionError("provider dispatch must not occur")


def _context(workspace_id: UUID) -> WorkspaceExecutionContext:
    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(request_id="judge-egress", trace_id="judge-egress"),
            organization_id=str(uuid4()),
            org_role="OWNER",
        ),
        workspace_id=str(workspace_id),
        workspace_role="DEVELOPER",
        permissions=frozenset({EVALUATION_MANAGE}),
    )


@pytest.mark.asyncio
async def test_frozen_judge_plan_rejects_credential_endpoint_drift_before_dispatch() -> None:
    workspace_id = uuid4()
    credential = ProviderCredential(
        id=_CREDENTIAL_ID,
        workspace_id=workspace_id,
        provider="openai-compatible",
        name="rotatable judge",
        secret="not-for-logs",
        base_url="https://changed.example/v1",
        enabled=True,
    )
    profile = _frozen(base_url="https://judge.example/v1")
    plan = judge_profile_to_plan(profile, workspace_id)
    adapter = _Adapter()
    gateway = ModelGatewayService(_Repository(credential), adapter)

    with pytest.raises(ModelGatewayError) as raised:
        await gateway.generate_resolved(
            _context(workspace_id),
            plan,
            ModelRequest(messages=(ModelMessage(role="user", content="score this"),)),
        )

    assert plan.primary.provider_egress == _egress("https://judge.example/v1")
    assert raised.value.code is ModelGatewayErrorCode.AGENT_VERSION_MODEL_BINDING_INVALID
    assert adapter.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base_url",
    ["HTTPS://Judge.Example.:443/v1/", None],
)
async def test_formal_experiment_probe_freezes_the_resolved_credential_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    base_url: str | None,
) -> None:
    workspace_id = uuid4()
    profile = _resolved_profile()
    credential = ProviderCredential(
        id=profile.provider_credential_id,
        workspace_id=workspace_id,
        provider="openai-compatible",
        name="formal judge",
        secret="not-for-logs",
        base_url=base_url,
        enabled=True,
    )

    class Repository:
        def __init__(self, _session) -> None:
            pass

        async def get_provider_credential(self, _context, credential_id):
            return credential if credential_id == credential.id else None

    class Resolver:
        def __init__(self, _repository) -> None:
            pass

        async def resolve_chain(self, _context, profile_id):
            return (profile,) if profile_id == profile.id else ()

    monkeypatch.setattr(
        evaluation_routes,
        "SqlAlchemyModelGatewayRepository",
        Repository,
    )
    monkeypatch.setattr(evaluation_routes, "ModelProfileResolver", Resolver)

    frozen = await evaluation_routes._freeze_judge_profile(
        object(),
        _context(workspace_id),
        profile.id,
    )

    assert frozen is not None
    expected = None if base_url is None else "https://judge.example/v1"
    assert frozen.provider_egress == _egress(expected)
