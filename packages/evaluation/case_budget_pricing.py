"""Route-pinned CNY upper bounds for interactive case model attempts.

The amount returned by this module is a conservative local ledger hold.  It is
not a provider invoice, a billed amount, or a guarantee that a provider cannot
charge outside the frozen fee coverage.  An ``UNKNOWN_OUTCOME`` still requires
provider-bill reconciliation.

Trust is supplied by composition: only a server bootstrap may populate
``BillingAttestationRegistry``.  The registry starts empty and deliberately does
not infer trusted routes for named providers.  Profile capabilities, estimator
counts, and response usage never establish a hard billing bound.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Literal, Protocol, Self
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.canonical.json_hash import canonical_json_hash
from packages.evaluation.models import PricingSnapshot
from packages.evaluation.reproducibility import pricing_snapshot_content_hash
from packages.model_gateway.capabilities import validate_capabilities
from packages.model_gateway.contracts import (
    PROVIDER_EGRESS_POLICY_VERSION,
    AliyunDeepSeekRequestMode,
    CapabilityRequirements,
    ModelRequest,
    ProviderEgress,
)
from packages.model_gateway.egress import (
    ProviderEgressError,
    effective_provider_base_url,
    normalize_provider_base_url,
)
from packages.model_gateway.models import ModelProfile, ProviderCredential
from packages.model_gateway.profile_resolution import ResolvedModelProfile
from packages.model_gateway.run_budget_adapter import (
    AttemptCostUpperBound,
    ModelAttemptKind,
)

INTERACTIVE_CASE_BUDGET_POLICY = "interactive_case_budget_v1"
_BASIS_IDENTITY_VERSION = "icb1"
_MONEY_SCALE = 8
_RATE_SCALE = 10**_MONEY_SCALE
_TOKENS_PER_PRICE_UNIT = 1_000_000
_MAX_LEDGER_AMOUNT = Decimal("999999999999.99999999")
_HASH_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class CaseBudgetPricingError(ValueError):
    """A safe, typed evidence-contract failure raised before dispatch."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class BillingFeeKind(StrEnum):
    """A fee class that request evidence either covers exactly or excludes."""

    INPUT_TOKENS = "INPUT_TOKENS"
    OUTPUT_TOKENS = "OUTPUT_TOKENS"
    CACHE_WRITE = "CACHE_WRITE"
    IMAGE_INPUT = "IMAGE_INPUT"
    TOOL_OR_EXTERNAL = "TOOL_OR_EXTERNAL"
    OTHER = "OTHER"


SUPPORTED_FEE_COVERAGE = frozenset(
    {
        BillingFeeKind.INPUT_TOKENS,
        BillingFeeKind.OUTPUT_TOKENS,
    }
)


