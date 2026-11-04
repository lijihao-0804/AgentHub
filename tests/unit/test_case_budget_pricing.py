from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from packages.evaluation.case_budget_pricing import (
    SUPPORTED_FEE_COVERAGE,
    BillingAttestation,
    BillingAttestationRegistry,
    BillingFeeKind,
    CaseBudgetPricingError,
    FrozenProviderRoute,
    InteractiveCaseAttemptPriceSource,
    RequestAdmissibilityEvidence,
)
from packages.evaluation.models import PricingSnapshot
from packages.evaluation.reproducibility import pricing_snapshot_content_hash
from packages.model_gateway.contracts import (
    PROVIDER_EGRESS_POLICY_VERSION,
    AliyunDeepSeekRequestMode,
    CapabilityRequirements,
    ModelCapabilities,
    ModelMessage,
    ModelRequest,
    ModelToolDefinition,
    RetryPolicy,
)
from packages.model_gateway.models import ModelProfile, ProviderCredential
from packages.model_gateway.profile_resolution import ResolvedModelProfile
from packages.model_gateway.run_budget_adapter import ModelAttemptKind


class _InputBoundValidator:
    identity = "synthetic-input-bound-validator-v1"

    def __init__(self, *, admitted: Any = True) -> None:
        self.admitted = admitted
        self.calls: list[tuple[ModelRequest, int]] = []

    def validate(
        self,
        request: ModelRequest,
        *,
        max_billable_input_tokens: int,
    ) -> Any:
        self.calls.append((request, max_billable_input_tokens))
        return self.admitted


class _Session:
    def __init__(
        self,
        *,
        snapshot: Any,
        profile: Any,
        credential: Any,
    ) -> None:
        self.objects = {
            PricingSnapshot: snapshot,
            ModelProfile: profile,
            ProviderCredential: credential,
        }

    async def get(self, model: type[Any], identity: UUID) -> Any | None:
        value = self.objects.get(model)
        if value is None or value.id != identity:
            return None
        return value


@dataclass(slots=True)
class _Fixture:
    route: FrozenProviderRoute
    evidence: RequestAdmissibilityEvidence
    snapshot: Any
    attestation: BillingAttestation
    profile: ResolvedModelProfile
    profile_row: Any
    credential: Any
    request: ModelRequest
    validator: _InputBoundValidator
    session: _Session
    registry: BillingAttestationRegistry
    source: InteractiveCaseAttemptPriceSource


