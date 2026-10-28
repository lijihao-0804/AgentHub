"""Single-attempt summary worker; scheduling and promotion remain opt-in.

The worker only executes a claimed durable job with a matching frozen model,
server-verified prompt, explicit price reservation, and budget admission. It
never retries or falls back after a provider boundary may have been crossed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from packages.agent_runtime.context_budget import (
    ContextBudgetPolicy,
    ContextCategory,
    ContextMessage,
)
from packages.agent_runtime.deepseek_v41_counter import DeepseekV41TextCounter
from packages.agent_runtime.frozen import (
    SUMMARY_NON_THINKING_COUNTER_ID,
    SUMMARY_NON_THINKING_MODE,
    FrozenAgentSpec,
    _parse_summary_policy,
    frozen_summary_request_identity,
)
from packages.agent_runtime.summary_service import (
    DEFAULT_LEASE_SECONDS,
    ClaimedSummaryJob,
    SummaryJobKey,
    SummaryJobService,
    SummaryResponseData,
    SummaryServiceError,
    SummarySettlement,
)
from packages.agent_runtime.summary_state import SummaryJobStatus
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.model_gateway.context_metadata import context_metadata_to_mapping
from packages.model_gateway.contracts import (
    AliyunDeepSeekRequestMode,
    CostEstimate,
    ModelGateway,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ResolvedModelExecutionPlan,
    ResolvedModelExecutionProfile,
    RetryPolicy,
)


def frozen_summary_model_spec(
    profile: ResolvedModelExecutionProfile,
    *,
    request_strategy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind the auxiliary job to the same non-secret frozen model profile."""
    capabilities = profile.capabilities
    result = {
        "provider": profile.provider,
        "model": profile.model,
        "profile_id": str(profile.id),
        "credential_ref": str(profile.provider_credential_id),
        "workspace_id": str(profile.workspace_id),
        "temperature": str(profile.temperature),
        "max_tokens": profile.max_tokens,
        "timeout_seconds": str(profile.timeout_seconds),
        "capabilities": {
            "tool_calling": capabilities.tool_calling,
            "streaming": capabilities.streaming,
            "structured_output": capabilities.structured_output,
            "vision": capabilities.vision,
            "max_context_tokens": capabilities.max_context_tokens,
            "context_metadata": (
                context_metadata_to_mapping(capabilities.context_metadata)
                if capabilities.context_metadata is not None
                else None
            ),
        },
    }
    if request_strategy is not None:
        identity = frozen_summary_request_identity(request_strategy)
        if identity["request_mode"] == SUMMARY_NON_THINKING_MODE:
            result.update(identity)
    return result