@dataclass(frozen=True, slots=True)
class FrozenProviderRoute:
    """The exact v1 route bound to one case attempt price source.

    ``base_url`` retains the normalized frozen credential intent, including
    ``None`` for the policy-owned default. ``effective_endpoint`` is always the
    normalized, concrete URL selected by ``provider_egress_v1``.
    """

    provider: str
    model: str
    policy_version: str
    base_url: str | None
    effective_endpoint: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _required_text(self.provider, "provider"))
        object.__setattr__(self, "model", _required_text(self.model, "model"))
        if self.policy_version != PROVIDER_EGRESS_POLICY_VERSION:
            raise CaseBudgetPricingError(
                "CASE_BUDGET_EGRESS_POLICY_UNSUPPORTED",
                "Only the known provider egress policy is supported.",
            )
        try:
            provider_egress = ProviderEgress(
                policy_version=self.policy_version,
                base_url=self.base_url,
            )
            normalized_base_url = normalize_provider_base_url(provider_egress.base_url)
            effective_endpoint = effective_provider_base_url(
                self.provider,
                normalized_base_url,
            )
        except (ProviderEgressError, TypeError, ValueError):
            raise CaseBudgetPricingError(
                "CASE_BUDGET_ROUTE_INVALID",
                "The frozen provider route is invalid.",
            ) from None
        if not isinstance(effective_endpoint, str) or not effective_endpoint:
            raise CaseBudgetPricingError(
                "CASE_BUDGET_ROUTE_INVALID",
                "The frozen provider route has no effective endpoint.",
            )
        object.__setattr__(self, "policy_version", provider_egress.policy_version)
        object.__setattr__(self, "base_url", normalized_base_url)
        object.__setattr__(self, "effective_endpoint", effective_endpoint)

    @classmethod
    def from_provider_egress(
        cls,
        *,
        provider: str,
        model: str,
        provider_egress: ProviderEgress,
    ) -> Self:
        """Build from the frozen provider egress contract."""

        if not isinstance(provider_egress, ProviderEgress):
            raise CaseBudgetPricingError(
                "CASE_BUDGET_ROUTE_INVALID",
                "The frozen provider egress contract is invalid.",
            )
        return cls(
            provider=provider,
            model=model,
            policy_version=provider_egress.policy_version,
            base_url=provider_egress.base_url,
        )

    @property
    def identity(self) -> str:
        """Return the canonical route identity used by the reservation basis."""

        return canonical_json_hash(
            {
                "schema_version": 1,
                "provider": self.provider,
                "model": self.model,
                "policy_version": self.policy_version,
                "base_url": self.base_url,
                "effective_endpoint": self.effective_endpoint,
            }
        )


@dataclass(frozen=True, slots=True)
class RequestAdmissibilityEvidence:
    """Server evidence for the billable request envelope.

    ``fee_coverage`` must equal ``SUPPORTED_FEE_COVERAGE``.  A route with cache
    write, image, tool/external, request, or otherwise unknown fees therefore
    cannot produce this evidence.
    """

    max_billable_input_tokens: int
    max_billable_output_tokens: int
    fee_coverage: frozenset[BillingFeeKind]
    request_mode: AliyunDeepSeekRequestMode
    input_upper_bound_validator_id: str

    def __post_init__(self) -> None:
        _positive_integer(self.max_billable_input_tokens, "max_billable_input_tokens")
        _positive_integer(self.max_billable_output_tokens, "max_billable_output_tokens")
        coverage = frozenset(self.fee_coverage)
        if any(not isinstance(item, BillingFeeKind) for item in coverage):
            raise CaseBudgetPricingError(
                "CASE_BUDGET_FEE_COVERAGE_INVALID",
                "Fee coverage contains an unknown fee class.",
            )
        if coverage != SUPPORTED_FEE_COVERAGE:
            raise CaseBudgetPricingError(
                "CASE_BUDGET_UNSUPPORTED_FEE",
                "Only ordinary input and output token fees are supported.",
            )
        if not isinstance(self.request_mode, AliyunDeepSeekRequestMode):
            raise CaseBudgetPricingError(
                "CASE_BUDGET_REQUEST_MODE_INVALID",
                "Request mode evidence is invalid.",
            )
        validator_id = _required_text(
            self.input_upper_bound_validator_id,
            "input_upper_bound_validator_id",
        )
        object.__setattr__(self, "fee_coverage", coverage)
        object.__setattr__(self, "input_upper_bound_validator_id", validator_id)