def _fixture(
    *,
    provider: str = "deepseek",
    base_url: str | None = "HTTPS://PROVIDER.INVALID:443/v1/",
    credential_base_url: str | None | object = ...,
    max_input_tokens: int = 4,
    max_output_tokens: int = 3,
    input_price: Decimal = Decimal("2"),
    output_price: Decimal = Decimal("3"),
    cached_input_price: Decimal | None = Decimal("1"),
    validator_admitted: bool = True,
) -> _Fixture:
    workspace_id = uuid4()
    route = FrozenProviderRoute(
        provider=provider,
        model="deepseek-synthetic-v1",
        policy_version=PROVIDER_EGRESS_POLICY_VERSION,
        base_url=base_url,
    )
    snapshot = SimpleNamespace(
        id=uuid4(),
        workspace_id=workspace_id,
        name="synthetic-test-price",
        provider=route.provider,
        model=route.model,
        currency="CNY",
        input_price_per_1m=input_price,
        output_price_per_1m=output_price,
        cached_input_price_per_1m=cached_input_price,
        effective_at=datetime(2026, 11, 5, tzinfo=UTC),
        source_note="synthetic unit-test evidence; not a provider price",
        content_hash="",
    )
    snapshot.content_hash = pricing_snapshot_content_hash(snapshot)
    evidence = RequestAdmissibilityEvidence(
        max_billable_input_tokens=max_input_tokens,
        max_billable_output_tokens=max_output_tokens,
        fee_coverage=SUPPORTED_FEE_COVERAGE,
        request_mode=AliyunDeepSeekRequestMode.NON_THINKING,
        input_upper_bound_validator_id=_InputBoundValidator.identity,
    )
    attestation = BillingAttestation(
        identity="synthetic-deepseek-proof-v1",
        workspace_id=workspace_id,
        route=route,
        pricing_snapshot_id=snapshot.id,
        pricing_snapshot_hash=snapshot.content_hash,
        currency="CNY",
        input_price_per_1m=input_price,
        output_price_per_1m=output_price,
        cached_input_price_per_1m=cached_input_price,
        request_evidence=evidence,
    )
    # This deliberately synthetic capability value is never used as a quote bound.
    capabilities = ModelCapabilities(max_context_tokens=777_777)
    credential_id = uuid4()
    profile = ResolvedModelProfile(
        id=uuid4(),
        workspace_id=workspace_id,
        provider_credential_id=credential_id,
        model=route.model,
        temperature=Decimal("0"),
        max_tokens=3,
        timeout_seconds=Decimal("30"),
        fallback_profile_id=None,
        capabilities=capabilities,
    )
    profile_row = SimpleNamespace(
        id=profile.id,
        workspace_id=workspace_id,
        provider_credential_id=credential_id,
        model=route.model,
        temperature=profile.temperature,
        max_tokens=profile.max_tokens,
        timeout_seconds=profile.timeout_seconds,
        fallback_profile_id=None,
        capabilities={
            "tool_calling": False,
            "streaming": False,
            "structured_output": False,
            "vision": False,
            "max_context_tokens": capabilities.max_context_tokens,
        },
        enabled=True,
    )
    credential = SimpleNamespace(
        id=credential_id,
        workspace_id=workspace_id,
        provider=route.provider,
        base_url=(base_url if credential_base_url is ... else credential_base_url),
        enabled=True,
    )
    request = ModelRequest(
        messages=(ModelMessage(role="user", content="synthetic request"),),
        request_mode=AliyunDeepSeekRequestMode.NON_THINKING,
    )
    validator = _InputBoundValidator(admitted=validator_admitted)
    session = _Session(snapshot=snapshot, profile=profile_row, credential=credential)
    registry = BillingAttestationRegistry.server_supplied(
        (attestation,),
        validators={validator.identity: validator},
    )
    source = InteractiveCaseAttemptPriceSource(
        registry,
        attestation_identity=attestation.identity,
        frozen_route=route,
    )
    return _Fixture(
        route=route,
        evidence=evidence,
        snapshot=snapshot,
        attestation=attestation,
        profile=profile,
        profile_row=profile_row,
        credential=credential,
        request=request,
        validator=validator,
        session=session,
        registry=registry,
        source=source,
    )


async def _resolve(
    fixture: _Fixture,
    *,
    profile: ResolvedModelProfile | None = None,
    request: ModelRequest | None = None,
    provider: str | None = None,
    attempt_kind: ModelAttemptKind = ModelAttemptKind.MAIN_MODEL,
):
    return await fixture.source.resolve(
        fixture.session,
        profile=fixture.profile if profile is None else profile,
        request=fixture.request if request is None else request,
        provider=fixture.route.provider if provider is None else provider,
        attempt_kind=attempt_kind,
    )


@pytest.mark.asyncio
async def test_quote_uses_server_bounds_and_exact_route() -> None:
    fixture = _fixture()

    quote = await _resolve(fixture)

    assert fixture.route.base_url == "https://provider.invalid/v1"
    assert fixture.route.effective_endpoint == "https://provider.invalid/v1"
    assert quote is not None
    assert quote.amount == Decimal("0.00001700")
    assert quote.currency == "CNY"
    assert len(quote.basis_identity) <= 256
    assert f"proof={fixture.attestation.identity}" in quote.basis_identity
    assert f"route={fixture.route.identity}" in quote.basis_identity
    assert f"price={fixture.snapshot.id}:{fixture.snapshot.content_hash}" in quote.basis_identity
    assert fixture.validator.calls == [
        (fixture.request, fixture.evidence.max_billable_input_tokens)
    ]


@pytest.mark.asyncio
async def test_quote_rounds_outward_to_eight_decimal_places() -> None:
    fixture = _fixture(
        max_input_tokens=1,
        max_output_tokens=1,
        input_price=Decimal("0.00000001"),
        output_price=Decimal("0.00000003"),
        cached_input_price=None,
    )
    fixture.profile_row.max_tokens = 1
    fixture.profile = replace(fixture.profile, max_tokens=1)

    quote = await _resolve(fixture)

    assert quote is not None
    assert quote.amount == Decimal("0.00000001")


