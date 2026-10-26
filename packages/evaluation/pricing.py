"""Frozen pricing-chain contracts for formal experiment accounting."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.evaluation import reproducibility as reproducibility_module
from packages.evaluation.models import PricingSnapshot
from packages.model_gateway.contracts import ResolvedModelExecutionPlan

FALLBACK_PRICING_REQUEST_KEY = "fallback_pricing_snapshot_ids"
FROZEN_PRICING_CHAIN_KEY = "evaluation_pricing_snapshot_chain"
PRICING_CHAIN_SCHEMA_VERSION = 1


class PricingChainError(ValueError):
    """A frozen pricing chain is missing, malformed, or no longer reproducible."""


@dataclass(frozen=True, slots=True)
class ModelCallAttempt:
    """One dispatched model call whose cost must be attributable."""

    source: str | None
    provider: str | None
    model: str | None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cached_tokens: int | None = None
    attempt_status: str | None = None
    cost_status: str | None = None

    def projection(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "provider": self.provider,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cached_tokens": self.cached_tokens,
            "attempt_status": self.attempt_status,
            "cost_status": self.cost_status,
        }


@dataclass(frozen=True, slots=True)
class PricingComputation:
    amount: Decimal | None
    currency: str | None
    unavailable_reason: str | None = None


def resolve_case_pricing(
    *,
    model_plan: ResolvedModelExecutionPlan,
    snapshots: Sequence[PricingSnapshot],
    model_attempts: Sequence[ModelCallAttempt],
    model_attempts_complete: bool,
    aggregate_billing_complete: bool,
    input_tokens: int | None,
    output_tokens: int | None,
    cached_tokens: int | None,
) -> PricingComputation:
    """Choose the only accounting proof available for one case.

    Aggregate usage is accepted only for one frozen primary model with one
    permitted attempt. A successful MODEL step cannot prove that failed retries
    were not billed, and a scenario aggregate can span several Runs.
    """

    if model_plan.retry_policy.max_attempts > 1 and (
        not model_attempts or aggregate_billing_complete or model_attempts_complete
    ):
        return PricingComputation(None, None, "RETRY_ATTEMPTS_UNPROVEN")
    if model_attempts:
        if not model_attempts_complete:
            return PricingComputation(None, None, "ATTEMPT_COLLECTION_INCOMPLETE")
        return price_model_attempts(
            model_attempts,
            model_plan,
            snapshots,
            collection_complete=True,
        )
    if (
        len(model_plan.chain) == 1
        and model_plan.retry_policy.max_attempts == 1
        and aggregate_billing_complete
    ):
        return price_aggregate_usage(
            snapshots[0],
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
        )
    if not aggregate_billing_complete:
        return PricingComputation(None, None, "AGGREGATE_BILLING_INCOMPLETE")
    return PricingComputation(None, None, "MODEL_ATTEMPTS_MISSING")


def consume_fallback_pricing_request(
    metadata: Mapping[str, Any],
    model_plan: ResolvedModelExecutionPlan,
    *,
    primary_snapshot_id: UUID,
) -> tuple[dict[str, Any], tuple[UUID, ...]]:
    """Remove the client request and return fallback snapshot ids in chain order.

    The returned metadata is safe to extend with the service-generated projection.
    Callers must never persist the request key itself.
    """

    normalized = dict(metadata)
    if FROZEN_PRICING_CHAIN_KEY in normalized:
        raise PricingChainError("A client may not supply a frozen pricing chain.")
    requested = normalized.pop(FALLBACK_PRICING_REQUEST_KEY, None)
    fallback_count = len(model_plan.fallbacks)
    if fallback_count == 0:
        if requested not in (None, []):
            raise PricingChainError("Pricing snapshots were supplied for a primary-only model.")
        return normalized, ()
    if requested is None:
        raise PricingChainError(
            "A fallback model chain requires a pricing snapshot for each model."
        )
    if not isinstance(requested, Sequence) or isinstance(requested, (str, bytes)):
        raise PricingChainError("Fallback pricing snapshot ids must be a list.")

    ids: list[UUID] = []
    for raw in requested:
        try:
            ids.append(UUID(str(raw)))
        except (TypeError, ValueError):
            raise PricingChainError("A fallback pricing snapshot id is invalid.") from None
    if len(ids) != fallback_count:
        raise PricingChainError(
            "The fallback pricing chain must contain exactly one snapshot per fallback model."
        )
    if len(set(ids)) != len(ids) or primary_snapshot_id in ids:
        raise PricingChainError("Pricing snapshots must identify each model exactly once.")
    return normalized, tuple(ids)


def build_frozen_pricing_chain(
    metadata: Mapping[str, Any],
    model_plan: ResolvedModelExecutionPlan,
    snapshots: Sequence[PricingSnapshot],
) -> dict[str, Any]:
    """Validate a complete snapshot chain and return canonical variant metadata."""

    normalized = dict(metadata)
    normalized.pop(FALLBACK_PRICING_REQUEST_KEY, None)
    if not model_plan.fallbacks:
        if FROZEN_PRICING_CHAIN_KEY in normalized:
            raise PricingChainError("A primary-only model must not carry a frozen pricing chain.")
        if len(snapshots) != 1:
            raise PricingChainError("A primary-only model requires exactly one pricing snapshot.")
        snapshot = snapshots[0]
        if (
            snapshot.workspace_id != model_plan.primary.workspace_id
            or snapshot.provider != model_plan.primary.provider
            or snapshot.model != model_plan.primary.model
        ):
            raise PricingChainError("The primary pricing snapshot does not match the model.")
        _snapshot_hash(snapshot)
        return normalized

    projection = _build_projection(model_plan, snapshots)
    normalized[FROZEN_PRICING_CHAIN_KEY] = projection
    return normalized


def validate_frozen_pricing_chain(
    metadata: Mapping[str, Any],
    model_plan: ResolvedModelExecutionPlan,
    snapshots: Sequence[PricingSnapshot],
    *,
    primary_snapshot_id: UUID,
    primary_snapshot_hash: str,
) -> None:
    """Verify a persisted chain still matches its frozen profile/snapshot order."""

    if FALLBACK_PRICING_REQUEST_KEY in metadata:
        raise PricingChainError("A persisted variant still contains a pricing request.")
    if not model_plan.fallbacks:
        if FROZEN_PRICING_CHAIN_KEY in metadata:
            raise PricingChainError("A primary-only model carries an unexpected pricing chain.")
        if len(snapshots) != 1:
            raise PricingChainError("A primary-only model requires exactly one pricing snapshot.")
        snapshot = snapshots[0]
        if (
            snapshot.id != primary_snapshot_id
            or _snapshot_hash(snapshot) != primary_snapshot_hash
            or snapshot.provider != model_plan.primary.provider
            or snapshot.model != model_plan.primary.model
        ):
            raise PricingChainError("The primary pricing snapshot no longer matches the model.")
        return

    raw = metadata.get(FROZEN_PRICING_CHAIN_KEY)
    if raw is None:
        raise PricingChainError("The fallback model chain has no frozen pricing chain.")
    entries = _parse_projection(raw)
    frozen = _build_projection(model_plan, snapshots)
    if entries != frozen["models"]:
        raise PricingChainError("The frozen pricing chain changed or is out of chain order.")
    if (
        snapshots[0].id != primary_snapshot_id
        or _snapshot_hash(snapshots[0]) != primary_snapshot_hash
    ):
        raise PricingChainError("The primary pricing binding does not match the chain.")


def pricing_snapshot_ids_from_metadata(
    metadata: Mapping[str, Any],
    *,
    model_count: int,
    primary_snapshot_id: UUID,
) -> tuple[UUID, ...]:
    """Read the persisted snapshot ids without treating metadata prices as trusted."""

    if FALLBACK_PRICING_REQUEST_KEY in metadata:
        raise PricingChainError("A persisted variant still contains a pricing request.")
    raw = metadata.get(FROZEN_PRICING_CHAIN_KEY)
    if model_count == 1:
        if raw is not None:
            raise PricingChainError("A primary-only model carries an unexpected pricing chain.")
        return (primary_snapshot_id,)
    if raw is None:
        raise PricingChainError("The fallback model chain has no frozen pricing chain.")
    entries = _parse_projection(raw)
    if len(entries) != model_count:
        raise PricingChainError("The frozen pricing chain has the wrong model count.")
    ids = tuple(UUID(str(entry["pricing_snapshot_id"])) for entry in entries)
    if ids[0] != primary_snapshot_id or len(set(ids)) != len(ids):
        raise PricingChainError("Frozen pricing snapshots are duplicated or misordered.")
    return ids


async def load_pricing_snapshots(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    snapshot_ids: Sequence[UUID],
) -> tuple[PricingSnapshot, ...]:
    rows = list(
        await session.scalars(
            select(PricingSnapshot).where(
                PricingSnapshot.workspace_id == workspace_id,
                PricingSnapshot.id.in_(tuple(snapshot_ids)),
            )
        )
    )
    by_id = {row.id: row for row in rows}
    if len(by_id) != len(set(snapshot_ids)) or any(
        snapshot_id not in by_id for snapshot_id in snapshot_ids
    ):
        raise PricingChainError("A frozen pricing snapshot is missing or belongs elsewhere.")
    return tuple(by_id[snapshot_id] for snapshot_id in snapshot_ids)


def price_aggregate_usage(
    snapshot: PricingSnapshot,
    *,
    input_tokens: int | None,
    output_tokens: int | None,
    cached_tokens: int | None,
) -> PricingComputation:
    """Price a legacy primary-only aggregate without changing its precision."""

    if input_tokens is None or output_tokens is None or cached_tokens is None:
        return PricingComputation(None, None, "AGGREGATE_USAGE_UNKNOWN")
    try:
        input_value = _token_count(input_tokens)
        output_value = _token_count(output_tokens)
        cached_value = _token_count(cached_tokens)
        if cached_value > input_value:
            return PricingComputation(None, None, "CACHED_USAGE_INVALID")
        cached_price = (
            Decimal(snapshot.cached_input_price_per_1m)
            if snapshot.cached_input_price_per_1m is not None
            else Decimal(snapshot.input_price_per_1m)
        )
        amount = (
            Decimal(input_value - cached_value)
            / Decimal(1_000_000)
            * Decimal(snapshot.input_price_per_1m)
            + Decimal(cached_value) / Decimal(1_000_000) * cached_price
            + Decimal(output_value) / Decimal(1_000_000) * Decimal(snapshot.output_price_per_1m)
        ).quantize(Decimal("0.00000001"))
        currency = _currency(snapshot.currency)
    except (InvalidOperation, TypeError, ValueError):
        return PricingComputation(None, None, "PRICING_SNAPSHOT_INVALID")
    return PricingComputation(amount, currency)


def price_model_attempts(
    attempts: Sequence[ModelCallAttempt],
    model_plan: ResolvedModelExecutionPlan,
    snapshots: Sequence[PricingSnapshot],
    *,
    collection_complete: bool,
) -> PricingComputation:
    """Sum every dispatched model attempt at the frozen rate for its actual model."""

    if not collection_complete:
        return PricingComputation(None, None, "ATTEMPT_COLLECTION_INCOMPLETE")
    if not attempts:
        return PricingComputation(None, None, "MODEL_ATTEMPTS_MISSING")
    if len(snapshots) != len(model_plan.chain):
        return PricingComputation(None, None, "PRICING_CHAIN_INCOMPLETE")

    identities = [(profile.provider, profile.model) for profile in model_plan.chain]
    if len(set(identities)) != len(identities):
        return PricingComputation(None, None, "PRICING_CHAIN_AMBIGUOUS")
    by_identity = {
        (profile.provider, profile.model): snapshot
        for profile, snapshot in zip(model_plan.chain, snapshots, strict=True)
    }
    amounts: list[Decimal] = []
    currencies: set[str] = set()
    for attempt in attempts:
        if attempt.source != "generation":
            return PricingComputation(None, None, "AUXILIARY_MODEL_UNPRICED")
        if attempt.attempt_status != "SUCCEEDED":
            return PricingComputation(None, None, "MODEL_ATTEMPT_FAILED")
        if not attempt.provider or not attempt.model:
            return PricingComputation(None, None, "MODEL_IDENTITY_UNKNOWN")
        snapshot = by_identity.get((attempt.provider, attempt.model))
        if snapshot is None:
            return PricingComputation(None, None, "MODEL_PRICE_UNMATCHED")

        usage = _attempt_usage(attempt, snapshot)
        if usage is None:
            return PricingComputation(None, None, "ATTEMPT_USAGE_UNKNOWN")
        input_value, output_value, cached_value = usage
        try:
            cached_price = (
                Decimal(snapshot.cached_input_price_per_1m)
                if snapshot.cached_input_price_per_1m is not None
                else Decimal(snapshot.input_price_per_1m)
            )
            amounts.append(
                Decimal(input_value - cached_value)
                / Decimal(1_000_000)
                * Decimal(snapshot.input_price_per_1m)
                + Decimal(cached_value) / Decimal(1_000_000) * cached_price
                + Decimal(output_value) / Decimal(1_000_000) * Decimal(snapshot.output_price_per_1m)
            )
            currencies.add(_currency(snapshot.currency))
        except (InvalidOperation, TypeError, ValueError):
            return PricingComputation(None, None, "PRICING_SNAPSHOT_INVALID")
    if len(currencies) != 1:
        return PricingComputation(None, None, "MIXED_CURRENCIES")
    return PricingComputation(
        sum(amounts, Decimal("0")).quantize(Decimal("0.00000001")),
        next(iter(currencies)),
    )


def _attempt_usage(
    attempt: ModelCallAttempt,
    snapshot: PricingSnapshot,
) -> tuple[int, int, int] | None:
    if attempt.input_tokens is None or attempt.output_tokens is None:
        return None
    try:
        input_value = _token_count(attempt.input_tokens)
        output_value = _token_count(attempt.output_tokens)
        if attempt.total_tokens is not None:
            total_value = _token_count(attempt.total_tokens)
            if total_value != input_value + output_value:
                return None
        if attempt.cached_tokens is None:
            if snapshot.cached_input_price_per_1m is not None:
                return None
            cached_value = 0
        else:
            cached_value = _token_count(attempt.cached_tokens)
    except (TypeError, ValueError):
        return None
    if cached_value > input_value:
        return None
    return input_value, output_value, cached_value


def _build_projection(
    model_plan: ResolvedModelExecutionPlan,
    snapshots: Sequence[PricingSnapshot],
) -> dict[str, Any]:
    chain = model_plan.chain
    if len(snapshots) != len(chain):
        raise PricingChainError("The pricing chain must cover every primary and fallback model.")
    profile_ids = [profile.id for profile in chain]
    identities = [(profile.provider, profile.model) for profile in chain]
    if len(set(profile_ids)) != len(profile_ids) or len(set(identities)) != len(identities):
        raise PricingChainError("Model profile ids and provider/model identities must be unique.")

    models: list[dict[str, str]] = []
    for profile, snapshot in zip(chain, snapshots, strict=True):
        snapshot_hash = _snapshot_hash(snapshot)
        if (
            snapshot.workspace_id != profile.workspace_id
            or snapshot.provider != profile.provider
            or snapshot.model != profile.model
        ):
            raise PricingChainError("A pricing snapshot does not match its frozen model profile.")
        models.append(
            {
                "profile_id": str(profile.id),
                "provider": profile.provider,
                "model": profile.model,
                "pricing_snapshot_id": str(snapshot.id),
                "pricing_snapshot_hash": snapshot_hash,
            }
        )
    return {"schema_version": PRICING_CHAIN_SCHEMA_VERSION, "models": models}


def _parse_projection(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, Mapping) or set(raw) != {"schema_version", "models"}:
        raise PricingChainError("The frozen pricing chain shape is invalid.")
    if raw.get("schema_version") != PRICING_CHAIN_SCHEMA_VERSION:
        raise PricingChainError("The frozen pricing chain version is unsupported.")
    models = raw.get("models")
    if not isinstance(models, list) or not models:
        raise PricingChainError("The frozen pricing chain has no models.")
    required = {
        "profile_id",
        "provider",
        "model",
        "pricing_snapshot_id",
        "pricing_snapshot_hash",
    }
    normalized: list[dict[str, str]] = []
    for entry in models:
        if not isinstance(entry, Mapping) or set(entry) != required:
            raise PricingChainError("A frozen pricing-chain entry is malformed.")
        if any(not isinstance(entry[key], str) or not entry[key] for key in required):
            raise PricingChainError("A frozen pricing-chain entry has empty identity fields.")
        try:
            UUID(entry["profile_id"])
            UUID(entry["pricing_snapshot_id"])
        except (TypeError, ValueError):
            raise PricingChainError("A frozen pricing-chain id is invalid.") from None
        normalized.append({key: str(entry[key]) for key in required})
    if len({entry["profile_id"] for entry in normalized}) != len(normalized):
        raise PricingChainError("The frozen pricing chain repeats a model profile.")
    identities = {(entry["provider"], entry["model"]) for entry in normalized}
    snapshot_ids = {entry["pricing_snapshot_id"] for entry in normalized}
    if len(identities) != len(normalized) or len(snapshot_ids) != len(normalized):
        raise PricingChainError("The frozen pricing chain contains a duplicate model or price.")
    return normalized


def _snapshot_hash(snapshot: PricingSnapshot) -> str:
    actual = reproducibility_module.pricing_snapshot_content_hash(snapshot)
    if not isinstance(snapshot.content_hash, str) or actual != snapshot.content_hash:
        raise PricingChainError("A frozen pricing snapshot content hash is invalid.")
    return actual


def _token_count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError
    return value


def _currency(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 3 or not value.isalpha():
        raise ValueError
    return value.upper()


__all__ = [
    "FALLBACK_PRICING_REQUEST_KEY",
    "FROZEN_PRICING_CHAIN_KEY",
    "ModelCallAttempt",
    "PRICING_CHAIN_SCHEMA_VERSION",
    "PricingChainError",
    "PricingComputation",
    "build_frozen_pricing_chain",
    "consume_fallback_pricing_request",
    "load_pricing_snapshots",
    "price_aggregate_usage",
    "price_model_attempts",
    "pricing_snapshot_ids_from_metadata",
    "resolve_case_pricing",
    "validate_frozen_pricing_chain",
]
