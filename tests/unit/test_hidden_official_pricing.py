from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from packages.evaluation.pricing import (
    FALLBACK_PRICING_REQUEST_KEY,
    FROZEN_PRICING_CHAIN_KEY,
    ModelCallAttempt,
    PricingChainError,
    build_frozen_pricing_chain,
    consume_fallback_pricing_request,
    price_aggregate_usage,
    price_model_attempts,
    pricing_snapshot_ids_from_metadata,
    resolve_case_pricing,
    validate_frozen_pricing_chain,
)
from packages.evaluation.reproducibility import pricing_snapshot_content_hash
from packages.evaluation.runner import (
    _model_attempt,
    _model_attempts_from_steps,
    _pricing_receipt_complete,
)
from packages.model_gateway.contracts import (
    ModelCapabilities,
    ResolvedModelExecutionPlan,
    ResolvedModelExecutionProfile,
    RetryPolicy,
)


def _profile(*, model: str, provider: str = "fake") -> ResolvedModelExecutionProfile:
    return ResolvedModelExecutionProfile(
        id=uuid4(),
        workspace_id=uuid4(),
        provider_credential_id=uuid4(),
        provider=provider,
        model=model,
        temperature=Decimal("0"),
        max_tokens=256,
        timeout_seconds=Decimal("30"),
        capabilities=ModelCapabilities(),
    )


def _plan(
    *,
    fallback: bool = True,
    max_attempts: int = 1,
) -> ResolvedModelExecutionPlan:
    primary = _profile(model="primary")
    fallback_profile = replace(
        _profile(model="fallback"),
        workspace_id=primary.workspace_id,
    )
    return ResolvedModelExecutionPlan(
        primary=primary,
        fallbacks=(fallback_profile,) if fallback else (),
        retry_policy=RetryPolicy(max_attempts=max_attempts),
    )