def test_registry_is_empty_by_default() -> None:
    registry = BillingAttestationRegistry.empty()

    assert len(registry) == 0
    assert registry.attestation_ids == ()
    assert registry.get("deepseek") is None


def test_unknown_egress_policy_is_rejected() -> None:
    with pytest.raises(CaseBudgetPricingError) as error:
        FrozenProviderRoute(
            provider="deepseek",
            model="deepseek-synthetic-v1",
            policy_version="synthetic-egress-policy-v1",
            base_url="https://provider.invalid/v1",
        )

    assert error.value.code == "CASE_BUDGET_EGRESS_POLICY_UNSUPPORTED"


@pytest.mark.asyncio
async def test_null_credential_uses_frozen_v1_default() -> None:
    fixture = _fixture(
        provider="deepseek",
        base_url=None,
        credential_base_url=None,
    )

    quote = await _resolve(fixture)

    assert fixture.route.base_url is None
    assert fixture.route.effective_endpoint == "https://api.deepseek.com"
    assert quote is not None


@pytest.mark.asyncio
async def test_explicit_equivalent_of_null_default_is_an_intent_drift() -> None:
    fixture = _fixture(
        provider="deepseek",
        base_url=None,
        credential_base_url="HTTPS://API.DEEPSEEK.COM:443/",
    )

    assert fixture.route.effective_endpoint == "https://api.deepseek.com"
    assert await _resolve(fixture) is None


@pytest.mark.asyncio
async def test_null_credential_default_route_drift_returns_none() -> None:
    fixture = _fixture(
        provider="deepseek",
        base_url=None,
        credential_base_url="https://api.deepseek.com/v1",
    )

    assert await _resolve(fixture) is None


@pytest.mark.asyncio
async def test_trailing_slash_and_default_port_are_normalized_before_comparison() -> None:
    fixture = _fixture(
        base_url="HTTPS://PROVIDER.INVALID:443/v1/",
        credential_base_url="https://provider.invalid/v1",
    )

    quote = await _resolve(fixture)
    round_trip = FrozenProviderRoute(
        provider=fixture.route.provider,
        model=fixture.route.model,
        policy_version=fixture.route.policy_version,
        base_url=fixture.route.effective_endpoint,
    )

    assert fixture.route.base_url == "https://provider.invalid/v1"
    assert round_trip.effective_endpoint == fixture.route.effective_endpoint
    assert quote is not None


@pytest.mark.asyncio
async def test_missing_attestation_or_validator_returns_none() -> None:
    fixture = _fixture()
    empty_source = InteractiveCaseAttemptPriceSource(
        BillingAttestationRegistry.empty(),
        attestation_identity=fixture.attestation.identity,
        frozen_route=fixture.route,
    )
    no_validator_registry = BillingAttestationRegistry((fixture.attestation,))
    no_validator_source = InteractiveCaseAttemptPriceSource(
        no_validator_registry,
        attestation_identity=fixture.attestation.identity,
        frozen_route=fixture.route,
    )

    assert (
        await empty_source.resolve(
            fixture.session,
            profile=fixture.profile,
            request=fixture.request,
            provider=fixture.route.provider,
            attempt_kind=ModelAttemptKind.MAIN_MODEL,
        )
        is None
    )
    assert (
        await no_validator_source.resolve(
            fixture.session,
            profile=fixture.profile,
            request=fixture.request,
            provider=fixture.route.provider,
            attempt_kind=ModelAttemptKind.MAIN_MODEL,
        )
        is None
    )


@pytest.mark.asyncio
async def test_constructor_bound_route_mismatch_returns_none() -> None:
    fixture = _fixture()
    different_route = replace(
        fixture.route,
        base_url="https://other-provider.invalid/v1",
    )
    source = InteractiveCaseAttemptPriceSource(
        fixture.registry,
        attestation_identity=fixture.attestation.identity,
        frozen_route=different_route,
    )

    quote = await source.resolve(
        fixture.session,
        profile=fixture.profile,
        request=fixture.request,
        provider=fixture.route.provider,
        attempt_kind=ModelAttemptKind.MAIN_MODEL,
    )

    assert quote is None