@dataclass(frozen=True, slots=True)
class BillingAttestation:
    """An immutable server-attested route and price projection."""

    identity: str
    workspace_id: UUID
    route: FrozenProviderRoute
    pricing_snapshot_id: UUID
    pricing_snapshot_hash: str
    currency: str
    input_price_per_1m: Decimal
    output_price_per_1m: Decimal
    cached_input_price_per_1m: Decimal | None
    request_evidence: RequestAdmissibilityEvidence

    def __post_init__(self) -> None:
        identity = _required_text(self.identity, "identity", maximum=64)
        if not isinstance(self.workspace_id, UUID):
            raise CaseBudgetPricingError(
                "CASE_BUDGET_ATTESTATION_INVALID",
                "The attestation workspace id is invalid.",
            )
        if not isinstance(self.route, FrozenProviderRoute):
            raise CaseBudgetPricingError(
                "CASE_BUDGET_ATTESTATION_INVALID",
                "The attestation route is invalid.",
            )
        if not isinstance(self.pricing_snapshot_id, UUID):
            raise CaseBudgetPricingError(
                "CASE_BUDGET_ATTESTATION_INVALID",
                "The pricing snapshot id is invalid.",
            )
        snapshot_hash = _required_text(
            self.pricing_snapshot_hash,
            "pricing_snapshot_hash",
        )
        if _HASH_PATTERN.fullmatch(snapshot_hash) is None:
            raise CaseBudgetPricingError(
                "CASE_BUDGET_ATTESTATION_INVALID",
                "The pricing snapshot hash is invalid.",
            )
        currency = _required_text(self.currency, "currency")
        if currency != "CNY":
            raise CaseBudgetPricingError(
                "CASE_BUDGET_CURRENCY_UNSUPPORTED",
                "Interactive case pricing must use CNY.",
            )
        input_price = _rate(self.input_price_per_1m, "input_price_per_1m")
        output_price = _rate(self.output_price_per_1m, "output_price_per_1m")
        cached_price = (
            None
            if self.cached_input_price_per_1m is None
            else _rate(self.cached_input_price_per_1m, "cached_input_price_per_1m")
        )
        if not isinstance(self.request_evidence, RequestAdmissibilityEvidence):
            raise CaseBudgetPricingError(
                "CASE_BUDGET_ATTESTATION_INVALID",
                "Request admissibility evidence is invalid.",
            )
        object.__setattr__(self, "identity", identity)
        object.__setattr__(self, "pricing_snapshot_hash", snapshot_hash)
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "input_price_per_1m", input_price)
        object.__setattr__(self, "output_price_per_1m", output_price)
        object.__setattr__(self, "cached_input_price_per_1m", cached_price)
        if len(self.basis_identity) > 256:
            raise CaseBudgetPricingError(
                "CASE_BUDGET_BASIS_IDENTITY_INVALID",
                "The cost basis identity exceeds the ledger limit.",
            )

    @property
    def price_identity(self) -> str:
        """Identify the exact snapshot id and content hash."""

        return f"{self.pricing_snapshot_id}:{self.pricing_snapshot_hash}"

    @property
    def basis_identity(self) -> str:
        """Identify the frozen proof, route, and price without duplicating hashes."""

        return (
            f"{_BASIS_IDENTITY_VERSION}:proof={self.identity}:"
            f"route={self.route.identity}:price={self.price_identity}"
        )


class BillingInputUpperBoundValidator(Protocol):
    """Server-supplied hard input-billing validator for one trusted route."""

    identity: str

    def validate(
        self,
        request: ModelRequest,
        *,
        max_billable_input_tokens: int,
    ) -> bool: ...


