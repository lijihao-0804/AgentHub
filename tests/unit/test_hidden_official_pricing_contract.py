from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from packages.core.errors.exceptions import AgentHubError
from packages.evaluation.experiments import ExperimentService
from packages.evaluation.pricing import (
    FALLBACK_PRICING_REQUEST_KEY,
    FROZEN_PRICING_CHAIN_KEY,
    PricingChainError,
    build_frozen_pricing_chain,
    consume_fallback_pricing_request,
    pricing_snapshot_ids_from_metadata,
)
from packages.evaluation.reproducibility import pricing_snapshot_content_hash
from packages.model_gateway.contracts import (
    ModelCapabilities,
    ResolvedModelExecutionPlan,
    ResolvedModelExecutionProfile,
    RetryPolicy,
)


def _profile(
    workspace_id,
    *,
    model: str,
) -> ResolvedModelExecutionProfile:
    return ResolvedModelExecutionProfile(
        id=uuid4(),
        workspace_id=workspace_id,
        provider_credential_id=uuid4(),
        provider="fake",
        model=model,
        temperature=Decimal("0"),
        max_tokens=256,
        timeout_seconds=Decimal("30"),
        capabilities=ModelCapabilities(),
    )


def _plan(*, fallback: bool = True) -> ResolvedModelExecutionPlan:
    workspace_id = uuid4()
    primary = _profile(workspace_id, model="primary")
    return ResolvedModelExecutionPlan(
        primary=primary,
        fallbacks=(_profile(workspace_id, model="fallback"),) if fallback else (),
        retry_policy=RetryPolicy(max_attempts=1),
    )


def _pricing(
    profile: ResolvedModelExecutionProfile,
    *,
    workspace_id=None,
    input_price: str = "1",
    output_price: str = "2",
    currency: str = "USD",
) -> SimpleNamespace:
    snapshot = SimpleNamespace(
        id=uuid4(),
        workspace_id=workspace_id or profile.workspace_id,
        name=f"price-{profile.model}",
        provider=profile.provider,
        model=profile.model,
        currency=currency,
        input_price_per_1m=Decimal(input_price),
        output_price_per_1m=Decimal(output_price),
        cached_input_price_per_1m=None,
        effective_at=datetime(2026, 10, 26, tzinfo=UTC),
        source_note="offline fixture",
    )
    snapshot.content_hash = pricing_snapshot_content_hash(snapshot)
    return snapshot


def test_service_generated_chain_does_not_persist_client_request() -> None:
    plan = _plan()
    pricing = (
        _pricing(plan.primary),
        _pricing(plan.fallbacks[0], input_price="3", output_price="4"),
    )
    metadata, fallback_ids = consume_fallback_pricing_request(
        {
            "temperature_label": "fallback",
            FALLBACK_PRICING_REQUEST_KEY: [str(pricing[1].id)],
        },
        plan,
        primary_snapshot_id=pricing[0].id,
    )
    frozen = build_frozen_pricing_chain(metadata, plan, pricing)

    assert fallback_ids == (pricing[1].id,)
    assert FALLBACK_PRICING_REQUEST_KEY not in frozen
    assert frozen["temperature_label"] == "fallback"
    assert [item["pricing_snapshot_id"] for item in frozen[FROZEN_PRICING_CHAIN_KEY]["models"]] == [
        str(item.id) for item in pricing
    ]


@pytest.mark.parametrize(
    "requested",
    [
        None,
        [],
        ["bad"],
        ["00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002"],
    ],
)
def test_fallback_request_must_exactly_cover_each_model(requested: Any) -> None:
    plan = _plan()
    primary_id = uuid4()
    with pytest.raises(PricingChainError):
        consume_fallback_pricing_request(
            {FALLBACK_PRICING_REQUEST_KEY: requested},
            plan,
            primary_snapshot_id=primary_id,
        )


def test_persisted_metadata_cannot_be_client_priced() -> None:
    plan = _plan()
    with pytest.raises(PricingChainError):
        consume_fallback_pricing_request(
            {
                FROZEN_PRICING_CHAIN_KEY: {
                    "schema_version": 1,
                    "models": [{"provider": "fake", "model": "primary"}],
                }
            },
            plan,
            primary_snapshot_id=uuid4(),
        )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda entries: entries.reverse(),
        lambda entries: entries.append(dict(entries[0])),
        lambda entries: entries[1].update({"provider": "other"}),
        lambda entries: entries[1].update({"model": "other"}),
        lambda entries: entries[1].update({"pricing_snapshot_hash": "0" * 64}),
    ],
)
def test_chain_hash_identity_and_order_drift_is_rejected(mutate) -> None:
    plan = _plan()
    pricing = (_pricing(plan.primary), _pricing(plan.fallbacks[0]))
    frozen = build_frozen_pricing_chain({}, plan, pricing)
    mutate(frozen[FROZEN_PRICING_CHAIN_KEY]["models"])

    from packages.evaluation.pricing import validate_frozen_pricing_chain

    with pytest.raises(PricingChainError):
        validate_frozen_pricing_chain(
            frozen,
            plan,
            pricing,
            primary_snapshot_id=pricing[0].id,
            primary_snapshot_hash=pricing[0].content_hash,
        )


def test_foreign_workspace_snapshot_is_rejected() -> None:
    plan = _plan()
    wrong_workspace = replace(plan.fallbacks[0], workspace_id=uuid4())
    pricing = (_pricing(plan.primary), _pricing(wrong_workspace))
    with pytest.raises(PricingChainError):
        build_frozen_pricing_chain({}, plan, pricing)


def test_primary_snapshot_order_is_the_authoritative_header() -> None:
    plan = _plan()
    pricing = (_pricing(plan.primary), _pricing(plan.fallbacks[0]))
    frozen = build_frozen_pricing_chain({}, plan, pricing)
    assert pricing_snapshot_ids_from_metadata(
        frozen,
        model_count=2,
        primary_snapshot_id=pricing[0].id,
    ) == (pricing[0].id, pricing[1].id)


def test_unknown_mapping_errors_are_not_lost() -> None:
    service = ExperimentService()
    with pytest.raises(AgentHubError) as error:
        raise service._pricing_chain_error(
            PricingChainError("missing frozen receipt"),
            status_code=409,
        )
    assert error.value.code == "EVALUATION_PRICING_CHAIN_INVALID"
    assert error.value.message == "missing frozen receipt"
    assert error.value.status_code == 409