class SummaryWorker:
    """Execute one claimed auxiliary call and persist before validating."""

    def __init__(
        self,
        service: SummaryJobService,
        gateway: ModelGateway,
        *,
        gateway_preparer: Callable[
            [WorkspaceExecutionContext, ResolvedModelExecutionPlan, ModelRequest],
            Awaitable[ModelGateway],
        ]
        | None = None,
        request_counter: DeepseekV41TextCounter | None = None,
    ) -> None:
        self.service = service
        self.gateway = gateway
        self.gateway_preparer = gateway_preparer
        self.request_counter = request_counter

    async def run_claimed(
        self,
        *,
        claimed: ClaimedSummaryJob,
        context: WorkspaceExecutionContext,
        frozen_spec: FrozenAgentSpec,
        pricing_snapshot_id: UUID,
        before_network_call: Callable[[], Awaitable[None]] | None = None,
        on_validated: Callable[[SummaryJobKey, str], Awaitable[SummarySettlement]] | None = None,
    ) -> SummarySettlement:
        if not isinstance(claimed, ClaimedSummaryJob):
            raise TypeError("claimed must be a server-issued summary lease")
        if not isinstance(context, WorkspaceExecutionContext) or not isinstance(
            frozen_spec, FrozenAgentSpec
        ):
            raise TypeError("workspace context and frozen AgentVersion are required")
        if context.workspace_id != str(claimed.key.workspace_id):
            raise SummaryServiceError("SUMMARY_WORKER_SCOPE_INVALID", "Workspace scope differs.")
        summary_policy = frozen_spec.runtime.get("summary_policy")
        try:
            summary_policy = _parse_summary_policy(
                summary_policy, primary=frozen_spec.model_plan.primary
            )
            request_identity = frozen_summary_request_identity(summary_policy)
        except AgentHubError:
            raise SummaryServiceError(
                "SUMMARY_WORKER_DISABLED", "The frozen summary policy is unavailable."
            ) from None
        policy = ContextBudgetPolicy(frozen_spec)
        if policy.context_policy is None:
            raise SummaryServiceError(
                "SUMMARY_WORKER_POLICY_INVALID", "Frozen context policy is required."
            )
        primary = frozen_spec.model_plan.primary
        if primary.workspace_id != claimed.key.workspace_id:
            raise SummaryServiceError("SUMMARY_WORKER_SCOPE_INVALID", "Model scope differs.")
        timeout = primary.timeout_seconds
        # Published profiles use NUMERIC(8, 3); reject malformed frozen values
        # before obtaining a lease or crossing a provider boundary.
        if (
            not isinstance(timeout, Decimal)
            or not timeout.is_finite()
            or not Decimal(0) < timeout <= Decimal("99999.999")
        ):
            raise SummaryServiceError(
                "SUMMARY_WORKER_POLICY_INVALID", "Model timeout is unavailable."
            )
        input_bound = policy.context_policy["model_available_input_tokens"]
        output_bound = primary.max_tokens
        if (
            type(input_bound) is not int
            or input_bound <= 0
            or type(output_bound) is not int
            or output_bound <= 0
            or policy.reserved_output_tokens < output_bound
        ):
            raise SummaryServiceError(
                "SUMMARY_WORKER_POLICY_INVALID", "Model bounds are unavailable."
            )
        prepared = await self.service.prepare_dispatch(
            claimed.key,
            lease_token=claimed.lease_token,
            reserved_input_tokens=input_bound,
            reserved_output_tokens=output_bound,
        )
        if context.user_id != str(prepared.created_by):
            raise SummaryServiceError("SUMMARY_WORKER_SCOPE_INVALID", "Execution actor differs.")
        if (
            prepared.policy_version != summary_policy["policy_version"]
            or prepared.prompt_version != summary_policy["prompt_version"]
            or prepared.prompt.prompt_version != prepared.prompt_version
            or prepared.tokenizer_id != summary_policy["tokenizer_id"]
            or prepared.tokenizer_version != str(summary_policy["tokenizer_version"])
        ):
            raise SummaryServiceError(
                "SUMMARY_WORKER_POLICY_MISMATCH", "Frozen summary policy differs."
            )
        if dict(prepared.model_spec) != frozen_summary_model_spec(
            primary, request_strategy=summary_policy
        ):
            raise SummaryServiceError(
                "SUMMARY_WORKER_MODEL_MISMATCH", "Frozen model identity differs."
            )
        messages = (
            ModelMessage("system", prepared.prompt.system_prompt),
            ModelMessage("user", prepared.prompt.user_prompt),
        )
        tagged = (
            ContextMessage(messages[0], ContextCategory.SYSTEM_PROMPT),
            ContextMessage(messages[1], ContextCategory.CURRENT_USER_TASK),
        )
        admission = policy.admit(tagged)
        if (
            admission.messages != messages
            or admission.usage.truncated
            or admission.usage.dropped_exchange_count
            or admission.tool_definitions
        ):
            raise SummaryServiceError(
                "SUMMARY_WORKER_BUDGET_REJECTED", "Prompt was not fully admitted."
            )
        request_mode_value = prepared.request_spec.get(
            "request_mode", request_identity["request_mode"]
        )
        frozen_request_mode = request_identity["request_mode"]
        if request_mode_value != frozen_request_mode:
            raise SummaryServiceError(
                "SUMMARY_WORKER_REQUEST_MODE_MISMATCH",
                "The dispatch request mode differs from the frozen summary strategy.",
            )
        if frozen_request_mode == SUMMARY_NON_THINKING_MODE and any(
            prepared.request_spec.get(key) != value for key, value in request_identity.items()
        ):
            raise SummaryServiceError(
                "SUMMARY_WORKER_REQUEST_MODE_MISMATCH",
                "The dispatch counter or route identity differs from the frozen strategy.",
            )
        try:
            request_mode = AliyunDeepSeekRequestMode(request_mode_value)
        except (TypeError, ValueError):
            raise SummaryServiceError(
                "SUMMARY_WORKER_REQUEST_MODE_UNSUPPORTED",
                "The frozen summary request mode is unsupported.",
            ) from None
        request = ModelRequest(
            messages=messages,
            retry_policy=RetryPolicy(max_attempts=1),
            request_mode=request_mode,
        )
        if frozen_request_mode == SUMMARY_NON_THINKING_MODE:
            try:
                exact_input_tokens = _count_frozen_summary_request_tokens(
                    request,
                    request_identity=request_identity,
                    counter=self.request_counter,
                    provider=primary.provider,
                    model=primary.model,
                )
            except Exception:
                exact_input_tokens = None
                counter_failure_code = "SUMMARY_REQUEST_COUNTER_FAILED"
            else:
                counter_failure_code = "SUMMARY_REQUEST_COUNTER_UNAVAILABLE"

            if exact_input_tokens is None:
                await self.service.fail_before_dispatch(
                    claimed.key,
                    lease_token=claimed.lease_token,
                    failure_code=counter_failure_code,
                )
                raise SummaryServiceError(
                    counter_failure_code,
                    "The frozen full-request counter is unavailable for this summary strategy.",
                )
            if type(exact_input_tokens) is not int or exact_input_tokens <= 0:
                await self.service.fail_before_dispatch(
                    claimed.key,
                    lease_token=claimed.lease_token,
                    failure_code="SUMMARY_REQUEST_COUNTER_INVALID",
                )
                raise SummaryServiceError(
                    "SUMMARY_REQUEST_COUNTER_INVALID",
                    "The frozen full-request counter returned an invalid count.",
                )
            if exact_input_tokens > admission.usage.available_input:
                await self.service.fail_before_dispatch(
                    claimed.key,
                    lease_token=claimed.lease_token,
                    failure_code="SUMMARY_REQUEST_BUDGET_EXCEEDED",
                )
                raise SummaryServiceError(
                    "SUMMARY_WORKER_BUDGET_REJECTED",
                    "The complete summary request exceeds the frozen input budget.",
                )
        # The auxiliary job has one durable price reservation and one dispatch.
        # Main-agent fallback configuration remains unchanged.
        single_attempt = ResolvedModelExecutionPlan(
            primary=primary, fallbacks=(), retry_policy=RetryPolicy(max_attempts=1)
        )
        prepared_gateway = self.gateway
        if self.gateway_preparer is not None:
            try:
                prepared_gateway = await self.gateway_preparer(
                    context, single_attempt, request
                )
            except BaseException:
                await asyncio.shield(
                    self.service.fail_before_dispatch(
                        claimed.key, lease_token=claimed.lease_token
                    )
                )
                raise
            if not callable(getattr(prepared_gateway, "generate_resolved", None)):
                await self.service.fail_before_dispatch(
                    claimed.key, lease_token=claimed.lease_token
                )
                raise SummaryServiceError(
                    "SUMMARY_WORKER_GATEWAY_INVALID", "The prepared gateway is unavailable."
                )
        # A long provider call must not outlive an ordinary claim merely
        # because the model profile allows more than the default lease. Renew
        # before the intent: an unsuccessful renewal cannot create a dispatch.
        lease_seconds = int(timeout.to_integral_value(rounding=ROUND_CEILING))
        await self.service.renew_lease(
            claimed.key,
            lease_token=claimed.lease_token,
            lease_seconds=lease_seconds + DEFAULT_LEASE_SECONDS,
        )
        await self.service.commit_dispatch_intent(
            claimed.key,
            lease_token=claimed.lease_token,
            pricing_snapshot_id=pricing_snapshot_id,
            reserved_input_tokens=input_bound,
            reserved_output_tokens=output_bound,
            request_spec=prepared.request_spec,
        )
        await self.service.mark_dispatched(claimed.key, lease_token=claimed.lease_token)
        if before_network_call is not None:
            try:
                await before_network_call()
            except BaseException:
                # The durable dispatch fence has been crossed. Even when the
                # local owner check prevents the provider call, recovery must
                # not treat the job as safely retryable.
                await asyncio.shield(
                    self.service.mark_dispatch_unknown(
                        claimed.key, lease_token=claimed.lease_token
                    )
                )
                raise
        try:
            async with asyncio.timeout(float(timeout)):
                response = await prepared_gateway.generate_resolved(
                    context, single_attempt, request
                )
            await self.service.store_provider_response(
                claimed.key,
                lease_token=claimed.lease_token,
                response=_response_data(response),
            )
        except asyncio.CancelledError:
            await asyncio.shield(
                self.service.mark_dispatch_unknown(claimed.key, lease_token=claimed.lease_token)
            )
            raise
        except Exception:
            return await self.service.mark_dispatch_unknown(
                claimed.key, lease_token=claimed.lease_token
            )
        # Saving a provider response releases the dispatch lease. Reacquire a
        # local recovery lease for validation, then let the caller publish
        # under that same token when it owns the inline path.
        return await self.validate_stored(claimed.key, on_validated=on_validated)

    async def validate_stored(
        self,
        key: SummaryJobKey,
        *,
        on_validated: Callable[[SummaryJobKey, str], Awaitable[SummarySettlement]] | None = None,
    ) -> SummarySettlement:
        """Resume a persisted response and optionally publish under the recovery lease."""
        lease = await self.service.claim_recovery(key)
        if lease is None:
            return await self.service.get_settlement(key)
        result = await self.service.validate_stored_response(key, lease_token=lease.lease_token)
        if result.status == SummaryJobStatus.PUBLISHED.value:
            raise SummaryServiceError("SUMMARY_WORKER_STATE_INVALID", "Unexpected published state.")
        if result.status == SummaryJobStatus.VALIDATED.value and on_validated is not None:
            return await on_validated(key, lease.lease_token)
        return result