class BillingAttestationRegistry:
    """An immutable registry populated only by trusted server composition."""

    __slots__ = ("_attestations", "_validators")

    def __init__(
        self,
        attestations: Iterable[BillingAttestation] = (),
        *,
        validators: Mapping[str, BillingInputUpperBoundValidator] | None = None,
    ) -> None:
        entries: dict[str, BillingAttestation] = {}
        for attestation in attestations:
            if not isinstance(attestation, BillingAttestation):
                raise CaseBudgetPricingError(
                    "CASE_BUDGET_ATTESTATION_INVALID",
                    "The registry contains an invalid attestation.",
                )
            if attestation.identity in entries:
                raise CaseBudgetPricingError(
                    "CASE_BUDGET_ATTESTATION_DUPLICATE",
                    "The registry contains a duplicate attestation identity.",
                )
            entries[attestation.identity] = attestation
        self._attestations = MappingProxyType(entries)
        self._validators = MappingProxyType(dict(validators or {}))

    @classmethod
    def empty(cls) -> Self:
        """Return the deny-by-default registry."""

        return cls()

    @classmethod
    def server_supplied(
        cls,
        attestations: Iterable[BillingAttestation],
        *,
        validators: Mapping[str, BillingInputUpperBoundValidator],
    ) -> Self:
        """Build a registry from server-verified evidence and validators."""

        return cls(attestations, validators=validators)

    def get(self, identity: str) -> BillingAttestation | None:
        """Look up one exact attestation without route inference."""

        if not isinstance(identity, str):
            return None
        return self._attestations.get(identity)

    def get_validator(
        self,
        attestation: BillingAttestation,
    ) -> BillingInputUpperBoundValidator | None:
        """Resolve the validator explicitly named by the attestation."""

        if not isinstance(attestation, BillingAttestation):
            return None
        validator = self._validators.get(
            attestation.request_evidence.input_upper_bound_validator_id
        )
        if validator is None:
            return None
        identity = getattr(validator, "identity", None)
        if identity != attestation.request_evidence.input_upper_bound_validator_id:
            return None
        if not callable(getattr(validator, "validate", None)):
            return None
        return validator

    @property
    def attestation_ids(self) -> tuple[str, ...]:
        """Expose immutable registry membership for diagnostics."""

        return tuple(sorted(self._attestations))

    def __len__(self) -> int:
        return len(self._attestations)


