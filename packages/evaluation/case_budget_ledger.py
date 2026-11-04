"""Evaluation-owned bridge for Interactive case budget persistence."""

from __future__ import annotations

from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.agent_runtime.frozen import parse_frozen_agent_spec
from packages.agent_runtime.models import AgentRun, AgentVersion
from packages.agent_runtime.run_budget_ledger import (
    MAX_AMOUNT,
    CaseBudgetAccountSnapshot,
    CaseBudgetParentAccount,
    CaseBudgetResponseObservationSnapshot,
    CaseBudgetRunBindingSnapshot,
    RunBudgetAccount,
    RunBudgetAttempt,
    RunBudgetAttemptStatus,
    RunBudgetLedger,
    RunBudgetLedgerError,
    _validate_amount,
    _validate_attempt_identity,
    _validate_budget_key,
    _validate_currency,
    _validate_evidence_identity,
    _validate_non_negative_int,
    _validate_optional_non_negative_int,
    _validate_optional_text,
    _validate_required_text,
    _validate_uuid,
)
from packages.evaluation.case_budget_models import (
    CASE_BUDGET_POLICY_VERSION,
    MAX_PROVIDER_BASE_URL_LENGTH,
    CaseBudgetAccount,
    CaseBudgetResponseObservation,
    CaseBudgetRunBinding,
)
from packages.evaluation.models import (
    EvaluationExperimentCaseResult,
    EvaluationExperimentVariant,
    InteractiveEpisode,
    InteractiveEpisodeTurn,
    PricingSnapshot,
)