@pytest.mark.asyncio
async def test_current_credential_route_must_still_match() -> None:
    fixture = _fixture()
    fixture.credential.base_url = "https://different-endpoint.invalid/v1"

    assert await _resolve(fixture) is None


@pytest.mark.asyncio
async def test_malformed_current_credential_endpoint_returns_none() -> None:
    fixture = _fixture()
    fixture.credential.base_url = "https://user:secret@provider.invalid/v1"

    assert await _resolve(fixture) is None


@pytest.mark.asyncio
async def test_current_profile_must_still_match() -> None:
    fixture = _fixture()
    fixture.profile_row.enabled = False

    assert await _resolve(fixture) is None


@pytest.mark.asyncio
async def test_snapshot_id_hash_and_rates_must_match_attestation() -> None:
    fixture = _fixture()
    fixture.snapshot.output_price_per_1m = Decimal("4")

    assert await _resolve(fixture) is None


@pytest.mark.asyncio
async def test_profile_input_window_is_not_used_as_billing_bound() -> None:
    fixture = _fixture()
    assert fixture.profile.capabilities.max_context_tokens == 777_777

    quote = await _resolve(fixture)

    assert quote is not None
    assert quote.amount == Decimal("0.00001700")
    assert fixture.validator.calls[0][1] == fixture.evidence.max_billable_input_tokens


@pytest.mark.asyncio
async def test_server_validator_rejection_returns_none() -> None:
    fixture = _fixture(validator_admitted=False)

    assert await _resolve(fixture) is None
    assert len(fixture.validator.calls) == 1


@pytest.mark.asyncio
async def test_truthy_non_boolean_validator_result_returns_none() -> None:
    fixture = _fixture(validator_admitted="accepted")

    assert await _resolve(fixture) is None
    assert len(fixture.validator.calls) == 1


@pytest.mark.asyncio
async def test_malformed_request_profile_and_attempt_kind_fail_closed() -> None:
    fixture = _fixture()

    assert (
        await fixture.source.resolve(
            fixture.session,
            profile=fixture.profile,
            request=object(),  # type: ignore[arg-type]
            provider=fixture.route.provider,
            attempt_kind=ModelAttemptKind.MAIN_MODEL,
        )
        is None
    )
    assert (
        await fixture.source.resolve(
            fixture.session,
            profile=object(),  # type: ignore[arg-type]
            request=fixture.request,
            provider=fixture.route.provider,
            attempt_kind=ModelAttemptKind.MAIN_MODEL,
        )
        is None
    )
    assert (
        await fixture.source.resolve(
            fixture.session,
            profile=fixture.profile,
            request=fixture.request,
            provider=fixture.route.provider,
            attempt_kind="MAIN_MODEL",  # type: ignore[arg-type]
        )
        is None
    )


@pytest.mark.asyncio
async def test_profile_output_cap_above_evidence_returns_none() -> None:
    fixture = _fixture()

    assert await _resolve(fixture, profile=replace(fixture.profile, max_tokens=4)) is None


@pytest.mark.asyncio
async def test_request_context_requirement_above_evidence_returns_none() -> None:
    fixture = _fixture()
    request = replace(
        fixture.request,
        required_capabilities=CapabilityRequirements(max_context_tokens=5),
    )

    assert await _resolve(fixture, request=request) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "attempt_kind",
    [ModelAttemptKind.FALLBACK, ModelAttemptKind.RETRY],
)
async def test_fallback_and_retry_attempts_return_none(
    attempt_kind: ModelAttemptKind,
) -> None:
    fixture = _fixture()

    assert await _resolve(fixture, attempt_kind=attempt_kind) is None


@pytest.mark.asyncio
async def test_frozen_fallback_profile_or_retry_policy_returns_none() -> None:
    fixture = _fixture()
    fallback_profile = replace(fixture.profile, fallback_profile_id=uuid4())
    retry_request = replace(
        fixture.request,
        retry_policy=RetryPolicy(max_attempts=2),
    )

    assert await _resolve(fixture, profile=fallback_profile) is None
    assert await _resolve(fixture, request=retry_request) is None