class InteractiveCaseAttemptPriceSource:
    """A route-pinned ``ModelAttemptPriceSource`` for synchronous generation.

    The gateway protocol has no route argument, so this instance binds the exact
    frozen route at construction.  Every quote re-reads the profile and
    credential inside the reservation session and requires them to still match.
    ``resolve`` preserves source compatibility for a source instance explicitly
    scoped to generate; adapters that know the operation should call
    ``resolve_for_operation``.
    """

    def __init__(
        self,
        registry: BillingAttestationRegistry,
        *,
        attestation_identity: str,
        frozen_route: FrozenProviderRoute,
        operation: Literal["generate"] = "generate",
    ) -> None:
        if not isinstance(registry, BillingAttestationRegistry):
            raise TypeError("registry must be a BillingAttestationRegistry")
        if operation != "generate":
            raise CaseBudgetPricingError(
                "CASE_BUDGET_OPERATION_UNSUPPORTED",
                "Interactive case budget v1 supports generate requests only.",
            )
        identity = _required_text(attestation_identity, "attestation_identity", maximum=64)
        if not isinstance(frozen_route, FrozenProviderRoute):
            raise TypeError("frozen_route must be a FrozenProviderRoute")
        self._registry = registry
        self._attestation_identity = identity
        self._frozen_route = frozen_route
        self._operation = operation

    async def resolve(
        self,
        session: AsyncSession,
        *,
        profile: ResolvedModelProfile,
        request: ModelRequest,
        provider: str,
        attempt_kind: ModelAttemptKind,
    ) -> AttemptCostUpperBound | None:
        """Implement the existing price-source protocol for the bound operation."""

        return await self.resolve_for_operation(
            session,
            profile=profile,
            request=request,
            provider=provider,
            attempt_kind=attempt_kind,
            operation=self._operation,
        )

    async def resolve_for_operation(
        self,
        session: AsyncSession,
        *,
        profile: ResolvedModelProfile,
        request: ModelRequest,
        provider: str,
        attempt_kind: ModelAttemptKind,
        operation: Literal["generate", "stream"],
    ) -> AttemptCostUpperBound | None:
        """Return a quote only when every route and billing proof is exact."""

        if operation != "generate" or attempt_kind is not ModelAttemptKind.MAIN_MODEL:
            return None
        if not isinstance(profile, ResolvedModelProfile) or not isinstance(request, ModelRequest):
            return None
        if not isinstance(provider, str) or session is None:
            return None

        attestation = self._registry.get(self._attestation_identity)
        if attestation is None or attestation.route != self._frozen_route:
            return None

        validator = self._registry.get_validator(attestation)
        if validator is None:
            return None

        try:
            if not self._request_is_admissible(profile, request, provider, attestation):
                return None
            if (
                validator.validate(
                    request,
                    max_billable_input_tokens=(
                        attestation.request_evidence.max_billable_input_tokens
                    ),
                )
                is not True
            ):
                return None
            if not await self._current_binding_matches(session, profile, attestation, provider):
                return None
            amount = _cost_upper_bound(attestation)
            if amount is None:
                return None
            return AttemptCostUpperBound(
                amount=amount,
                currency=attestation.currency,
                basis_identity=attestation.basis_identity,
            )
        except Exception:
            # Unknown DB state, validator failure, or malformed evidence all
            # remain pre-dispatch refusals.  Cancellation is not an Exception.
            return None

    @staticmethod
    def _request_is_admissible(
        profile: ResolvedModelProfile,
        request: ModelRequest,
        provider: str,
        attestation: BillingAttestation,
    ) -> bool:
        evidence = attestation.request_evidence
        route = attestation.route
        if provider != route.provider or profile.workspace_id != attestation.workspace_id:
            return False
        if profile.fallback_profile_id is not None or request.retry_policy.max_attempts != 1:
            return False
        if request.request_mode is not evidence.request_mode:
            return False
        if profile.model != route.model:
            return False
        if profile.max_tokens > evidence.max_billable_output_tokens:
            return False
        required = request.required_capabilities
        if "vision" in required.required:
            return False
        if (
            required.max_context_tokens is not None
            and required.max_context_tokens > evidence.max_billable_input_tokens
        ):
            return False
        if profile.capabilities.vision:
            return False
        if request.tools:
            return False
        if any(message.role == "tool" or bool(message.tool_calls) for message in request.messages):
            return False
        return evidence.fee_coverage == SUPPORTED_FEE_COVERAGE

    @staticmethod
    async def _current_binding_matches(
        session: AsyncSession,
        profile: ResolvedModelProfile,
        attestation: BillingAttestation,
        provider: str,
    ) -> bool:
        snapshot = await session.get(PricingSnapshot, attestation.pricing_snapshot_id)
        current_profile = await session.get(ModelProfile, profile.id)
        credential = await session.get(
            ProviderCredential,
            profile.provider_credential_id,
        )
        if snapshot is None or current_profile is None or credential is None:
            return False

        snapshot_hash = pricing_snapshot_content_hash(snapshot)
        if (
            snapshot.id != attestation.pricing_snapshot_id
            or snapshot.workspace_id != attestation.workspace_id
            or snapshot.provider != attestation.route.provider
            or snapshot.model != attestation.route.model
            or snapshot.currency != "CNY"
            or snapshot.content_hash != attestation.pricing_snapshot_hash
            or snapshot_hash != attestation.pricing_snapshot_hash
            or _rate(snapshot.input_price_per_1m, "snapshot input price")
            != attestation.input_price_per_1m
            or _rate(snapshot.output_price_per_1m, "snapshot output price")
            != attestation.output_price_per_1m
        ):
            return False
        snapshot_cached = (
            None
            if snapshot.cached_input_price_per_1m is None
            else _rate(snapshot.cached_input_price_per_1m, "snapshot cached input price")
        )
        if snapshot_cached != attestation.cached_input_price_per_1m:
            return False

        current_capabilities = validate_capabilities(
            current_profile.capabilities,
            CapabilityRequirements(),
        )
        if (
            current_profile.workspace_id != profile.workspace_id
            or current_profile.enabled is not True
            or current_profile.provider_credential_id != profile.provider_credential_id
            or current_profile.model != profile.model
            or current_profile.temperature != profile.temperature
            or current_profile.max_tokens != profile.max_tokens
            or current_profile.timeout_seconds != profile.timeout_seconds
            or current_profile.fallback_profile_id is not None
            or current_capabilities != profile.capabilities
        ):
            return False
        normalized_credential_base_url = normalize_provider_base_url(credential.base_url)
        effective_credential_base_url = effective_provider_base_url(
            provider,
            normalized_credential_base_url,
        )
        if (
            credential.id != profile.provider_credential_id
            or credential.workspace_id != profile.workspace_id
            or credential.enabled is not True
            or credential.provider != provider
            or credential.provider != attestation.route.provider
            or normalized_credential_base_url != attestation.route.base_url
            or effective_credential_base_url != attestation.route.effective_endpoint
        ):
            return False
        return True