class _EvaluationCaseBudgetParentPort:
    """Keep parent case locking and totals in the evaluation package."""

    async def lock_for_run(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        run_id: UUID,
    ) -> CaseBudgetParentAccount | None:
        case_id = await session.scalar(
            select(RunBudgetAccount.case_id).where(
                RunBudgetAccount.workspace_id == workspace_id,
                RunBudgetAccount.run_id == run_id,
            )
        )
        if case_id is None:
            return None
        return await self.lock_account(
            session, workspace_id=workspace_id, case_id=case_id
        )

    async def lock_account(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        case_id: UUID,
    ) -> CaseBudgetAccount:
        account = await session.scalar(
            select(CaseBudgetAccount)
            .where(
                CaseBudgetAccount.workspace_id == workspace_id,
                CaseBudgetAccount.case_id == case_id,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if account is None:
            raise RunBudgetLedgerError(
                "CASE_BUDGET_ACCOUNT_NOT_FOUND",
                "The case budget account was not found.",
            )
        expected = (
            await session.execute(
                select(
                    func.coalesce(
                        func.sum(
                            case(
                                (
                                    RunBudgetAttempt.status.in_(
                                        [
                                            RunBudgetAttemptStatus.RESERVED.value,
                                            RunBudgetAttemptStatus.UNKNOWN_OUTCOME.value,
                                        ]
                                    ),
                                    RunBudgetAttempt.reserved_amount,
                                ),
                                else_=Decimal("0"),
                            )
                        ),
                        Decimal("0"),
                    ),
                    func.coalesce(
                        func.sum(
                            case(
                                (
                                    RunBudgetAttempt.status == RunBudgetAttemptStatus.SETTLED.value,
                                    RunBudgetAttempt.settlement_amount,
                                ),
                                else_=Decimal("0"),
                            )
                        ),
                        Decimal("0"),
                    ),
                ).where(
                    RunBudgetAttempt.workspace_id == workspace_id,
                    RunBudgetAttempt.case_id == case_id,
                )
            )
        ).one()
        if (account.reserved_amount, account.settled_amount) != expected:
            raise RunBudgetLedgerError(
                "CASE_BUDGET_ACCOUNT_INVARIANT",
                "The case account totals differ from retained attempt evidence.",
            )
        if account.reserved_amount > account.limit_amount:
            raise RunBudgetLedgerError(
                "CASE_BUDGET_ACCOUNT_INVARIANT",
                "The case reservation exceeds its limit.",
            )
        return account

    async def validate_attempt_binding(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        run_id: UUID,
        case_id: UUID,
        case_attempt_identity: str,
        basis_identity: str,
    ) -> None:
        del basis_identity
        binding = await session.scalar(
            select(CaseBudgetRunBinding).where(
                CaseBudgetRunBinding.workspace_id == workspace_id,
                CaseBudgetRunBinding.case_id == case_id,
                CaseBudgetRunBinding.agent_run_id == run_id,
            )
        )
        if binding is None:
            raise RunBudgetLedgerError(
                "CASE_BUDGET_RUN_BINDING_NOT_FOUND",
                "A case-linked reservation requires its Interactive turn binding.",
            )
        if not case_attempt_identity:
            raise RunBudgetLedgerError(
                "CASE_BUDGET_ATTEMPT_IDENTITY_REQUIRED",
                "A case-linked reservation requires a case attempt identity.",
            )

    async def add_reservation(
        self,
        session: AsyncSession,
        *,
        account: CaseBudgetParentAccount,
        amount: Decimal,
    ) -> None:
        if account.reserved_amount + account.settled_amount + amount > account.limit_amount:
            raise RunBudgetLedgerError(
                "CASE_BUDGET_INSUFFICIENT_FUNDS",
                "The parent case account cannot fund the full requested reservation.",
            )
        account.reserved_amount += amount
        session.add(account)

    async def settle(
        self,
        session: AsyncSession,
        *,
        account: CaseBudgetParentAccount,
        reservation_amount: Decimal,
        bill_amount: Decimal,
    ) -> None:
        if account.reserved_amount < reservation_amount:
            raise RunBudgetLedgerError(
                "CASE_BUDGET_ACCOUNT_INVARIANT",
                "The case account does not retain the attempt reservation.",
            )
        new_reserved = account.reserved_amount - reservation_amount
        new_settled = account.settled_amount + bill_amount
        if new_reserved > MAX_AMOUNT or new_settled > MAX_AMOUNT:
            raise RunBudgetLedgerError(
                "RUN_BUDGET_AMOUNT_PRECISION_EXCEEDED",
                "The case settlement cannot fit NUMERIC(20, 8).",
            )
        account.reserved_amount = new_reserved
        account.settled_amount = new_settled
        session.add(account)


class CaseBudgetLedger(RunBudgetLedger):
    """Run ledger configured with the evaluation-owned parent account bridge."""

    def __init__(self) -> None:
        super().__init__(case_parent=_EvaluationCaseBudgetParentPort())

    async def create_case_account(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        case_id: UUID,
        currency: str,
        limit_amount: Decimal,
        provider_egress_policy_version: str,
        provider_base_url: str | None,
    ) -> tuple[CaseBudgetAccountSnapshot, bool]:
        """Create a CNY parent account from already frozen evaluation evidence."""

        _validate_uuid(workspace_id, "workspace_id")
        _validate_uuid(case_id, "case_id")
        _validate_currency(currency)
        if currency != "CNY":
            raise ValueError("case budget currency must be CNY")
        normalized_limit = _validate_amount(
            limit_amount,
            field="limit_amount",
            allow_zero=False,
        )
        policy_version = _validate_required_text(
            provider_egress_policy_version,
            field="provider_egress_policy_version",
            maximum=64,
        )
        normalized_base_url = _validate_optional_text(
            provider_base_url,
            field="provider_base_url",
            maximum=MAX_PROVIDER_BASE_URL_LENGTH,
        )

        episode = await session.scalar(
            select(InteractiveEpisode).where(
                InteractiveEpisode.workspace_id == workspace_id,
                InteractiveEpisode.case_id == case_id,
            )
        )
        if episode is None:
            raise RunBudgetLedgerError(
                "CASE_BUDGET_EPISODE_NOT_FOUND",
                "The Interactive episode does not exist inside the Workspace.",
            )
        case_result = await session.scalar(
            select(EvaluationExperimentCaseResult).where(
                EvaluationExperimentCaseResult.workspace_id == workspace_id,
                EvaluationExperimentCaseResult.id == case_id,
            )
        )
        if case_result is None:
            raise RunBudgetLedgerError(
                "CASE_BUDGET_CASE_RESULT_NOT_FOUND",
                "The evaluation case does not exist inside the Workspace.",
            )
        variant = await session.scalar(
            select(EvaluationExperimentVariant).where(
                EvaluationExperimentVariant.workspace_id == workspace_id,
                EvaluationExperimentVariant.id == case_result.experiment_variant_id,
            )
        )
        if variant is None:
            raise RunBudgetLedgerError(
                "CASE_BUDGET_VARIANT_NOT_FOUND",
                "The frozen evaluation variant is unavailable.",
            )
        pricing = await session.scalar(
            select(PricingSnapshot).where(
                PricingSnapshot.workspace_id == workspace_id,
                PricingSnapshot.id == variant.pricing_snapshot_id,
            )
        )
        if (
            pricing is None
            or pricing.currency != "CNY"
            or pricing.content_hash != variant.pricing_snapshot_hash
        ):
            raise RunBudgetLedgerError(
                "CASE_BUDGET_PRICING_INVALID",
                "The frozen CNY pricing snapshot is unavailable or inconsistent.",
            )
        version = await session.scalar(
            select(AgentVersion).where(
                AgentVersion.workspace_id == workspace_id,
                AgentVersion.id == variant.agent_version_id,
            )
        )
        if version is None or version.resolved_spec_hash != variant.resolved_spec_hash:
            raise RunBudgetLedgerError(
                "CASE_BUDGET_MODEL_BINDING_INVALID",
                "The frozen AgentVersion is unavailable or inconsistent.",
            )
        try:
            spec = parse_frozen_agent_spec(version.resolved_spec, workspace_id=workspace_id)
        except Exception:
            raise RunBudgetLedgerError(
                "CASE_BUDGET_MODEL_BINDING_INVALID",
                "The frozen AgentVersion model binding is invalid.",
            ) from None
        primary = spec.model_plan.primary
        if primary.provider != pricing.provider or primary.model != pricing.model:
            raise RunBudgetLedgerError(
                "CASE_BUDGET_MODEL_BINDING_INVALID",
                "The primary model and frozen pricing snapshot do not match.",
            )

        statement = (
            postgresql_insert(CaseBudgetAccount.__table__)
            .values(
                workspace_id=workspace_id,
                case_id=case_id,
                experiment_variant_id=variant.id,
                currency=currency,
                limit_amount=normalized_limit,
                reserved_amount=Decimal("0"),
                settled_amount=Decimal("0"),
                pricing_snapshot_id=pricing.id,
                pricing_snapshot_hash=pricing.content_hash,
                provider=primary.provider,
                model=primary.model,
                provider_egress_policy_version=policy_version,
                provider_base_url=normalized_base_url,
                budget_policy_version=CASE_BUDGET_POLICY_VERSION,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    CaseBudgetAccount.__table__.c.workspace_id,
                    CaseBudgetAccount.__table__.c.case_id,
                ]
            )
            .returning(CaseBudgetAccount.__table__.c.case_id)
        )
        inserted = (await session.execute(statement)).scalar_one_or_none() is not None
        account = await session.scalar(
            select(CaseBudgetAccount)
            .where(
                CaseBudgetAccount.workspace_id == workspace_id,
                CaseBudgetAccount.case_id == case_id,
            )
            .execution_options(populate_existing=True)
        )
        if account is None:
            raise RunBudgetLedgerError(
                "CASE_BUDGET_ACCOUNT_UNAVAILABLE",
                "The case budget account could not be observed after creation.",
            )
        if (
            account.experiment_variant_id != variant.id
            or account.currency != currency
            or account.limit_amount != normalized_limit
            or account.pricing_snapshot_id != pricing.id
            or account.pricing_snapshot_hash != pricing.content_hash
            or account.provider != primary.provider
            or account.model != primary.model
            or account.provider_egress_policy_version != policy_version
            or account.provider_base_url != normalized_base_url
            or account.budget_policy_version != CASE_BUDGET_POLICY_VERSION
        ):
            raise RunBudgetLedgerError(
                "CASE_BUDGET_ACCOUNT_CONFLICT",
                "The case already has a different immutable budget account.",
            )
        return _case_account_snapshot(account), inserted

    async def create_case_run_binding(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        case_id: UUID,
        turn_index: int,
        evaluation_run_id: UUID,
        agent_run_id: UUID,
        dispatch_owner: str,
        dispatch_generation: int,
    ) -> tuple[CaseBudgetRunBindingSnapshot, bool]:
        """Bind one real Interactive turn Run to a case budget account."""

        _validate_uuid(workspace_id, "workspace_id")
        _validate_uuid(case_id, "case_id")
        _validate_uuid(evaluation_run_id, "evaluation_run_id")
        _validate_uuid(agent_run_id, "agent_run_id")
        _validate_non_negative_int(turn_index, "turn_index")
        _validate_non_negative_int(dispatch_generation, "dispatch_generation")
        owner = _validate_required_text(
            dispatch_owner,
            field="dispatch_owner",
            maximum=128,
        )

        await self._lock_case_account(session, workspace_id, case_id)
        turn = await session.scalar(
            select(InteractiveEpisodeTurn)
            .where(
                InteractiveEpisodeTurn.workspace_id == workspace_id,
                InteractiveEpisodeTurn.case_id == case_id,
                InteractiveEpisodeTurn.turn_index == turn_index,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if (
            turn is None
            or turn.agent_run_id != agent_run_id
            or turn.thread_turn_id is None
        ):
            raise RunBudgetLedgerError(
                "CASE_BUDGET_TURN_BINDING_INVALID",
                "The Interactive turn is missing or bound to another AgentRun.",
            )
        case_result = await session.scalar(
            select(EvaluationExperimentCaseResult).where(
                EvaluationExperimentCaseResult.workspace_id == workspace_id,
                EvaluationExperimentCaseResult.id == case_id,
            )
        )
        episode = await session.scalar(
            select(InteractiveEpisode).where(
                InteractiveEpisode.workspace_id == workspace_id,
                InteractiveEpisode.case_id == case_id,
            )
        )
        agent_run = await session.scalar(
            select(AgentRun).where(
                AgentRun.workspace_id == workspace_id,
                AgentRun.id == agent_run_id,
            )
        )
        if case_result is None or case_result.experiment_run_id != evaluation_run_id:
            raise RunBudgetLedgerError(
                "CASE_BUDGET_RUN_BINDING_INVALID",
                "The AgentRun does not match the frozen Interactive case.",
            )
        variant_agent_version_id = await session.scalar(
            select(EvaluationExperimentVariant.agent_version_id).where(
                EvaluationExperimentVariant.workspace_id == workspace_id,
                EvaluationExperimentVariant.id == case_result.experiment_variant_id,
            )
        )
        if (
            episode is None
            or agent_run is None
            or agent_run.thread_id != episode.thread_id
            or agent_run.thread_turn_id != turn.thread_turn_id
            or agent_run.agent_version_id != variant_agent_version_id
        ):
            raise RunBudgetLedgerError(
                "CASE_BUDGET_RUN_BINDING_INVALID",
                "The AgentRun does not match the frozen Interactive case.",
            )

        statement = (
            postgresql_insert(CaseBudgetRunBinding.__table__)
            .values(
                workspace_id=workspace_id,
                case_id=case_id,
                turn_index=turn_index,
                evaluation_run_id=evaluation_run_id,
                agent_run_id=agent_run_id,
                dispatch_owner=owner,
                dispatch_generation=dispatch_generation,
            )
            .on_conflict_do_nothing()
            .returning(CaseBudgetRunBinding.__table__.c.agent_run_id)
        )
        try:
            inserted = (await session.execute(statement)).scalar_one_or_none() is not None
        except IntegrityError:
            raise RunBudgetLedgerError(
                "CASE_BUDGET_RUN_BINDING_CONFLICT",
                "The case/turn or AgentRun is already bound elsewhere.",
            ) from None
        binding = await session.scalar(
            select(CaseBudgetRunBinding)
            .where(
                CaseBudgetRunBinding.workspace_id == workspace_id,
                CaseBudgetRunBinding.case_id == case_id,
                CaseBudgetRunBinding.turn_index == turn_index,
            )
            .execution_options(populate_existing=True)
        )
        if binding is None or (
            binding.evaluation_run_id != evaluation_run_id
            or binding.agent_run_id != agent_run_id
            or binding.dispatch_owner != owner
            or binding.dispatch_generation != dispatch_generation
        ):
            raise RunBudgetLedgerError(
                "CASE_BUDGET_RUN_BINDING_CONFLICT",
                "The case/turn already has a different immutable Run binding.",
            )
        return _case_run_binding_snapshot(binding), inserted

    async def get_case_account(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        case_id: UUID,
    ) -> CaseBudgetAccountSnapshot:
        """Read the parent case account without locking it."""

        _validate_uuid(workspace_id, "workspace_id")
        _validate_uuid(case_id, "case_id")
        account = await session.scalar(
            select(CaseBudgetAccount)
            .where(
                CaseBudgetAccount.workspace_id == workspace_id,
                CaseBudgetAccount.case_id == case_id,
            )
            .execution_options(populate_existing=True)
        )
        if account is None:
            raise RunBudgetLedgerError(
                "CASE_BUDGET_ACCOUNT_NOT_FOUND",
                "The case budget account was not found.",
            )
        return _case_account_snapshot(account)

    async def get_case_run_binding(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        case_id: UUID,
        turn_index: int,
    ) -> CaseBudgetRunBindingSnapshot:
        """Read one immutable case/turn binding."""

        _validate_uuid(workspace_id, "workspace_id")
        _validate_uuid(case_id, "case_id")
        _validate_non_negative_int(turn_index, "turn_index")
        binding = await session.scalar(
            select(CaseBudgetRunBinding)
            .where(
                CaseBudgetRunBinding.workspace_id == workspace_id,
                CaseBudgetRunBinding.case_id == case_id,
                CaseBudgetRunBinding.turn_index == turn_index,
            )
            .execution_options(populate_existing=True)
        )
        if binding is None:
            raise RunBudgetLedgerError(
                "CASE_BUDGET_RUN_BINDING_NOT_FOUND",
                "The case Run binding was not found.",
            )
        return _case_run_binding_snapshot(binding)

    async def record_response_observation(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        run_id: UUID,
        attempt_identity: str,
        observation_identity: str,
        provider: str,
        model: str,
        provider_egress_policy_version: str,
        provider_base_url: str | None,
        input_tokens: int | None,
        output_tokens: int | None,
        total_tokens: int | None,
        cached_tokens: int | None,
        cost_amount: Decimal | None,
        cost_currency: str | None,
    ) -> tuple[CaseBudgetResponseObservationSnapshot, bool]:
        """Append response evidence without changing either account total.

        The parent account is locked before its Run account and attempt.  This
        keeps observation writes on the same lock order as reserve, unknown,
        and provider-bill settlement while preserving the existing hold.
        """

        _validate_budget_key(workspace_id, run_id)
        _validate_attempt_identity(attempt_identity)
        normalized_observation_identity = _validate_evidence_identity(
            observation_identity
        )
        normalized_provider = _validate_required_text(
            provider,
            field="provider",
            maximum=100,
        )
        normalized_model = _validate_required_text(
            model,
            field="model",
            maximum=200,
        )
        normalized_policy_version = _validate_required_text(
            provider_egress_policy_version,
            field="provider_egress_policy_version",
            maximum=64,
        )
        normalized_base_url = _validate_optional_text(
            provider_base_url,
            field="provider_base_url",
            maximum=MAX_PROVIDER_BASE_URL_LENGTH,
        )
        normalized_input_tokens = _validate_optional_non_negative_int(
            input_tokens,
            "input_tokens",
        )
        normalized_output_tokens = _validate_optional_non_negative_int(
            output_tokens,
            "output_tokens",
        )
        normalized_total_tokens = _validate_optional_non_negative_int(
            total_tokens,
            "total_tokens",
        )
        normalized_cached_tokens = _validate_optional_non_negative_int(
            cached_tokens,
            "cached_tokens",
        )
        if (
            normalized_cached_tokens is not None
            and normalized_input_tokens is not None
            and normalized_cached_tokens > normalized_input_tokens
        ):
            raise ValueError("cached_tokens cannot exceed input_tokens")
        if (
            normalized_total_tokens is not None
            and normalized_input_tokens is not None
            and normalized_output_tokens is not None
            and normalized_total_tokens
            != normalized_input_tokens + normalized_output_tokens
        ):
            raise ValueError("total_tokens must equal input_tokens + output_tokens")

        if (cost_amount is None) != (cost_currency is None):
            raise ValueError("cost_amount and cost_currency must be provided together")
        normalized_cost_amount = None
        normalized_cost_currency = None
        if cost_amount is not None:
            normalized_cost_amount = _validate_amount(
                cost_amount,
                field="cost_amount",
                allow_zero=True,
            )
            _validate_currency(cost_currency)
            if cost_currency != "CNY":
                raise ValueError("cost_currency must be CNY")
            normalized_cost_currency = cost_currency

        case_account = await self._lock_case_account_for_run(
            session,
            workspace_id,
            run_id,
        )
        if case_account is None:
            raise RunBudgetLedgerError(
                "CASE_BUDGET_ACCOUNT_NOT_FOUND",
                "Response observation requires a case-linked Run account.",
            )
        frozen_case_account = cast(CaseBudgetAccount, case_account)
        account = await self._lock_account(session, workspace_id, run_id)
        if account.case_id != frozen_case_account.case_id:
            raise RunBudgetLedgerError(
                "RUN_BUDGET_ACCOUNT_INVARIANT",
                "The Run account case binding changed during observation.",
            )
        attempt = await self._lock_attempt(
            session,
            workspace_id=workspace_id,
            run_id=run_id,
            attempt_identity=attempt_identity,
        )
        if attempt.case_id != frozen_case_account.case_id:
            raise RunBudgetLedgerError(
                "RUN_BUDGET_ACCOUNT_INVARIANT",
                "The attempt case binding differs from its Run account.",
            )
        if attempt.case_attempt_identity is None or attempt.basis_identity is None:
            raise RunBudgetLedgerError(
                "RUN_BUDGET_ACCOUNT_INVARIANT",
                "The case attempt is missing its stable identity evidence.",
            )
        assert self._case_parent is not None
        await self._case_parent.validate_attempt_binding(
            session,
            workspace_id=workspace_id,
            run_id=run_id,
            case_id=frozen_case_account.case_id,
            case_attempt_identity=attempt.case_attempt_identity,
            basis_identity=attempt.basis_identity,
        )

        expected_values = {
            "observation_identity": normalized_observation_identity,
            "provider": normalized_provider,
            "model": normalized_model,
            "provider_egress_policy_version": normalized_policy_version,
            "provider_base_url": normalized_base_url,
            "input_tokens": normalized_input_tokens,
            "output_tokens": normalized_output_tokens,
            "total_tokens": normalized_total_tokens,
            "cached_tokens": normalized_cached_tokens,
            "cost_amount": normalized_cost_amount,
            "cost_currency": normalized_cost_currency,
        }
        if (
            normalized_provider != frozen_case_account.provider
            or normalized_model != frozen_case_account.model
            or normalized_policy_version
            != frozen_case_account.provider_egress_policy_version
            or normalized_base_url != frozen_case_account.provider_base_url
        ):
            raise RunBudgetLedgerError(
                "CASE_BUDGET_RESPONSE_BINDING_MISMATCH",
                "The response provider binding differs from the frozen case account.",
            )

        existing = await session.scalar(
            select(CaseBudgetResponseObservation).where(
                CaseBudgetResponseObservation.workspace_id == workspace_id,
                CaseBudgetResponseObservation.run_id == run_id,
                CaseBudgetResponseObservation.attempt_identity == attempt_identity,
            )
        )
        if existing is not None:
            _assert_matching_response_observation(existing, expected_values)
            return _response_observation_snapshot(existing), False

        statement = (
            postgresql_insert(CaseBudgetResponseObservation.__table__)
            .values(
                id=uuid4(),
                workspace_id=workspace_id,
                case_id=frozen_case_account.case_id,
                run_id=run_id,
                attempt_identity=attempt_identity,
                **expected_values,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    CaseBudgetResponseObservation.__table__.c.workspace_id,
                    CaseBudgetResponseObservation.__table__.c.run_id,
                    CaseBudgetResponseObservation.__table__.c.attempt_identity,
                ]
            )
            .returning(CaseBudgetResponseObservation.__table__.c.id)
        )
        inserted = (await session.execute(statement)).scalar_one_or_none() is not None
        observation = await session.scalar(
            select(CaseBudgetResponseObservation)
            .where(
                CaseBudgetResponseObservation.workspace_id == workspace_id,
                CaseBudgetResponseObservation.run_id == run_id,
                CaseBudgetResponseObservation.attempt_identity == attempt_identity,
            )
            .execution_options(populate_existing=True)
        )
        if observation is None:
            raise RunBudgetLedgerError(
                "CASE_BUDGET_RESPONSE_OBSERVATION_UNAVAILABLE",
                "The response observation could not be observed after insertion.",
            )
        if not inserted:
            _assert_matching_response_observation(observation, expected_values)
        return _response_observation_snapshot(observation), inserted


def _case_account_snapshot(account: CaseBudgetAccount) -> CaseBudgetAccountSnapshot:
    return CaseBudgetAccountSnapshot(
        workspace_id=account.workspace_id,
        case_id=account.case_id,
        experiment_variant_id=account.experiment_variant_id,
        currency=account.currency,
        limit_amount=account.limit_amount,
        reserved_amount=account.reserved_amount,
        settled_amount=account.settled_amount,
        pricing_snapshot_id=account.pricing_snapshot_id,
        pricing_snapshot_hash=account.pricing_snapshot_hash,
        provider=account.provider,
        model=account.model,
        provider_egress_policy_version=account.provider_egress_policy_version,
        provider_base_url=account.provider_base_url,
        budget_policy_version=account.budget_policy_version,
        created_at=account.created_at,
    )


def _case_run_binding_snapshot(
    binding: CaseBudgetRunBinding,
) -> CaseBudgetRunBindingSnapshot:
    return CaseBudgetRunBindingSnapshot(
        workspace_id=binding.workspace_id,
        case_id=binding.case_id,
        turn_index=binding.turn_index,
        evaluation_run_id=binding.evaluation_run_id,
        agent_run_id=binding.agent_run_id,
        dispatch_owner=binding.dispatch_owner,
        dispatch_generation=binding.dispatch_generation,
        created_at=binding.created_at,
    )


def _response_observation_snapshot(
    observation: CaseBudgetResponseObservation,
) -> CaseBudgetResponseObservationSnapshot:
    return CaseBudgetResponseObservationSnapshot(
        id=observation.id,
        workspace_id=observation.workspace_id,
        case_id=observation.case_id,
        run_id=observation.run_id,
        attempt_identity=observation.attempt_identity,
        observation_identity=observation.observation_identity,
        provider=observation.provider,
        model=observation.model,
        provider_egress_policy_version=(
            observation.provider_egress_policy_version
        ),
        provider_base_url=observation.provider_base_url,
        input_tokens=observation.input_tokens,
        output_tokens=observation.output_tokens,
        total_tokens=observation.total_tokens,
        cached_tokens=observation.cached_tokens,
        cost_amount=observation.cost_amount,
        cost_currency=observation.cost_currency,
        observed_at=observation.observed_at,
    )


def _assert_matching_response_observation(
    observation: CaseBudgetResponseObservation,
    expected: dict[str, object],
) -> None:
    if any(getattr(observation, field) != value for field, value in expected.items()):
        raise RunBudgetLedgerError(
            "CASE_BUDGET_RESPONSE_OBSERVATION_CONFLICT",
            "The attempt already has different response observation evidence.",
        )


__all__ = ["CaseBudgetLedger"]