def _pricing(
    profile: ResolvedModelExecutionProfile,
    *,
    input_price: str,
    output_price: str,
    currency: str = "USD",
) -> SimpleNamespace:
    snapshot = SimpleNamespace(
        id=uuid4(),
        workspace_id=profile.workspace_id,
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


def test_primary_only_aggregate_keeps_exact_existing_cost() -> None:
    plan = _plan(fallback=False)
    snapshot = _pricing(plan.primary, input_price="0.10", output_price="0.20")

    result = price_aggregate_usage(
        snapshot,
        input_tokens=100,
        output_tokens=50,
        cached_tokens=0,
    )

    assert result.amount == Decimal("0.00002000")
    assert result.currency == "USD"
    assert result.unavailable_reason is None


@pytest.mark.parametrize(
    ("max_attempts", "aggregate_complete", "expected_status"),
    [
        (1, False, "AGGREGATE_BILLING_INCOMPLETE"),
        (2, True, "RETRY_ATTEMPTS_UNPROVEN"),
    ],
)
def test_aggregate_cost_requires_one_attempt_and_complete_multi_run_receipt(
    max_attempts: int,
    aggregate_complete: bool,
    expected_status: str,
) -> None:
    plan = _plan(fallback=False, max_attempts=max_attempts)
    snapshot = _pricing(plan.primary, input_price="0.10", output_price="0.20")

    result = resolve_case_pricing(
        model_plan=plan,
        snapshots=(snapshot,),
        model_attempts=(),
        model_attempts_complete=False,
        aggregate_billing_complete=aggregate_complete,
        input_tokens=100,
        output_tokens=50,
        cached_tokens=0,
    )

    assert result.amount is None
    assert result.currency is None
    assert result.unavailable_reason == expected_status


def test_complete_primary_only_aggregate_uses_frozen_price_through_central_decision() -> None:
    plan = _plan(fallback=False)
    snapshot = _pricing(plan.primary, input_price="0.10", output_price="0.20")

    result = resolve_case_pricing(
        model_plan=plan,
        snapshots=(snapshot,),
        model_attempts=(),
        model_attempts_complete=False,
        aggregate_billing_complete=True,
        input_tokens=100,
        output_tokens=50,
        cached_tokens=0,
    )

    assert result.amount == Decimal("0.00002000")
    assert result.currency == "USD"
    assert result.unavailable_reason is None


def test_successful_model_step_cannot_prove_failed_retries_were_free() -> None:
    step = SimpleNamespace(
        kind="MODEL",
        status="SUCCEEDED",
        safe_metadata={
            "provider": "fake",
            "model": "primary",
            "usage_source": "generation",
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
            "cached_tokens": 0,
        },
    )
    attempts, _ = _model_attempts_from_steps(
        [step],
        require_complete_receipt=False,
    )
    model_attempts_complete = _pricing_receipt_complete(
        [step],
        frozen_fallback_chain=False,
        max_attempts=2,
    )
    plan = _plan(fallback=False, max_attempts=2)
    snapshot = _pricing(plan.primary, input_price="0.10", output_price="0.20")

    result = resolve_case_pricing(
        model_plan=plan,
        snapshots=(snapshot,),
        model_attempts=tuple(_model_attempt(attempt) for attempt in attempts),
        model_attempts_complete=model_attempts_complete,
        aggregate_billing_complete=model_attempts_complete,
        input_tokens=100,
        output_tokens=50,
        cached_tokens=0,
    )

    assert not model_attempts_complete
    assert result.amount is None
    assert result.unavailable_reason == "ATTEMPT_COLLECTION_INCOMPLETE"


def test_complete_success_only_receipt_cannot_cover_retry_policy() -> None:
    plan = _plan(fallback=False, max_attempts=2)
    snapshot = _pricing(plan.primary, input_price="0.10", output_price="0.20")
    attempt = ModelCallAttempt(
        source="generation",
        provider=plan.primary.provider,
        model=plan.primary.model,
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
        cached_tokens=0,
        attempt_status="SUCCEEDED",
    )

    result = resolve_case_pricing(
        model_plan=plan,
        snapshots=(snapshot,),
        model_attempts=(attempt,),
        model_attempts_complete=True,
        aggregate_billing_complete=True,
        input_tokens=100,
        output_tokens=50,
        cached_tokens=0,
    )

    assert result.amount is None
    assert result.currency is None
    assert result.unavailable_reason == "RETRY_ATTEMPTS_UNPROVEN"


def test_primary_only_metadata_and_hash_payload_remain_unchanged() -> None:
    plan = _plan(fallback=False)
    metadata = {"temperature_label": "baseline"}
    snapshot = _pricing(plan.primary, input_price="0.10", output_price="0.20")
    assert build_frozen_pricing_chain(metadata, plan, (snapshot,)) == metadata


def test_fallback_chain_is_ordered_and_service_generated() -> None:
    plan = _plan()
    snapshots = (
        _pricing(plan.primary, input_price="1", output_price="2"),
        _pricing(plan.fallbacks[0], input_price="3", output_price="4"),
    )
    metadata = {
        "temperature_label": "fallback",
        FALLBACK_PRICING_REQUEST_KEY: [str(snapshots[1].id)],
    }

    without_request, ids = consume_fallback_pricing_request(
        metadata,
        plan,
        primary_snapshot_id=snapshots[0].id,
    )
    frozen = build_frozen_pricing_chain(without_request, plan, snapshots)

    assert ids == (snapshots[1].id,)
    assert FALLBACK_PRICING_REQUEST_KEY not in frozen
    assert FROZEN_PRICING_CHAIN_KEY in frozen
    validate_frozen_pricing_chain(
        frozen,
        plan,
        snapshots,
        primary_snapshot_id=snapshots[0].id,
        primary_snapshot_hash=snapshots[0].content_hash,
    )


@pytest.mark.parametrize(
    "snapshots",
    [
        lambda plan: (_pricing(plan.primary, input_price="1", output_price="2"),),
        lambda plan: (
            _pricing(plan.primary, input_price="1", output_price="2"),
            _pricing(plan.primary, input_price="3", output_price="4"),
        ),
    ],
)
def test_fallback_chain_rejects_missing_duplicate_or_mismatched_prices(snapshots) -> None:
    plan = _plan()
    with pytest.raises(PricingChainError):
        build_frozen_pricing_chain({}, plan, snapshots(plan))


def test_fallback_attempts_use_actual_models_and_exclude_primary_rate() -> None:
    plan = _plan()
    snapshots = (
        _pricing(plan.primary, input_price="10", output_price="30"),
        _pricing(plan.fallbacks[0], input_price="2", output_price="4"),
    )
    attempts = (
        ModelCallAttempt(
            source="generation",
            provider=plan.fallbacks[0].provider,
            model=plan.fallbacks[0].model,
            input_tokens=1_000_000,
            output_tokens=500_000,
            cached_tokens=0,
            attempt_status="SUCCEEDED",
        ),
    )

    result = price_model_attempts(
        attempts,
        plan,
        snapshots,
        collection_complete=True,
    )

    assert result.amount == Decimal("4.00000000")
    assert result.currency == "USD"


@pytest.mark.parametrize(
    ("attempt", "snapshots_factory", "reason"),
    [
        (
            ModelCallAttempt(
                source="generation",
                provider="fake",
                model="fallback",
                input_tokens=None,
                output_tokens=None,
                attempt_status="SUCCEEDED",
            ),
            lambda plan: (
                _pricing(plan.primary, input_price="10", output_price="30"),
                _pricing(plan.fallbacks[0], input_price="2", output_price="4"),
            ),
            "ATTEMPT_USAGE_UNKNOWN",
        ),
        (
            ModelCallAttempt(
                source="onyx_selector",
                provider="fake",
                model="fallback",
                input_tokens=10,
                output_tokens=5,
                attempt_status="SUCCEEDED",
                cost_status="UNKNOWN",
            ),
            lambda plan: (
                _pricing(plan.primary, input_price="10", output_price="30"),
                _pricing(plan.fallbacks[0], input_price="2", output_price="4"),
            ),
            "AUXILIARY_MODEL_UNPRICED",
        ),
        (
            ModelCallAttempt(
                source="generation",
                provider="fake",
                model="unpublished-model",
                input_tokens=10,
                output_tokens=5,
                attempt_status="SUCCEEDED",
            ),
            lambda plan: (
                _pricing(plan.primary, input_price="10", output_price="30"),
                _pricing(plan.fallbacks[0], input_price="2", output_price="4"),
            ),
            "MODEL_PRICE_UNMATCHED",
        ),
        (
            ModelCallAttempt(
                source="generation",
                provider="fake",
                model="fallback",
                input_tokens=10,
                output_tokens=5,
                attempt_status="TIMEOUT",
            ),
            lambda plan: (
                _pricing(plan.primary, input_price="10", output_price="30"),
                _pricing(plan.fallbacks[0], input_price="2", output_price="4"),
            ),
            "MODEL_ATTEMPT_FAILED",
        ),
    ],
)
def test_unknown_attempts_make_cost_unavailable(attempt, snapshots_factory, reason) -> None:
    plan = _plan()
    result = price_model_attempts(
        (attempt,),
        plan,
        snapshots_factory(plan),
        collection_complete=True,
    )
    assert result.amount is None
    assert result.currency is None
    assert result.unavailable_reason == reason


def test_mixed_currencies_make_cost_unavailable() -> None:
    plan = _plan()
    snapshots = (
        _pricing(plan.primary, input_price="10", output_price="30", currency="USD"),
        _pricing(plan.fallbacks[0], input_price="2", output_price="4", currency="EUR"),
    )
    attempts = (
        ModelCallAttempt(
            source="generation",
            provider=plan.primary.provider,
            model=plan.primary.model,
            input_tokens=1,
            output_tokens=1,
            cached_tokens=0,
            attempt_status="SUCCEEDED",
        ),
        ModelCallAttempt(
            source="generation",
            provider=plan.fallbacks[0].provider,
            model=plan.fallbacks[0].model,
            input_tokens=1,
            output_tokens=1,
            cached_tokens=0,
            attempt_status="SUCCEEDED",
        ),
    )
    result = price_model_attempts(
        attempts,
        plan,
        snapshots,
        collection_complete=True,
    )
    assert result.amount is None
    assert result.currency is None
    assert result.unavailable_reason == "MIXED_CURRENCIES"


def test_pricing_projection_order_drift_is_rejected() -> None:
    plan = _plan()
    snapshots = (
        _pricing(plan.primary, input_price="1", output_price="2"),
        _pricing(plan.fallbacks[0], input_price="3", output_price="4"),
    )
    frozen = build_frozen_pricing_chain({}, plan, snapshots)
    models = frozen[FROZEN_PRICING_CHAIN_KEY]["models"]
    frozen[FROZEN_PRICING_CHAIN_KEY]["models"] = list(reversed(models))

    with pytest.raises(PricingChainError):
        validate_frozen_pricing_chain(
            frozen,
            plan,
            snapshots,
            primary_snapshot_id=snapshots[0].id,
            primary_snapshot_hash=snapshots[0].content_hash,
        )
    with pytest.raises(PricingChainError):
        pricing_snapshot_ids_from_metadata(
            frozen,
            model_count=2,
            primary_snapshot_id=snapshots[0].id,
        )


def test_run_steps_without_receipt_cannot_claim_complete_attempt_collection() -> None:
    fallback_step = SimpleNamespace(
        kind="MODEL",
        status="SUCCEEDED",
        safe_metadata={
            "provider": "fake",
            "model": "fallback",
            "usage_source": "generation",
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "cached_tokens": 0,
        },
    )
    selector_step = SimpleNamespace(
        kind="AUXILIARY_MODEL",
        status="SUCCEEDED",
        safe_metadata={
            "provider": "fake",
            "model": "selector",
            "usage_source": "onyx_selector",
            "attempt_status": "SUCCEEDED",
            "cost_status": "UNKNOWN",
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
        },
    )

    attempts, _ = _model_attempts_from_steps(
        [fallback_step, selector_step],
        require_complete_receipt=True,
    )
    assert [attempt["model"] for attempt in attempts] == ["fallback", "selector"]
    assert not _pricing_receipt_complete(
        [fallback_step],
        frozen_fallback_chain=True,
        max_attempts=1,
    )
    assert not _pricing_receipt_complete(
        [fallback_step, selector_step],
        frozen_fallback_chain=False,
        max_attempts=1,
    )
    assert not _pricing_receipt_complete(
        [fallback_step],
        frozen_fallback_chain=False,
        max_attempts=2,
    )
    assert not _pricing_receipt_complete(
        [],
        frozen_fallback_chain=False,
        max_attempts=2,
    )
