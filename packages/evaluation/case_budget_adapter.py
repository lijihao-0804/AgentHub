"""Case-linked attempt reservations; deliberately not enabled by composition.

The caller must first pass support admission and create immutable account/turn
bindings. No default billing attestation or production enablement is provided.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.frozen import parse_frozen_agent_spec
from packages.agent_runtime.model_budget_adapter import RunBudgetAttemptAdapter
from packages.agent_runtime.models import AgentVersion
from packages.evaluation.case_budget_fence import lock_case_budget_dispatch
from packages.evaluation.case_budget_ledger import CaseBudgetLedger
from packages.evaluation.case_budget_pricing import (
    BillingAttestation,
    BillingAttestationRegistry,
    FrozenProviderRoute,
    InteractiveCaseAttemptPriceSource,
)
from packages.evaluation.interactive_lifecycle import CaseExecutionIdentity
from packages.model_gateway.contracts import ModelResponse
from packages.model_gateway.egress import effective_provider_base_url
from packages.model_gateway.run_budget_adapter import (
    ModelAttemptBudgetError,
    ModelAttemptBudgetReservation,
    ModelAttemptKind,
)


class CaseBudgetAttemptAdapter(RunBudgetAttemptAdapter):
    """Commit a fenced cross-Run hold before one synchronous provider attempt."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        identity: CaseExecutionIdentity,
        turn_index: int,
        agent_run_id: UUID,
        model_round: int,
        execution_owner: str,
        attestation: BillingAttestation,
        registry: BillingAttestationRegistry,
    ) -> None:
        if not isinstance(identity, CaseExecutionIdentity):
            raise TypeError("identity must be a CaseExecutionIdentity")
        if type(turn_index) is not int or turn_index < 0:
            raise ValueError("turn_index must be a non-negative integer")
        if type(model_round) is not int or model_round < 1:
            raise ValueError("model_round must be a positive integer")
        if not isinstance(execution_owner, str) or not execution_owner.strip():
            raise ValueError("execution_owner must be non-empty")
        if (
            not isinstance(attestation, BillingAttestation)
            or attestation.workspace_id != identity.workspace_id
            or registry.get(attestation.identity) != attestation
        ):
            raise ValueError("The server registry must contain this workspace's attestation")
        self._identity = identity
        self._turn_index = turn_index
        self._model_round = model_round
        self._execution_owner = execution_owner
        self._attestation = attestation
        self._held_reservation: ModelAttemptBudgetReservation | None = None
        super().__init__(
            session_factory,
            workspace_id=identity.workspace_id,
            run_id=agent_run_id,
            call_identity=f"round-{model_round}",
            price_source=InteractiveCaseAttemptPriceSource(
                registry,
                attestation_identity=attestation.identity,
                frozen_route=attestation.route,
            ),
            ledger=CaseBudgetLedger(),
        )

    async def reserve_attempt(self, *args, **kwargs) -> ModelAttemptBudgetReservation:
        try:
            reservation = await super().reserve_attempt(*args, **kwargs)
        except ModelAttemptBudgetError as error:
            if error.code in {"RUN_BUDGET_ATTEMPT_REPLAYED", "RUN_BUDGET_CASE_ATTEMPT_REPLAYED"}:
                # A committed hold alone cannot prove whether the remote call
                # happened before a crash. Keep it and refuse another dispatch.
                raise ModelAttemptBudgetError(
                    "CASE_BUDGET_ATTEMPT_RECONCILIATION_REQUIRED",
                    "The retained case attempt requires reconciliation before recovery.",
                ) from None
            raise
        self._held_reservation = reservation
        return reservation

    async def _reservation_evidence(
        self,
        session,
        *,
        context,
        quote,
        profile,
        request,
        provider,
        operation,
        profile_index,
        attempt_index,
        attempt_kind,
    ) -> dict[str, str]:
        if (
            operation != "generate"
            or profile_index != 0
            or attempt_index != 0
            or attempt_kind is not ModelAttemptKind.MAIN_MODEL
            or request.retry_policy.max_attempts != 1
            or profile.fallback_profile_id is not None
        ):
            raise ModelAttemptBudgetError(
                "CASE_BUDGET_ATTEMPT_UNSUPPORTED", "This case attempt is unsupported."
            )
        run = await lock_case_budget_dispatch(
            session,
            identity=self._identity,
            turn_index=self._turn_index,
            agent_run_id=self._run_id,
            execution_owner=self._execution_owner,
        )
        if str(run.created_by) != context.user_id or "evaluation_run" not in context.permissions:
            raise ModelAttemptBudgetError(
                "CASE_BUDGET_ACTOR_BINDING_INVALID", "The case actor binding differs."
            )
        version = await session.get(AgentVersion, run.agent_version_id)
        if (
            version is None
            or version.workspace_id != self._workspace_id
            or version.resolved_spec_hash != run.resolved_spec_hash
        ):
            raise _frozen_binding_invalid()
        try:
            spec = parse_frozen_agent_spec(version.resolved_spec, workspace_id=self._workspace_id)
            frozen = spec.model_plan.primary
            route = FrozenProviderRoute.from_provider_egress(
                provider=frozen.provider, model=frozen.model, provider_egress=frozen.provider_egress
            )
        except Exception:
            raise _frozen_binding_invalid() from None
        if (
            spec.model_plan.fallbacks
            or spec.model_plan.retry_policy.max_attempts != 1
            or route != self._attestation.route
            or frozen.id != profile.id
            or frozen.provider_credential_id != profile.provider_credential_id
            or frozen.model != profile.model
            or frozen.max_tokens != profile.max_tokens
            or frozen.temperature != profile.temperature
            or frozen.timeout_seconds != profile.timeout_seconds
            or frozen.capabilities != profile.capabilities
        ):
            raise _frozen_binding_invalid()
        account = await self._ledger.get_case_account(
            session,
            workspace_id=self._workspace_id,
            case_id=self._identity.case_id,
        )
        proof = self._attestation
        if (
            account.experiment_variant_id != self._identity.variant_id
            or account.pricing_snapshot_id != proof.pricing_snapshot_id
            or account.pricing_snapshot_hash != proof.pricing_snapshot_hash
            or account.provider != provider
            or account.provider != proof.route.provider
            or account.model != profile.model
            or account.model != proof.route.model
            or account.provider_egress_policy_version != proof.route.policy_version
            or account.provider_base_url != proof.route.base_url
            or effective_provider_base_url(account.provider, account.provider_base_url)
            != proof.route.effective_endpoint
            or quote.basis_identity != proof.basis_identity
            or quote.currency != account.currency
        ):
            raise ModelAttemptBudgetError(
                "CASE_BUDGET_PRICE_BINDING_INVALID", "The frozen case price binding differs."
            )
        return {
            "case_attempt_identity": (
                f"interactive-v1:turn-{self._turn_index}:round-{self._model_round}"
                ":generate:profile-0:attempt-0"
            ),
            "basis_identity": quote.basis_identity,
        }

    async def record_response(self, response: ModelResponse) -> None:
        """Append usage only; success never releases or settles the hold.

        A late response may still add evidence after lease loss. Its immutable
        attempt must already exist; this path never creates a dispatch or bill.
        """
        reservation = self._held_reservation
        if reservation is None or not isinstance(response, ModelResponse):
            raise ModelAttemptBudgetError(
                "CASE_BUDGET_RESPONSE_UNBOUND", "No committed attempt binds this response."
            )
        if (
            response.provider != self._attestation.route.provider
            or response.model != self._attestation.route.model
        ):
            raise ModelAttemptBudgetError(
                "CASE_BUDGET_RESPONSE_BINDING_INVALID", "The response model binding differs."
            )
        usage, cost = response.usage, response.cost_estimate
        async with self._session_factory() as session:
            async with session.begin():
                account = await self._ledger.get_case_account(
                    session, workspace_id=self._workspace_id, case_id=self._identity.case_id
                )
                await self._ledger.record_response_observation(
                    session,
                    workspace_id=self._workspace_id,
                    run_id=self._run_id,
                    attempt_identity=reservation.attempt_identity,
                    observation_identity=f"response:{reservation.attempt_identity}",
                    provider=response.provider,
                    model=response.model,
                    provider_egress_policy_version=account.provider_egress_policy_version,
                    provider_base_url=account.provider_base_url,
                    input_tokens=usage.input_tokens if usage else None,
                    output_tokens=usage.output_tokens if usage else None,
                    total_tokens=usage.total_tokens if usage else None,
                    cached_tokens=usage.cached_tokens if usage else None,
                    cost_amount=cost.amount if cost else None,
                    cost_currency=cost.currency if cost else None,
                )


def _frozen_binding_invalid() -> ModelAttemptBudgetError:
    return ModelAttemptBudgetError(
        "CASE_BUDGET_FROZEN_MODEL_BINDING_INVALID", "The frozen case model binding differs."
    )
