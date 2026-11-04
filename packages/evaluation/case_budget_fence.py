"""Authoritative dispatch fences for a future case-budget attempt adapter.

Call inside the reservation transaction, before taking the case/Run budget locks.
This module grants no dispatch permission by itself and performs no model call.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.agent_runtime.models import AgentRun
from packages.core.errors.exceptions import AgentHubError
from packages.evaluation.case_budget_models import CaseBudgetRunBinding
from packages.evaluation.interactive_lifecycle import CaseExecutionIdentity, InteractiveLifecycle
from packages.evaluation.models import (
    EvaluationExperimentCaseResult,
    EvaluationExperimentRun,
    EvaluationExperimentVariant,
    InteractiveEpisode,
    InteractiveEpisodeTurn,
)
from packages.model_gateway.run_budget_adapter import ModelAttemptBudgetError
from packages.threads.models import ThreadTurn


async def lock_case_budget_dispatch(
    session: AsyncSession,
    *,
    identity: CaseExecutionIdentity,
    turn_index: int,
    agent_run_id: UUID,
    execution_owner: str,
) -> AgentRun:
    """Lock and re-read the evaluation lease, turn binding and AgentRun owner.

    Locks follow the existing interactive dispatch order: evaluation Run, case,
    episode, turn, ThreadTurn, AgentRun. The caller next locks budget parent,
    child and attempt. Cached ORM objects cannot authorize an expired executor.
    A valid fence authorizes only this reservation transaction; it cannot prove
    exactly-once remote delivery after commit.
    """
    if not session.in_transaction():
        raise ModelAttemptBudgetError(
            "CASE_BUDGET_TRANSACTION_REQUIRED", "A reservation transaction is required."
        )
    if (
        not isinstance(identity, CaseExecutionIdentity)
        or type(turn_index) is not int
        or turn_index < 0
        or not isinstance(agent_run_id, UUID)
        or not isinstance(execution_owner, str)
        or not execution_owner.strip()
    ):
        raise ModelAttemptBudgetError(
            "CASE_BUDGET_EXECUTION_FENCE_INVALID", "The execution fence is invalid."
        )

    async def locked(model, *conditions):
        return await session.scalar(
            select(model)
            .where(*conditions)
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    parent = await locked(
        EvaluationExperimentRun,
        EvaluationExperimentRun.workspace_id == identity.workspace_id,
        EvaluationExperimentRun.id == identity.run_id,
    )
    case = await locked(
        EvaluationExperimentCaseResult,
        EvaluationExperimentCaseResult.workspace_id == identity.workspace_id,
        EvaluationExperimentCaseResult.id == identity.case_id,
    )
    episode = await locked(
        InteractiveEpisode,
        InteractiveEpisode.workspace_id == identity.workspace_id,
        InteractiveEpisode.case_id == identity.case_id,
    )
    turn = await locked(
        InteractiveEpisodeTurn,
        InteractiveEpisodeTurn.workspace_id == identity.workspace_id,
        InteractiveEpisodeTurn.case_id == identity.case_id,
        InteractiveEpisodeTurn.turn_index == turn_index,
    )
    now = datetime.now(UTC)
    if (
        parent is None
        or parent.status != "RUNNING"
        or parent.lease_owner != identity.owner
        or parent.lease_generation != identity.generation
        or not _active(parent.lease_expires_at, now)
        or case is None
        or case.experiment_run_id != identity.run_id
        or case.status != "RUNNING"
        or case.lease_owner != identity.owner
        or case.lease_generation != identity.generation
        or case.experiment_variant_id != identity.variant_id
        or case.dataset_item_id != identity.item_id
        or case.repetition_index != identity.repetition_index
        or episode is None
        or episode.execution_status != "RUNNING"
        or turn is None
        or turn.dispatch_status != "DISPATCH_INTENT"
        or turn.agent_run_id != agent_run_id
        or turn.thread_turn_id is None
    ):
        raise _lost()
    try:
        InteractiveLifecycle._require_not_cleaned(episode)
    except AgentHubError:
        raise _lost() from None

    thread_turn = await locked(
        ThreadTurn,
        ThreadTurn.workspace_id == identity.workspace_id,
        ThreadTurn.id == turn.thread_turn_id,
    )
    agent_run = await locked(
        AgentRun,
        AgentRun.workspace_id == identity.workspace_id,
        AgentRun.id == agent_run_id,
    )
    variant = await session.scalar(
        select(EvaluationExperimentVariant)
        .where(
            EvaluationExperimentVariant.workspace_id == identity.workspace_id,
            EvaluationExperimentVariant.id == identity.variant_id,
        )
        .execution_options(populate_existing=True)
    )
    binding = await session.scalar(
        select(CaseBudgetRunBinding)
        .where(
            CaseBudgetRunBinding.workspace_id == identity.workspace_id,
            CaseBudgetRunBinding.case_id == identity.case_id,
            CaseBudgetRunBinding.turn_index == turn_index,
        )
        .execution_options(populate_existing=True)
    )
    # Acquiring a later lock can wait past either lease's expiration.
    now = datetime.now(UTC)
    if (
        not _active(parent.lease_expires_at, now)
        or binding is None
        or binding.evaluation_run_id != identity.run_id
        or binding.agent_run_id != agent_run_id
        or binding.dispatch_owner != identity.owner
        or binding.dispatch_generation != identity.generation
        or thread_turn is None
        or thread_turn.thread_id != episode.thread_id
        or thread_turn.client_token != turn.client_token
        or thread_turn.agent_run_id != agent_run_id
        or agent_run is None
        or agent_run.status != "RUNNING"
        or agent_run.execution_owner != execution_owner
        or not _active(agent_run.execution_lease_expires_at, now)
        or agent_run.created_by != parent.created_by
        or agent_run.thread_id != episode.thread_id
        or agent_run.thread_id != episode.reserved_thread_id
        or agent_run.thread_turn_id != thread_turn.id
        or variant is None
        or not InteractiveLifecycle._agent_run_matches_frozen_variant(agent_run, variant)
        or not InteractiveLifecycle._input_matches_reserved_turn(
            reserved_hash=turn.input_hash,
            thread_turn=thread_turn,
            agent_run_input=agent_run.input_text,
        )
    ):
        raise _lost()
    return agent_run


def _active(expiry: datetime | None, now: datetime) -> bool:
    if not isinstance(expiry, datetime):
        return False
    return (expiry if expiry.tzinfo else expiry.replace(tzinfo=UTC)) > now


def _lost() -> ModelAttemptBudgetError:
    return ModelAttemptBudgetError(
        "CASE_BUDGET_EXECUTION_FENCE_INVALID", "The case execution is no longer current."
    )
