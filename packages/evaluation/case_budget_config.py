"""Load reviewed, expiring token-billing contracts from a worker-owned local file."""

from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

from packages.core.canonical.json_hash import canonical_json_hash
from packages.evaluation.case_budget_pricing import (
    SUPPORTED_FEE_COVERAGE,
    BillingAttestation,
    BillingAttestationRegistry,
    CaseBudgetPricingError,
    FrozenProviderRoute,
    RequestAdmissibilityEvidence,
)
from packages.model_gateway.contracts import (
    PROVIDER_EGRESS_POLICY_VERSION,
    AliyunDeepSeekRequestMode,
    ModelRequest,
)

_MAX_CONFIG_BYTES = 1_048_576
_TOKEN_BOUND = Annotated[int, Field(strict=True, gt=0, le=2**31 - 1)]
_OVERHEAD = Annotated[int, Field(strict=True, ge=0, le=2**31 - 1)]
_RATE = Annotated[str, StringConstraints(pattern=r"^(0|[1-9][0-9]{0,11})(\.[0-9]{1,8})?$")]


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: UUID
    pricing_snapshot_id: UUID
    pricing_snapshot_hash: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    provider: Literal["openai-compatible"]
    model: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    base_url: Annotated[str, StringConstraints(min_length=1, max_length=2048)]
    currency: Literal["CNY"]
    input_price_per_1m: _RATE
    output_price_per_1m: _RATE
    cached_input_price_per_1m: _RATE | None
    review_reference: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)
    ]
    valid_until: AwareDatetime
    billing_terms: Literal["text_tokens_only_including_failed_requests_v1"]
    request_mode: AliyunDeepSeekRequestMode
    max_billable_input_tokens: _TOKEN_BOUND
    max_billable_output_tokens: _TOKEN_BOUND
    input_bound: Literal["utf8_bytes_plus_attested_framing_v1"]
    tokens_per_utf8_byte: _TOKEN_BOUND
    tokens_per_message: _OVERHEAD
    tokens_per_request: _OVERHEAD

    @property
    def identity(self) -> str:
        return canonical_json_hash(self.model_dump(mode="json"))

    def attestation(self) -> BillingAttestation:
        if not self.base_url.startswith("https://"):
            raise ValueError("The billing contract requires a concrete HTTPS endpoint.")
        return BillingAttestation(
            identity=self.identity,
            workspace_id=self.workspace_id,
            route=FrozenProviderRoute(
                self.provider, self.model, PROVIDER_EGRESS_POLICY_VERSION, self.base_url
            ),
            pricing_snapshot_id=self.pricing_snapshot_id,
            pricing_snapshot_hash=self.pricing_snapshot_hash,
            currency=self.currency,
            input_price_per_1m=Decimal(self.input_price_per_1m),
            output_price_per_1m=Decimal(self.output_price_per_1m),
            cached_input_price_per_1m=(
                Decimal(self.cached_input_price_per_1m)
                if self.cached_input_price_per_1m is not None
                else None
            ),
            request_evidence=RequestAdmissibilityEvidence(
                self.max_billable_input_tokens,
                self.max_billable_output_tokens,
                SUPPORTED_FEE_COVERAGE,
                self.request_mode,
                self.identity,
            ),
            valid_until=self.valid_until,
        )


@dataclass(frozen=True, slots=True)
class _InputBound:
    contract: _Contract

    @property
    def identity(self) -> str:
        return self.contract.identity

    def validate(self, request: ModelRequest, *, max_billable_input_tokens: int) -> bool:
        if (
            max_billable_input_tokens != self.contract.max_billable_input_tokens
            or request.request_mode != self.contract.request_mode
            or not request.messages
            or request.tools
            or request.response_schema is not None
            or request.retry_policy.max_attempts != 1
            or request.required_capabilities.required
        ):
            return False
        bound = (
            self.contract.tokens_per_request
            + len(request.messages) * self.contract.tokens_per_message
        )
        for message in request.messages:
            if (
                message.role not in {"user", "system", "assistant"}
                or type(message.content) is not str
                or message.name is not None
                or message.tool_calls
                or message.tool_call_id is not None
            ):
                return False
            bound += len(message.content.encode("utf-8")) * self.contract.tokens_per_utf8_byte
            if bound > max_billable_input_tokens:
                return False
        return True


class _Document(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1]
    contracts: Annotated[list[_Contract], Field(max_length=512)]


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate billing contract field.")
    return result


def load_case_budget_registry(path: Path | None) -> BillingAttestationRegistry:
    """Call at the start of a job; a missing setting disables finite-budget execution."""
    if path is None:
        return BillingAttestationRegistry.empty()
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            raise ValueError("Expected a regular file.")
        with path.open("rb") as file:
            raw = file.read(_MAX_CONFIG_BYTES + 1)
        if len(raw) > _MAX_CONFIG_BYTES:
            raise ValueError("File size limit exceeded.")
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(data, dict) or type(data.get("schema_version")) is not int:
            raise ValueError("Missing schema version.")
        document = _Document.model_validate(data)
        return BillingAttestationRegistry.server_supplied(
            [contract.attestation() for contract in document.contracts],
            validators={
                contract.identity: _InputBound(contract) for contract in document.contracts
            },
        )
    except (OSError, TypeError, ValueError, RecursionError):
        raise CaseBudgetPricingError(
            "CASE_BUDGET_CONFIG_INVALID", "The deployment billing contracts could not be loaded."
        ) from None