def _count_frozen_summary_request_tokens(
    request: ModelRequest,
    *,
    request_identity: Mapping[str, Any],
    counter: DeepseekV41TextCounter | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> int | None:
    """Return an exact finite-format count only from a digest-verified asset."""
    if counter is None or not counter.verified_source:
        return None
    if (
        request_identity.get("request_counter_id") != SUMMARY_NON_THINKING_COUNTER_ID
        or request_identity.get("request_counter_thinking") is not False
    ):
        return None
    try:
        return counter.count_frozen_summary_request(
            request,
            request_identity=request_identity,
            provider=provider,
            model=model,
        )
    except (TypeError, ValueError):
        return None


def _response_data(response: ModelResponse) -> SummaryResponseData:
    if not isinstance(response, ModelResponse):
        raise TypeError("gateway returned an invalid response")
    usage = response.usage
    usage_valid = (
        isinstance(usage, ModelUsage)
        and all(
            type(value) is int and 0 <= value <= 2_147_483_647
            for value in (usage.input_tokens, usage.output_tokens, usage.total_tokens)
        )
        and usage.total_tokens == usage.input_tokens + usage.output_tokens
        and (
            usage.cached_tokens is None
            or type(usage.cached_tokens) is int
            and 0 <= usage.cached_tokens <= usage.input_tokens
        )
    )
    cost = response.cost_estimate
    cost_valid = isinstance(cost, CostEstimate) and _persistable_cost(cost)
    metadata = {
        "usage_status": "VERIFIED" if usage_valid else "UNKNOWN",
        "cost_status": (
            "ESTIMATED"
            if cost_valid and cost is not None and cost.is_estimate
            else "PROVIDER_REPORTED"
            if cost_valid
            else "INVALID_REPORTED"
            if cost is not None
            else "UNKNOWN"
        ),
        "unexpected_tool_call_count": len(response.tool_calls),
        "provider_finish_reason": response.finish_reason,
    }
    return SummaryResponseData(
        provider=response.provider,
        model=response.model,
        raw_response=response.content,
        usage_metadata=metadata,
        input_tokens=usage.input_tokens if usage_valid and usage is not None else None,
        output_tokens=usage.output_tokens if usage_valid and usage is not None else None,
        total_tokens=usage.total_tokens if usage_valid and usage is not None else None,
        cached_tokens=usage.cached_tokens if usage_valid and usage is not None else None,
        cost_amount=cost.amount if cost_valid and cost is not None else None,
        cost_currency=cost.currency if cost_valid and cost is not None else None,
        cost_is_estimate=cost.is_estimate if cost_valid and cost is not None else None,
        finish_reason="unexpected_tool_calls" if response.tool_calls else response.finish_reason,
    )


def _persistable_cost(cost: CostEstimate) -> bool:
    """Reject invalid metadata without discarding an already received response.

    ThreadSummaryResponse uses NUMERIC(20, 8) and an ASCII currency code. Do
    not silently round a reported amount or let a database conversion failure
    erase the response and make a known provider result appear unknown.
    """
    amount = cost.amount
    currency = cost.currency
    if (
        not isinstance(amount, Decimal)
        or not amount.is_finite()
        or amount < 0
        or amount >= Decimal("1000000000000")
        or not isinstance(currency, str)
        or len(currency) != 3
        or not currency.isascii()
        or not currency.isalpha()
        or not currency.isupper()
        or type(cost.is_estimate) is not bool
    ):
        return False
    try:
        return amount == amount.quantize(Decimal("0.00000001"))
    except InvalidOperation:
        return False