@pytest.mark.asyncio
async def test_unsupported_request_mode_and_stream_operation_return_none() -> None:
    fixture = _fixture()
    different_mode = replace(
        fixture.request,
        request_mode=AliyunDeepSeekRequestMode.PROVIDER_DEFAULT,
    )

    stream_quote = await fixture.source.resolve_for_operation(
        fixture.session,
        profile=fixture.profile,
        request=fixture.request,
        provider=fixture.route.provider,
        attempt_kind=ModelAttemptKind.MAIN_MODEL,
        operation="stream",
    )

    assert await _resolve(fixture, request=different_mode) is None
    assert stream_quote is None


def test_stream_source_cannot_be_constructed() -> None:
    fixture = _fixture()

    with pytest.raises(CaseBudgetPricingError) as error:
        InteractiveCaseAttemptPriceSource(
            fixture.registry,
            attestation_identity=fixture.attestation.identity,
            frozen_route=fixture.route,
            operation="stream",  # type: ignore[arg-type]
        )

    assert error.value.code == "CASE_BUDGET_OPERATION_UNSUPPORTED"


@pytest.mark.asyncio
async def test_tools_tool_messages_and_vision_requests_return_none() -> None:
    fixture = _fixture()
    tool_request = replace(
        fixture.request,
        tools=(
            ModelToolDefinition(
                name="synthetic-tool",
                parameters={"type": "object", "properties": {}},
            ),
        ),
    )
    tool_message_request = replace(
        fixture.request,
        messages=(
            ModelMessage(
                role="tool",
                content="synthetic tool output",
                tool_call_id="provider-id-must-not-matter",
            ),
        ),
    )
    vision_required = replace(
        fixture.request,
        required_capabilities=CapabilityRequirements(
            required=frozenset({"vision"}),
        ),
    )
    vision_profile = replace(
        fixture.profile,
        capabilities=replace(fixture.profile.capabilities, vision=True),
    )

    assert await _resolve(fixture, request=tool_request) is None
    assert await _resolve(fixture, request=tool_message_request) is None
    assert await _resolve(fixture, request=vision_required) is None
    assert await _resolve(fixture, profile=vision_profile) is None


@pytest.mark.parametrize(
    "extra_fee",
    [
        BillingFeeKind.CACHE_WRITE,
        BillingFeeKind.IMAGE_INPUT,
        BillingFeeKind.TOOL_OR_EXTERNAL,
        BillingFeeKind.OTHER,
    ],
)
def test_extra_fee_coverage_is_a_typed_contract_error(extra_fee: BillingFeeKind) -> None:
    with pytest.raises(CaseBudgetPricingError) as error:
        RequestAdmissibilityEvidence(
            max_billable_input_tokens=10,
            max_billable_output_tokens=5,
            fee_coverage=SUPPORTED_FEE_COVERAGE | {extra_fee},
            request_mode=AliyunDeepSeekRequestMode.NON_THINKING,
            input_upper_bound_validator_id=_InputBoundValidator.identity,
        )

    assert error.value.code == "CASE_BUDGET_UNSUPPORTED_FEE"


@pytest.mark.parametrize(
    "invalid_rate",
    [
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-1"),
        Decimal("0.000000001"),
    ],
)
def test_invalid_rate_is_a_typed_contract_error(invalid_rate: Decimal) -> None:
    fixture = _fixture()

    with pytest.raises(CaseBudgetPricingError) as error:
        replace(fixture.attestation, input_price_per_1m=invalid_rate)

    assert error.value.code in {
        "CASE_BUDGET_PRICE_INVALID",
        "CASE_BUDGET_PRECISION_INVALID",
    }


def test_non_cny_attestation_is_a_typed_contract_error() -> None:
    fixture = _fixture()

    with pytest.raises(CaseBudgetPricingError) as error:
        replace(fixture.attestation, currency="USD")

    assert error.value.code == "CASE_BUDGET_CURRENCY_UNSUPPORTED"


@pytest.mark.asyncio
async def test_zero_priced_request_has_no_positive_upper_bound() -> None:
    fixture = _fixture(
        input_price=Decimal("0"),
        output_price=Decimal("0"),
        cached_input_price=None,
    )

    assert await _resolve(fixture) is None