def _cost_upper_bound(attestation: BillingAttestation) -> Decimal | None:
    """Ceil exact token/rate arithmetic to the ledger's 8-place precision."""

    evidence = attestation.request_evidence
    input_rate = attestation.input_price_per_1m
    if (
        attestation.cached_input_price_per_1m is not None
        and attestation.cached_input_price_per_1m > input_rate
    ):
        input_rate = attestation.cached_input_price_per_1m
    input_scaled = _scaled_rate(input_rate)
    output_scaled = _scaled_rate(attestation.output_price_per_1m)
    total_scaled = (
        evidence.max_billable_input_tokens * input_scaled
        + evidence.max_billable_output_tokens * output_scaled
    )
    if total_scaled <= 0:
        return None
    rounded_units = (total_scaled + _TOKENS_PER_PRICE_UNIT - 1) // _TOKENS_PER_PRICE_UNIT
    amount = Decimal(rounded_units).scaleb(-_MONEY_SCALE)
    if amount <= 0 or amount > _MAX_LEDGER_AMOUNT:
        return None
    return amount


def _scaled_rate(value: Decimal) -> int:
    numerator, denominator = _rate(value, "price").as_integer_ratio()
    scaled_numerator = numerator * _RATE_SCALE
    if scaled_numerator % denominator:
        raise CaseBudgetPricingError(
            "CASE_BUDGET_PRECISION_INVALID",
            "A price exceeds the supported 8-place precision.",
        )
    return scaled_numerator // denominator


def _rate(value: Decimal, field: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise CaseBudgetPricingError(
            "CASE_BUDGET_PRICE_INVALID",
            f"{field} must be a Decimal.",
        )
    if not value.is_finite() or value.is_signed() or value < 0:
        raise CaseBudgetPricingError(
            "CASE_BUDGET_PRICE_INVALID",
            f"{field} must be finite and non-negative.",
        )
    if value.as_tuple().exponent < -_MONEY_SCALE:
        raise CaseBudgetPricingError(
            "CASE_BUDGET_PRECISION_INVALID",
            f"{field} exceeds the supported 8-place precision.",
        )
    return value


def _positive_integer(value: int, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise CaseBudgetPricingError(
            "CASE_BUDGET_BOUND_INVALID",
            f"{field} must be a positive integer.",
        )
    return value


def _required_text(value: str, field: str, *, maximum: int | None = None) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\x00" in value
        or (maximum is not None and len(value) > maximum)
    ):
        raise CaseBudgetPricingError(
            "CASE_BUDGET_IDENTITY_INVALID",
            f"{field} must be a trimmed, non-empty string.",
        )
    return value


__all__ = [
    "BillingAttestation",
    "BillingAttestationRegistry",
    "BillingFeeKind",
    "BillingInputUpperBoundValidator",
    "CaseBudgetPricingError",
    "FrozenProviderRoute",
    "INTERACTIVE_CASE_BUDGET_POLICY",
    "InteractiveCaseAttemptPriceSource",
    "RequestAdmissibilityEvidence",
    "SUPPORTED_FEE_COVERAGE",
]
