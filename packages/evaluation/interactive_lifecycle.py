"""Durable lifecycle and fencing for interactive evaluation executions.

This module owns database identity and transaction boundaries only. It never
invokes the evaluation runner, starts a model, retries an action, or imports a
driver. Driver and runner code may depend on this module; the reverse dependency
would make retries and cancellation difficult to reason about.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.models import AgentRun, AgentVersion, RunStep
from packages.approvals.contracts import (
    ApprovalDecisionStatus,
    ApprovalExecutionStatus,
)
from packages.approvals.models import Approval
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.errors.exceptions import AgentHubError
from packages.evaluation.models import (
    EvaluationDatasetItem,
    EvaluationExperimentCaseResult,
    EvaluationExperimentRun,
    EvaluationExperimentVariant,
    InteractiveEpisode,
    InteractiveEpisodeTurn,
)
from packages.threads.models import AgentThread, ThreadTurn
from packages.tools.models import Customer, Ticket

INTERACTIVE_DRIVER_VERSION = "interactive-driver-v2"
INTERACTIVE_ENVIRONMENT_VERSION = "environment-v2"
INTERACTIVE_SIMULATOR_VERSION = "scripted-disclosure-v1"
INTERACTIVE_LIFECYCLE_VERSION = "interactive-lifecycle-v1"

_LIFECYCLE_NAMESPACE = UUID("f30af0f4-0d32-5df0-9c49-9343e8576a95")
_FIXTURE_REF = "support_customer"
_ENVIRONMENT_KIND = "support_fixture"
_CLEANUP_RETRY_DELAY = timedelta(seconds=60)
_PARTIAL_BUSINESS_ASSERTION_SOURCE = "support_fixture.assert_state.partial"
_DISPATCHED_TURN_STATUSES = {
    "DISPATCH_INTENT",
    "TERMINAL",
    "UNKNOWN_OUTCOME",
    "RECOVERY_REQUIRED",
}
_FINAL_AGENT_STATUSES = {"SUCCEEDED", "FAILED", "CANCELLED"}
_ACTIVE_AGENT_STATUSES = {"RUNNING", "WAITING_APPROVAL", "CANCEL_REQUESTED"}
_SAFE_TURN_EVIDENCE_KEYS = {
    "agent_output_hash",
    "agent_status",
    "aggregate_billing_complete",
    "business_assertion",
    "cached_tokens",
    "cost_amount",
    "cost_currency",
    "executed_tool_sequence",
    "execution_evidence",
    "evidence_source",
    "failure_code",
    "input_tokens",
    "output_tokens",
    "latency_ms",
    "observed_agent_status",
    "proposed_tool_sequence",
    "run_id",
    "status",
    "thread_turn_id",
    "tool_sequence",
    "total_tokens",
    "trajectory",
    "usage",
    "user_input_hash",
    "user_input_length",
}
_UNSAFE_RECORD_KEYS = {
    "canary",
    "expected",
    "final_output",
    "private_facts",
    "raw_response",
    "ticket_subject",
    "ticket_subjects",
    "triggers",
    "user_input",
}
_OBSERVATION_KEYS = {
    "observation",
    "agent_run_id",
    "latency_ms",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_tokens",
    "cost_amount",
    "cost_currency",
    "aggregate_billing_complete",
    "failure_code",
    "safe_failure_message",
}


@dataclass(frozen=True, slots=True)
class CaseExecutionIdentity:
    """Fencing identity passed from the runner to an interactive driver."""

    workspace_id: UUID
    run_id: UUID
    case_id: UUID
    variant_id: UUID
    item_id: UUID
    repetition_index: int
    owner: str
    generation: int

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, UUID)
            for value in (
                self.workspace_id,
                self.run_id,
                self.case_id,
                self.variant_id,
                self.item_id,
            )
        ):
            raise TypeError("case execution identities must be UUIDs")
        if (
            isinstance(self.repetition_index, bool)
            or not isinstance(self.repetition_index, int)
            or self.repetition_index < 0
        ):
            raise ValueError("repetition_index must be a non-negative integer")
        if not isinstance(self.owner, str) or not self.owner.strip():
            raise ValueError("owner must be non-empty")
        if len(self.owner) > 128:
            raise ValueError("owner is too long")
        if (
            isinstance(self.generation, bool)
            or not isinstance(self.generation, int)
            or self.generation < 0
        ):
            raise ValueError("generation must be a non-negative integer")


def _now() -> datetime:
    return datetime.now(UTC)


def _error(code: str, message: str, status_code: int = 409) -> AgentHubError:
    return AgentHubError(code, message, status_code)


def _require_hash(value: str, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field_name} must be lowercase SHA-256")
    return value


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("evidence contains a non-finite number")
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError("evidence keys must be strings")
            if key.casefold() in _UNSAFE_RECORD_KEYS:
                raise ValueError("evidence contains a forbidden field")
            normalized[key] = _json_safe(child)
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_safe(child) for child in value]
    raise ValueError("evidence is not JSON safe")


def _safe_dict(value: Mapping[str, Any]) -> dict[str, Any]:
    normalized = _json_safe(value)
    if not isinstance(normalized, dict):
        raise ValueError("evidence must be an object")
    return normalized


def _safe_turn_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    normalized = _safe_dict(value)
    projected = {key: child for key, child in normalized.items() if key in _SAFE_TURN_EVIDENCE_KEYS}
    business_assertion = projected.get("business_assertion")
    if business_assertion is not None:
        _validate_partial_business_assertion(business_assertion)
    return projected


def _validate_partial_business_assertion(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {
        "passed",
        "reason",
        "observed",
        "source",
    }:
        raise ValueError("business_assertion has an invalid shape")
    if not isinstance(value["passed"], bool):
        raise ValueError("business_assertion.passed must be boolean")
    if value["reason"] is not None and not isinstance(value["reason"], str):
        raise ValueError("business_assertion.reason must be string or null")
    if value["source"] != _PARTIAL_BUSINESS_ASSERTION_SOURCE:
        raise ValueError("business_assertion source is invalid")
    observed = value["observed"]
    if not isinstance(observed, dict) or set(observed) != {
        "ticket_count",
        "ticket_statuses",
    }:
        raise ValueError("business_assertion.observed has an invalid shape")
    ticket_count = observed["ticket_count"]
    if isinstance(ticket_count, bool) or not isinstance(ticket_count, int) or ticket_count < 0:
        raise ValueError("business_assertion ticket_count is invalid")
    ticket_statuses = observed["ticket_statuses"]
    if not isinstance(ticket_statuses, list) or not all(
        isinstance(status, str) for status in ticket_statuses
    ):
        raise ValueError("business_assertion ticket_statuses is invalid")


def _terminal_status(value: Any) -> str | None:
    status = str(value or "").upper()
    if status == "UNKNOWN_OUTCOME":
        return "NEEDS_ATTENTION"
    if status in _FINAL_AGENT_STATUSES | {"NEEDS_ATTENTION"}:
        return status
    return None


class InteractiveLifecycle:
    """Persisted, fenced lifecycle service used by the interactive driver."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self.session_factory = session_factory

    async def reserve(
        self,
        identity: CaseExecutionIdentity,
        *,
        seed: int,
        fixture_ref: str,
        input_hash: str,
        policy_hash: str,
    ) -> InteractiveEpisode:
        if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
            raise ValueError("seed must be a non-negative integer")
        if fixture_ref != _FIXTURE_REF:
            raise ValueError("only support_customer fixtures are supported")
        _require_hash(input_hash, "input_hash")
        _require_hash(policy_hash, "policy_hash")
        fixture_hash = canonical_json_hash(
            {
                "fixture_ref": fixture_ref,
                "seed": seed,
            }
        )

        async with self.session_factory() as session:
            run, case = await self._lock_current_parent(session, identity)
            await self._validate_frozen_item_hashes(
                session,
                case=case,
                dataset_version_id=run.dataset_version_id,
                input_hash=input_hash,
                policy_hash=policy_hash,
            )
            episode = await self._episode_for_update(
                session,
                workspace_id=identity.workspace_id,
                case_id=identity.case_id,
            )
            if episode is not None:
                self._require_same_configuration(
                    episode,
                    seed=seed,
                    fixture_ref=fixture_ref,
                    input_hash=input_hash,
                    policy_hash=policy_hash,
                    fixture_hash=fixture_hash,
                )
                if await self._has_dispatched_turn(session, episode):
                    raise _error(
                        "INTERACTIVE_EPISODE_ALREADY_DISPATCHED",
                        "A dispatched interactive episode cannot restart.",
                    )
                if episode.execution_status != "PREPARING":
                    raise _error(
                        "INTERACTIVE_EPISODE_ALREADY_STARTED",
                        "A started interactive episode cannot be reserved again.",
                    )
                await session.commit()
                await session.refresh(episode)
                return episode
            else:
                episode = InteractiveEpisode(
                    case_id=identity.case_id,
                    workspace_id=identity.workspace_id,
                    instance_id=identity.case_id,
                    reserved_thread_id=uuid5(
                        _LIFECYCLE_NAMESPACE,
                        f"thread:{identity.workspace_id}:{identity.case_id}",
                    ),
                    thread_id=None,
                    seed=seed,
                    fixture_ref=fixture_ref,
                    fixture_hash=fixture_hash,
                    env_kind=_ENVIRONMENT_KIND,
                    env_version=INTERACTIVE_ENVIRONMENT_VERSION,
                    simulator_version=INTERACTIVE_SIMULATOR_VERSION,
                    driver_version=INTERACTIVE_DRIVER_VERSION,
                    input_hash=input_hash,
                    policy_hash=policy_hash,
                    customer_id=None,
                    customer_ref=None,
                    fixture_created_at=None,
                    prepare_stage="RESERVED",
                    execution_status="PREPARING",
                    cleanup_status="PENDING",
                    cleanup_attempts=0,
                )
                session.add(episode)
            assert case.workspace_id == identity.workspace_id
            await session.commit()
            await session.refresh(episode)
            return episode

    async def create_fixture(
        self,
        identity: CaseExecutionIdentity,
    ) -> InteractiveEpisode:
        async with self.session_factory() as session:
            _, _, episode = await self._lock_current_episode(session, identity)
            self._require_not_cleaned(episode)
            if episode.customer_id is not None:
                await session.commit()
                await session.refresh(episode)
                return episode
            if episode.execution_status != "PREPARING":
                raise _error(
                    "INTERACTIVE_FIXTURE_STATE_INVALID",
                    "The interactive fixture can only be created while preparing.",
                )
            if await self._has_dispatched_turn(session, episode):
                raise _error(
                    "INTERACTIVE_EPISODE_ALREADY_DISPATCHED",
                    "A dispatched interactive episode cannot create another fixture.",
                )

            customer_id = uuid5(
                _LIFECYCLE_NAMESPACE,
                f"customer:{identity.workspace_id}:{identity.case_id}",
            )
            customer_ref = self._customer_ref(episode)
            existing = await session.scalar(
                select(Customer.id).where(
                    Customer.workspace_id == identity.workspace_id,
                    or_(
                        Customer.id == customer_id,
                        Customer.customer_ref == customer_ref,
                    ),
                )
            )
            if existing is not None:
                raise _error(
                    "INTERACTIVE_FIXTURE_IDENTITY_CONFLICT",
                    "The controlled fixture identity is already owned elsewhere.",
                )
            session.add(
                Customer(
                    id=customer_id,
                    workspace_id=identity.workspace_id,
                    customer_ref=customer_ref,
                    name=f"AE6B Fixture {episode.instance_id.hex[:8]}",
                    email=f"{customer_ref.lower()}@example.test",
                )
            )
            now = _now()
            episode.customer_id = customer_id
            episode.customer_ref = customer_ref
            episode.fixture_created_at = now
            episode.prepare_stage = "FIXTURE_READY"
            episode.updated_at = now
            await session.commit()
            await session.refresh(episode)
            return episode

    async def ensure_current(self, identity: CaseExecutionIdentity) -> None:
        async with self.session_factory() as session:
            _, _, episode = await self._lock_current_episode(session, identity)
            self._require_not_cleaned(episode)
            if episode.execution_status not in {"PREPARING", "RUNNING"}:
                raise _error(
                    "INTERACTIVE_EPISODE_NOT_CURRENT",
                    "The interactive episode is no longer executing.",
                )
            await session.rollback()

    async def bind_thread(
        self,
        identity: CaseExecutionIdentity,
        *,
        thread_id: UUID,
    ) -> None:
        async with self.session_factory() as session:
            run, case, episode = await self._lock_current_episode(session, identity)
            self._require_not_cleaned(episode)
            if thread_id != episode.reserved_thread_id:
                raise _error(
                    "INTERACTIVE_THREAD_BINDING_INVALID",
                    "The Thread must use the episode's reserved UUID.",
                )
            if episode.thread_id is not None:
                if episode.thread_id != thread_id:
                    raise _error(
                        "INTERACTIVE_THREAD_BINDING_CONFLICT",
                        "The episode is already bound to another Thread.",
                    )
                await session.commit()
                return
            if episode.execution_status != "PREPARING":
                raise _error(
                    "INTERACTIVE_THREAD_BINDING_STATE_INVALID",
                    "The Thread can only be bound while preparing.",
                )
            if episode.customer_id is None:
                raise _error(
                    "INTERACTIVE_FIXTURE_NOT_READY",
                    "The fixture must be created before binding its Thread.",
                )
            thread = await session.scalar(
                select(AgentThread)
                .where(
                    AgentThread.workspace_id == identity.workspace_id,
                    AgentThread.id == thread_id,
                )
                .with_for_update()
            )
            if thread is None:
                raise _error(
                    "INTERACTIVE_THREAD_BINDING_INVALID",
                    "The reserved Thread does not exist.",
                )
            agent_version_id = await self._case_agent_version_id(
                session, workspace_id=identity.workspace_id, case=case
            )
            version = await session.scalar(
                select(AgentVersion).where(
                    AgentVersion.workspace_id == identity.workspace_id,
                    AgentVersion.id == agent_version_id,
                )
            )
            if (
                version is None
                or thread.agent_id != version.agent_id
                or thread.created_by != run.created_by
            ):
                raise _error(
                    "INTERACTIVE_THREAD_BINDING_INVALID",
                    "The Thread does not match the frozen case owner or Agent.",
                )
            episode.thread_id = thread_id
            episode.prepare_stage = "THREAD_BOUND"
            episode.updated_at = _now()
            await session.commit()

    async def reserve_turn(
        self,
        identity: CaseExecutionIdentity,
        *,
        turn_index: int,
        input_hash: str,
    ) -> InteractiveEpisodeTurn:
        if isinstance(turn_index, bool) or not isinstance(turn_index, int) or turn_index < 0:
            raise ValueError("turn_index must be a non-negative integer")
        _require_hash(input_hash, "input_hash")
        client_token = self._client_token(identity.case_id, turn_index)

        async with self.session_factory() as session:
            _, _, episode = await self._lock_current_episode(session, identity)
            self._require_not_cleaned(episode)
            if episode.execution_status not in {"PREPARING", "RUNNING"}:
                raise _error(
                    "INTERACTIVE_TURN_STATE_INVALID",
                    "Turns cannot be reserved for this episode state.",
                )
            turn = await session.scalar(
                select(InteractiveEpisodeTurn)
                .where(
                    InteractiveEpisodeTurn.workspace_id == identity.workspace_id,
                    InteractiveEpisodeTurn.case_id == identity.case_id,
                    InteractiveEpisodeTurn.turn_index == turn_index,
                )
                .with_for_update()
            )
            if turn is not None:
                if turn.client_token != client_token or turn.input_hash != input_hash:
                    raise _error(
                        "INTERACTIVE_TURN_IDEMPOTENCY_CONFLICT",
                        "The turn index is already bound to different input.",
                    )
                await session.commit()
                await session.refresh(turn)
                return turn
            turn = InteractiveEpisodeTurn(
                workspace_id=identity.workspace_id,
                case_id=identity.case_id,
                turn_index=turn_index,
                client_token=client_token,
                input_hash=input_hash,
                dispatch_status="RESERVED",
            )
            session.add(turn)
            if episode.execution_status == "PREPARING":
                episode.prepare_stage = "TURNS_RESERVED"
            episode.updated_at = _now()
            await session.commit()
            await session.refresh(turn)
            return turn

    async def bind_turn(
        self,
        identity: CaseExecutionIdentity,
        *,
        turn_index: int,
        turn_id: UUID,
        run_id: UUID,
    ) -> None:
        if not isinstance(turn_id, UUID) or not isinstance(run_id, UUID):
            raise TypeError("turn_id and run_id must be UUIDs")
        async with self.session_factory() as session:
            run, case, episode = await self._lock_current_episode(session, identity)
            self._require_not_cleaned(episode)
            turn = await self._turn_for_update(
                session,
                workspace_id=identity.workspace_id,
                case_id=identity.case_id,
                turn_index=turn_index,
            )
            if turn.thread_turn_id is not None or turn.agent_run_id is not None:
                if turn.thread_turn_id == turn_id and turn.agent_run_id == run_id:
                    await session.commit()
                    return
                raise _error(
                    "INTERACTIVE_TURN_BINDING_CONFLICT",
                    "The turn is already bound to another ThreadTurn or Run.",
                )
            if turn.dispatch_status != "RESERVED":
                raise _error(
                    "INTERACTIVE_TURN_STATE_INVALID",
                    "Only a reserved turn can be bound.",
                )
            if episode.thread_id is None:
                raise _error(
                    "INTERACTIVE_THREAD_NOT_BOUND",
                    "The episode Thread must be bound before its turns.",
                )
            if episode.execution_status not in {"PREPARING", "RUNNING"}:
                raise _error(
                    "INTERACTIVE_TURN_BINDING_STATE_INVALID",
                    "The episode is not accepting turn bindings.",
                )

            thread_turn = await session.scalar(
                select(ThreadTurn)
                .where(
                    ThreadTurn.workspace_id == identity.workspace_id,
                    ThreadTurn.id == turn_id,
                )
                .with_for_update()
            )
            if (
                thread_turn is None
                or thread_turn.thread_id != episode.thread_id
                or thread_turn.client_token != turn.client_token
                or not self._reserved_hash_matches_turn_input(
                    reserved_hash=turn.input_hash,
                    thread_turn=thread_turn,
                )
            ):
                raise _error(
                    "INTERACTIVE_THREAD_TURN_BINDING_INVALID",
                    "The ThreadTurn does not match the reserved lifecycle turn.",
                )
            agent_run = await session.scalar(
                select(AgentRun)
                .where(
                    AgentRun.workspace_id == identity.workspace_id,
                    AgentRun.id == run_id,
                )
                .with_for_update()
            )
            variant = await self._case_variant(
                session, workspace_id=identity.workspace_id, case=case
            )
            if (
                agent_run is None
                or agent_run.thread_id != episode.thread_id
                or agent_run.thread_id != episode.reserved_thread_id
                or agent_run.thread_turn_id != turn_id
                or not self._agent_run_matches_frozen_variant(agent_run, variant)
                or agent_run.created_by != run.created_by
                or agent_run.status != "RUNNING"
                or not self._input_matches_reserved_turn(
                    reserved_hash=turn.input_hash,
                    thread_turn=thread_turn,
                    agent_run_input=agent_run.input_text,
                )
            ):
                raise _error(
                    "INTERACTIVE_AGENT_RUN_BINDING_INVALID",
                    "The AgentRun does not match the frozen case and ThreadTurn.",
                )
            if thread_turn.agent_run_id is not None and thread_turn.agent_run_id != run_id:
                raise _error(
                    "INTERACTIVE_THREAD_TURN_BINDING_CONFLICT",
                    "The ThreadTurn is already bound to another AgentRun.",
                )
            if thread_turn.agent_run_id is None:
                thread_turn.agent_run_id = run_id
            turn.thread_turn_id = turn_id
            turn.agent_run_id = run_id
            turn.dispatch_status = "BOUND"
            turn.updated_at = _now()
            if turn_index == 0:
                if case.agent_run_id is None:
                    case.agent_run_id = run_id
                elif case.agent_run_id != run_id:
                    raise _error(
                        "INTERACTIVE_CASE_RUN_BINDING_CONFLICT",
                        "The case is already linked to another first AgentRun.",
                    )
            await session.commit()

    async def dispatch(
        self,
        identity: CaseExecutionIdentity,
        *,
        turn_index: int,
    ) -> None:
        async with self.session_factory() as session:
            parent_run, case, episode = await self._lock_current_episode(session, identity)
            self._require_not_cleaned(episode)
            if episode.execution_status not in {"PREPARING", "RUNNING"}:
                raise _error(
                    "INTERACTIVE_DISPATCH_STATE_INVALID",
                    "The episode is not accepting a new dispatch.",
                )
            turn = await self._turn_for_update(
                session,
                workspace_id=identity.workspace_id,
                case_id=identity.case_id,
                turn_index=turn_index,
            )
            if turn.dispatch_status != "BOUND":
                raise _error(
                    "INTERACTIVE_DISPATCH_ALREADY_RECORDED",
                    "The interactive turn is not in the dispatchable BOUND state.",
                )
            if turn.thread_turn_id is None or turn.agent_run_id is None:
                raise _error(
                    "INTERACTIVE_DISPATCH_BINDING_MISSING",
                    "A turn must bind its ThreadTurn and AgentRun before dispatch.",
                )
            thread_turn = await session.scalar(
                select(ThreadTurn)
                .where(
                    ThreadTurn.workspace_id == identity.workspace_id,
                    ThreadTurn.id == turn.thread_turn_id,
                )
                .with_for_update()
            )
            agent_run = await session.scalar(
                select(AgentRun)
                .where(
                    AgentRun.workspace_id == identity.workspace_id,
                    AgentRun.id == turn.agent_run_id,
                )
                .with_for_update()
            )
            variant = await self._case_variant(
                session, workspace_id=identity.workspace_id, case=case
            )
            if (
                thread_turn is None
                or thread_turn.thread_id != episode.thread_id
                or thread_turn.client_token != turn.client_token
                or thread_turn.agent_run_id != turn.agent_run_id
                or agent_run is None
                or agent_run.thread_id != episode.thread_id
                or agent_run.thread_id != episode.reserved_thread_id
                or agent_run.thread_turn_id != thread_turn.id
                or not self._agent_run_matches_frozen_variant(agent_run, variant)
                or agent_run.created_by != parent_run.created_by
                or agent_run.status != "RUNNING"
                or not self._input_matches_reserved_turn(
                    reserved_hash=turn.input_hash,
                    thread_turn=thread_turn,
                    agent_run_input=agent_run.input_text,
                )
            ):
                raise _error(
                    "INTERACTIVE_DISPATCH_BINDING_INVALID",
                    "The bound ThreadTurn or AgentRun no longer matches the frozen case.",
                )
            now = _now()
            turn.dispatch_status = "DISPATCH_INTENT"
            turn.dispatch_intent_at = now
            turn.updated_at = now
            episode.execution_status = "RUNNING"
            episode.prepare_stage = "TURNS_DISPATCHED"
            episode.updated_at = now
            await session.commit()

    async def record_turn(
        self,
        identity: CaseExecutionIdentity,
        *,
        turn_index: int,
        evidence: dict[str, Any],
    ) -> None:
        normalized = _safe_turn_evidence(evidence)
        evidence_hash = canonical_json_hash(normalized)
        async with self.session_factory() as session:
            _, _, episode = await self._lock_current_episode(session, identity)
            self._require_not_cleaned(episode)
            if episode.execution_status not in {"PREPARING", "RUNNING"}:
                raise _error(
                    "INTERACTIVE_TURN_RECORD_STATE_INVALID",
                    "The episode is not accepting turn results.",
                )
            turn = await self._turn_for_update(
                session,
                workspace_id=identity.workspace_id,
                case_id=identity.case_id,
                turn_index=turn_index,
            )
            if turn.evidence_hash is not None:
                if turn.evidence_hash == evidence_hash and turn.evidence == normalized:
                    await session.commit()
                    return
                raise _error(
                    "INTERACTIVE_TURN_EVIDENCE_CONFLICT",
                    "The turn already retains different evidence.",
                )
            if turn.dispatch_status != "DISPATCH_INTENT":
                raise _error(
                    "INTERACTIVE_TURN_STATE_INVALID",
                    "Only a dispatched turn can record terminal evidence.",
                )
            if turn.agent_run_id is None:
                raise _error(
                    "INTERACTIVE_AGENT_RUN_NOT_BOUND",
                    "A terminal turn requires its AgentRun binding.",
                )
            agent_run = await session.scalar(
                select(AgentRun)
                .where(
                    AgentRun.workspace_id == identity.workspace_id,
                    AgentRun.id == turn.agent_run_id,
                )
                .with_for_update()
            )
            if agent_run is None:
                raise _error(
                    "INTERACTIVE_AGENT_RUN_NOT_FOUND",
                    "The bound AgentRun was not found.",
                )
            evidence_status = (
                normalized.get("observed_agent_status")
                or normalized.get("agent_status")
                or normalized.get("status")
            )
            terminal_status = _terminal_status(agent_run.status)
            if terminal_status is None:
                raise _error(
                    "INTERACTIVE_TURN_NOT_TERMINAL",
                    "The AgentRun has not reached a terminal storage state.",
                )
            supplied_status = _terminal_status(evidence_status)
            if evidence_status and supplied_status != terminal_status:
                raise _error(
                    "INTERACTIVE_TURN_STATUS_CONFLICT",
                    "The turn evidence conflicts with the persisted AgentRun status.",
                )
            unknown_execution = await self._has_unknown_execution(
                session,
                workspace_id=identity.workspace_id,
                run_id=agent_run.id,
            )
            effective_terminal_status = "NEEDS_ATTENTION" if unknown_execution else terminal_status
            if unknown_execution:
                agent_run.status = "NEEDS_ATTENTION"
                agent_run.failure_code = agent_run.failure_code or "INTERACTIVE_UNKNOWN_OUTCOME"
            now = _now()
            turn.evidence = normalized
            turn.evidence_hash = evidence_hash
            turn.terminal_status = effective_terminal_status
            turn.failure_code = (
                str(normalized.get("failure_code") or agent_run.failure_code or "") or None
            )
            turn.completed_at = now
            turn.updated_at = now
            if effective_terminal_status == "NEEDS_ATTENTION":
                turn.dispatch_status = "UNKNOWN_OUTCOME"
                episode.execution_status = "RECOVERY_REQUIRED"
                episode.termination_reason = "UNKNOWN_OUTCOME"
            else:
                turn.dispatch_status = "TERMINAL"
                if episode.execution_status == "PREPARING":
                    episode.execution_status = "RUNNING"
            episode.updated_at = now
            await session.commit()

    async def retain_assertion(
        self,
        identity: CaseExecutionIdentity,
        *,
        assertion: dict[str, Any],
        source: str,
    ) -> None:
        normalized = _safe_dict(assertion)
        if not isinstance(source, str) or not source.strip() or len(source) > 128:
            raise ValueError("assertion source is invalid")
        assertion_hash = canonical_json_hash(normalized)
        async with self.session_factory() as session:
            _, _, episode = await self._lock_current_episode(session, identity)
            self._require_not_cleaned(episode)
            if episode.execution_status not in {"PREPARING", "RUNNING"}:
                raise _error(
                    "INTERACTIVE_ASSERTION_STATE_INVALID",
                    "The episode is not accepting assertion evidence.",
                )
            if episode.assertion_hash is not None:
                if (
                    episode.assertion_hash == assertion_hash
                    and episode.assertion == normalized
                    and episode.assertion_source == source
                ):
                    await session.commit()
                    return
                raise _error(
                    "INTERACTIVE_ASSERTION_EVIDENCE_CONFLICT",
                    "The episode already retains a different assertion.",
                )
            if episode.cleanup_status == "TEARDOWN_COMPLETE":
                raise _error(
                    "INTERACTIVE_CLEANUP_ALREADY_COMPLETE",
                    "Assertion evidence cannot be added after fixture cleanup.",
                )
            now = _now()
            episode.assertion = normalized
            episode.assertion_hash = assertion_hash
            episode.assertion_source = source
            episode.assertion_recorded_at = now
            episode.updated_at = now
            await session.commit()

    async def retain_observation(
        self,
        identity: CaseExecutionIdentity,
        *,
        envelope: dict[str, Any],
    ) -> None:
        normalized = _safe_dict(envelope)
        missing = _OBSERVATION_KEYS - normalized.keys()
        if missing:
            raise ValueError(
                "observation envelope is missing required fields: " + ", ".join(sorted(missing))
            )
        if not isinstance(normalized["observation"], dict):
            raise ValueError("observation envelope observation must be an object")
        if not isinstance(normalized["aggregate_billing_complete"], bool):
            raise ValueError("aggregate_billing_complete must be boolean")
        observation_hash = canonical_json_hash(normalized)
        async with self.session_factory() as session:
            _, _, episode = await self._lock_current_episode(session, identity)
            if episode.observation_hash is not None:
                if (
                    episode.observation_hash == observation_hash
                    and episode.observation_envelope == normalized
                ):
                    await session.commit()
                    return
                raise _error(
                    "INTERACTIVE_OBSERVATION_EVIDENCE_CONFLICT",
                    "The episode already retains a different observation.",
                )
            self._require_not_cleaned(episode)
            if episode.execution_status not in {"PREPARING", "RUNNING"}:
                raise _error(
                    "INTERACTIVE_OBSERVATION_STATE_INVALID",
                    "The episode is not accepting a terminal observation.",
                )
            if episode.assertion_hash is None:
                raise _error(
                    "INTERACTIVE_ASSERTION_EVIDENCE_MISSING",
                    "A terminal observation requires the retained business assertion.",
                )
            observed_environment = normalized["observation"].get("environment")
            if (
                isinstance(observed_environment, dict)
                and "assertion" in observed_environment
                and observed_environment["assertion"] != episode.assertion
            ):
                raise _error(
                    "INTERACTIVE_ASSERTION_EVIDENCE_MISMATCH",
                    "The observation must reference the retained business assertion.",
                )
            incomplete = await session.scalar(
                select(func.count())
                .select_from(InteractiveEpisodeTurn)
                .where(
                    InteractiveEpisodeTurn.workspace_id == identity.workspace_id,
                    InteractiveEpisodeTurn.case_id == identity.case_id,
                    InteractiveEpisodeTurn.dispatch_status != "TERMINAL",
                )
            )
            if incomplete:
                raise _error(
                    "INTERACTIVE_OBSERVATION_EXECUTION_INCOMPLETE",
                    "A non-terminal turn cannot be materialized as a complete observation.",
                )
            now = _now()
            episode.observation_envelope = normalized
            episode.observation_hash = observation_hash
            episode.observation_recorded_at = now
            episode.execution_status = "COMPLETED"
            episode.prepare_stage = "COMPLETED"
            episode.completed_at = now
            episode.updated_at = now
            await session.commit()

    async def terminate(
        self,
        identity: CaseExecutionIdentity,
        *,
        reason: str,
    ) -> None:
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 96:
            raise ValueError("termination reason is invalid")
        async with self.session_factory() as session:
            _, _, episode = await self._lock_current_episode(session, identity)
            self._require_not_cleaned(episode)
            if episode.execution_status == "TERMINATED":
                if episode.termination_reason == reason:
                    await session.commit()
                    return
                raise _error(
                    "INTERACTIVE_TERMINATION_CONFLICT",
                    "The episode was terminated for a different reason.",
                )
            if episode.observation_hash is not None:
                raise _error(
                    "INTERACTIVE_TERMINATION_EVIDENCE_RETAINED",
                    "A completed interactive observation cannot be terminated.",
                )
            if episode.execution_status not in {"PREPARING", "RUNNING"}:
                raise _error(
                    "INTERACTIVE_TERMINATION_STATE_INVALID",
                    "The interactive episode cannot be terminated in its current state.",
                )
            if await self._has_dispatched_turn(session, episode):
                raise _error(
                    "INTERACTIVE_TERMINATION_REQUIRES_RECOVERY",
                    "A dispatched episode must go through recovery.",
                )
            now = _now()
            episode.execution_status = "TERMINATED"
            episode.termination_reason = reason
            episode.prepare_stage = "TERMINATED"
            episode.completed_at = now
            episode.updated_at = now
            await session.commit()

    async def cleanup(self, identity: CaseExecutionIdentity) -> str:
        try:
            async with self.session_factory() as session:
                _parent_run, case, episode = await self._lock_current_episode(session, identity)
                status = await self._cleanup_episode_in_session(
                    session,
                    episode=episode,
                    case=case,
                )
                await session.commit()
                return status
        except AgentHubError:
            raise
        except Exception:
            await self._record_cleanup_failure(identity)
            return "TEARDOWN_FAILED"

    async def recover_case(
        self,
        session: AsyncSession,
        case: EvaluationExperimentCaseResult,
        *,
        cancelled: bool = False,
    ) -> dict[str, Any]:
        """Recover one caller-locked case without committing its transaction."""

        episode = await self._episode_for_update(
            session,
            workspace_id=case.workspace_id,
            case_id=case.id,
        )
        if episode is None:
            agent_run = await self._agent_run_for_case(session, case)
            return {
                "envelope": None,
                "reason": "EVALUATION_INTERACTIVE_RECOVERY_REQUIRED",
                "cleanup_status": "MISSING",
                "turns": [],
                "agent_run_id": str(case.agent_run_id) if case.agent_run_id else None,
                "observed_agent_status": (agent_run.status if agent_run is not None else None),
                "observed_agent_failure_code": (
                    agent_run.failure_code if agent_run is not None else None
                ),
            }

        turns = await self._turns_for_update(session, episode)
        await self._recover_unbound_objects(session, case, episode, turns)
        turns = await self._turns_for_update(session, episode)
        await self._stabilize_preparation_runs(
            session,
            episode=episode,
            turns=turns,
        )
        agent_runs = await self._episode_agent_runs(session, episode, turns)
        uncertain = await self._recovery_uncertainty(
            session,
            episode=episode,
            turns=turns,
            agent_runs=agent_runs,
        )

        if uncertain:
            if episode.execution_status in {"PREPARING", "RUNNING"}:
                episode.execution_status = "RECOVERY_REQUIRED"
                episode.termination_reason = "EVALUATION_INTERACTIVE_RECOVERY_REQUIRED"
                episode.updated_at = _now()
            if agent_runs:
                await self._cancel_pending_approvals(
                    session,
                    workspace_id=case.workspace_id,
                    run_ids=tuple(run.id for run in agent_runs),
                )
            cleanup_status = await self._cleanup_episode_in_session(
                session,
                episode=episode,
                case=case,
            )
            return self._recovery_projection(
                episode,
                turns,
                agent_runs,
                envelope=None,
                reason="EVALUATION_INTERACTIVE_RECOVERY_REQUIRED",
                cleanup_status=cleanup_status,
            )

        if cancelled:
            await self._cancel_safe_drafts(
                session,
                workspace_id=case.workspace_id,
                turns=turns,
                agent_runs=agent_runs,
            )

        if episode.observation_envelope is None:
            if episode.execution_status in {"PREPARING", "RUNNING"}:
                episode.execution_status = "RECOVERY_REQUIRED"
                episode.termination_reason = "EVALUATION_INTERACTIVE_RECOVERY_REQUIRED"
                episode.updated_at = _now()
            cleanup_status = await self._cleanup_episode_in_session(
                session,
                episode=episode,
                case=case,
            )
            return self._recovery_projection(
                episode,
                turns,
                agent_runs,
                envelope=None,
                reason="EVALUATION_INTERACTIVE_RECOVERY_REQUIRED",
                cleanup_status=cleanup_status,
            )

        cleanup_status = await self._cleanup_episode_in_session(
            session,
            episode=episode,
            case=case,
        )
        return self._recovery_projection(
            episode,
            turns,
            agent_runs,
            envelope=episode.observation_envelope,
            reason="COMPLETED",
            cleanup_status=cleanup_status,
        )

    async def reconcile_cleanup(self, *, limit: int = 100) -> int:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        now = _now()
        async with self.session_factory() as discovery:
            candidates = list(
                await discovery.scalars(
                    select(InteractiveEpisode)
                    .where(
                        InteractiveEpisode.cleanup_status.in_(
                            {"PENDING", "TEARDOWN_FAILED", "CLEANUP_BLOCKED"}
                        ),
                        or_(
                            InteractiveEpisode.cleanup_next_attempt_at.is_(None),
                            InteractiveEpisode.cleanup_next_attempt_at <= now,
                        ),
                    )
                    .order_by(
                        InteractiveEpisode.workspace_id,
                        InteractiveEpisode.case_id,
                    )
                    .limit(limit)
                )
            )
            candidate_keys = [(episode.workspace_id, episode.case_id) for episode in candidates]

        processed = 0
        for workspace_id, case_id in candidate_keys:
            async with self.session_factory() as session:
                stale_case = await session.scalar(
                    select(EvaluationExperimentCaseResult).where(
                        EvaluationExperimentCaseResult.workspace_id == workspace_id,
                        EvaluationExperimentCaseResult.id == case_id,
                    )
                )
                if stale_case is None:
                    continue
                parent_run = await session.scalar(
                    select(EvaluationExperimentRun)
                    .where(
                        EvaluationExperimentRun.workspace_id == workspace_id,
                        EvaluationExperimentRun.id == stale_case.experiment_run_id,
                    )
                    .with_for_update(skip_locked=True)
                )
                if parent_run is None:
                    continue
                case = await session.scalar(
                    select(EvaluationExperimentCaseResult)
                    .where(
                        EvaluationExperimentCaseResult.workspace_id == workspace_id,
                        EvaluationExperimentCaseResult.id == case_id,
                        EvaluationExperimentCaseResult.experiment_run_id == parent_run.id,
                    )
                    .with_for_update(skip_locked=True)
                )
                if case is None:
                    continue
                episode = await self._episode_for_update(
                    session,
                    workspace_id=workspace_id,
                    case_id=case_id,
                    skip_locked=True,
                )
                if episode is None or episode.cleanup_status == "TEARDOWN_COMPLETE":
                    continue
                if (
                    episode.cleanup_next_attempt_at is not None
                    and episode.cleanup_next_attempt_at > _now()
                ):
                    continue
                if episode.observation_envelope is None and episode.execution_status in {
                    "PREPARING",
                    "RUNNING",
                }:
                    if case.status in {"PENDING", "RUNNING"}:
                        continue
                    now = _now()
                    episode.execution_status = "RECOVERY_REQUIRED"
                    episode.termination_reason = "EVALUATION_INTERACTIVE_RECOVERY_REQUIRED"
                    episode.completed_at = now
                    episode.updated_at = now
                try:
                    await self._cleanup_episode_in_session(
                        session,
                        episode=episode,
                        case=case,
                    )
                    await session.commit()
                except Exception:
                    await session.rollback()
                    await self._record_cleanup_failure_for_episode(
                        workspace_id=workspace_id,
                        case_id=case_id,
                        parent_run_id=parent_run.id,
                    )
                processed += 1
        return processed

    async def _lock_current_parent(
        self,
        session: AsyncSession,
        identity: CaseExecutionIdentity,
    ) -> tuple[EvaluationExperimentRun, EvaluationExperimentCaseResult]:
        run = await session.scalar(
            select(EvaluationExperimentRun)
            .where(
                EvaluationExperimentRun.workspace_id == identity.workspace_id,
                EvaluationExperimentRun.id == identity.run_id,
            )
            .with_for_update()
        )
        now = _now()
        if (
            run is None
            or run.status != "RUNNING"
            or run.lease_owner != identity.owner
            or run.lease_generation != identity.generation
            or run.lease_expires_at is None
            or run.lease_expires_at <= now
        ):
            raise _error(
                "INTERACTIVE_EXECUTION_FENCE_INVALID",
                "The evaluation run lease is no longer current.",
            )
        case = await session.scalar(
            select(EvaluationExperimentCaseResult)
            .where(
                EvaluationExperimentCaseResult.workspace_id == identity.workspace_id,
                EvaluationExperimentCaseResult.id == identity.case_id,
                EvaluationExperimentCaseResult.experiment_run_id == identity.run_id,
            )
            .with_for_update()
        )
        if (
            case is None
            or case.status != "RUNNING"
            or case.lease_owner != identity.owner
            or case.lease_generation != identity.generation
            or case.experiment_variant_id != identity.variant_id
            or case.dataset_item_id != identity.item_id
            or case.repetition_index != identity.repetition_index
        ):
            raise _error(
                "INTERACTIVE_CASE_FENCE_INVALID",
                "The evaluation case no longer matches the execution identity.",
            )
        return run, case

    async def _lock_current_episode(
        self,
        session: AsyncSession,
        identity: CaseExecutionIdentity,
    ) -> tuple[
        EvaluationExperimentRun,
        EvaluationExperimentCaseResult,
        InteractiveEpisode,
    ]:
        run, case = await self._lock_current_parent(session, identity)
        episode = await self._episode_for_update(
            session,
            workspace_id=identity.workspace_id,
            case_id=identity.case_id,
        )
        if episode is None:
            raise _error(
                "INTERACTIVE_EPISODE_NOT_FOUND",
                "The interactive episode has not been reserved.",
                404,
            )
        return run, case, episode

    async def _episode_for_update(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        case_id: UUID,
        skip_locked: bool = False,
    ) -> InteractiveEpisode | None:
        statement = (
            select(InteractiveEpisode)
            .where(
                InteractiveEpisode.workspace_id == workspace_id,
                InteractiveEpisode.case_id == case_id,
            )
            .with_for_update(skip_locked=skip_locked)
        )
        return await session.scalar(statement)

    async def _turn_for_update(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        case_id: UUID,
        turn_index: int,
    ) -> InteractiveEpisodeTurn:
        turn = await session.scalar(
            select(InteractiveEpisodeTurn)
            .where(
                InteractiveEpisodeTurn.workspace_id == workspace_id,
                InteractiveEpisodeTurn.case_id == case_id,
                InteractiveEpisodeTurn.turn_index == turn_index,
            )
            .with_for_update()
        )
        if turn is None:
            raise _error(
                "INTERACTIVE_TURN_NOT_FOUND",
                "The interactive turn was not reserved.",
                404,
            )
        return turn

    async def _turns_for_update(
        self,
        session: AsyncSession,
        episode: InteractiveEpisode,
    ) -> list[InteractiveEpisodeTurn]:
        return list(
            await session.scalars(
                select(InteractiveEpisodeTurn)
                .where(
                    InteractiveEpisodeTurn.workspace_id == episode.workspace_id,
                    InteractiveEpisodeTurn.case_id == episode.case_id,
                )
                .order_by(InteractiveEpisodeTurn.turn_index)
                .with_for_update()
            )
        )

    async def _has_dispatched_turn(
        self,
        session: AsyncSession,
        episode: InteractiveEpisode,
    ) -> bool:
        return bool(
            await session.scalar(
                select(func.count())
                .select_from(InteractiveEpisodeTurn)
                .where(
                    InteractiveEpisodeTurn.workspace_id == episode.workspace_id,
                    InteractiveEpisodeTurn.case_id == episode.case_id,
                    InteractiveEpisodeTurn.dispatch_status.in_(_DISPATCHED_TURN_STATUSES),
                )
            )
        )

    @staticmethod
    def _require_same_configuration(
        episode: InteractiveEpisode,
        *,
        seed: int,
        fixture_ref: str,
        input_hash: str,
        policy_hash: str,
        fixture_hash: str,
    ) -> None:
        if (
            episode.cleanup_status == "TEARDOWN_COMPLETE"
            or episode.instance_id != episode.case_id
            or episode.seed != seed
            or episode.fixture_ref != fixture_ref
            or episode.input_hash != input_hash
            or episode.policy_hash != policy_hash
            or episode.fixture_hash != fixture_hash
            or episode.env_kind != _ENVIRONMENT_KIND
            or episode.driver_version != INTERACTIVE_DRIVER_VERSION
            or episode.env_version != INTERACTIVE_ENVIRONMENT_VERSION
            or episode.simulator_version != INTERACTIVE_SIMULATOR_VERSION
        ):
            raise _error(
                "INTERACTIVE_EPISODE_CONFIGURATION_CONFLICT",
                "The case already has a different execution configuration.",
            )

    async def _validate_frozen_item_hashes(
        self,
        session: AsyncSession,
        *,
        case: EvaluationExperimentCaseResult,
        dataset_version_id: UUID,
        input_hash: str,
        policy_hash: str,
    ) -> None:
        item = await session.scalar(
            select(EvaluationDatasetItem).where(
                EvaluationDatasetItem.workspace_id == case.workspace_id,
                EvaluationDatasetItem.id == case.dataset_item_id,
            )
        )
        if (
            item is None
            or item.dataset_version_id != dataset_version_id
            or item.category != "INTERACTIVE"
        ):
            raise _error(
                "INTERACTIVE_DATASET_ITEM_MISSING",
                "The frozen interactive dataset item was not found.",
            )
        simulator = item.input.get("simulator")
        triggers = simulator.get("triggers") if isinstance(simulator, Mapping) else None
        if not isinstance(triggers, Mapping):
            raise _error(
                "INTERACTIVE_DATASET_POLICY_INVALID",
                "The frozen interactive dataset policy is invalid.",
            )
        if (
            canonical_json_hash(item.input) != input_hash
            or canonical_json_hash(dict(triggers)) != policy_hash
        ):
            raise _error(
                "INTERACTIVE_EPISODE_CONFIGURATION_CONFLICT",
                "The execution hashes do not match the frozen dataset item.",
            )

    @staticmethod
    def _require_not_cleaned(episode: InteractiveEpisode) -> None:
        if episode.cleanup_status == "TEARDOWN_COMPLETE":
            raise _error(
                "INTERACTIVE_CLEANUP_ALREADY_COMPLETE",
                "The interactive fixture has already been cleaned up.",
            )

    @staticmethod
    def _client_token(case_id: UUID, turn_index: int) -> str:
        token = f"{case_id.hex}:{turn_index}"
        if len(token) > 64:
            raise ValueError("interactive turn token is too long")
        return token

    @staticmethod
    def _customer_ref(episode: InteractiveEpisode) -> str:
        return f"AE6B-{episode.instance_id.hex}"

    async def _case_agent_version_id(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        case: EvaluationExperimentCaseResult,
    ) -> UUID:
        variant = await self._case_variant(session, workspace_id=workspace_id, case=case)
        return variant.agent_version_id

    async def _case_variant(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        case: EvaluationExperimentCaseResult,
    ) -> EvaluationExperimentVariant:
        variant = await session.scalar(
            select(EvaluationExperimentVariant).where(
                EvaluationExperimentVariant.workspace_id == workspace_id,
                EvaluationExperimentVariant.id == case.experiment_variant_id,
            )
        )
        if variant is None:
            raise _error(
                "INTERACTIVE_CASE_VARIANT_MISSING",
                "The evaluation case variant was not found.",
            )
        return variant

    @staticmethod
    def _agent_run_matches_frozen_variant(
        agent_run: AgentRun,
        variant: EvaluationExperimentVariant,
    ) -> bool:
        return (
            agent_run.agent_version_id == variant.agent_version_id
            and agent_run.resolved_spec_hash == variant.resolved_spec_hash
            and agent_run.effective_knowledge_snapshots == variant.effective_knowledge_snapshots
            and agent_run.effective_wiki_publications == variant.effective_wiki_publications
        )

    @staticmethod
    def _input_matches_reserved_turn(
        *,
        reserved_hash: str,
        thread_turn: ThreadTurn,
        agent_run_input: str,
    ) -> bool:
        return InteractiveLifecycle._reserved_hash_matches_turn_input(
            reserved_hash=reserved_hash,
            thread_turn=thread_turn,
        ) and _plain_input_hash(agent_run_input) == _plain_input_hash(thread_turn.user_input)

    @staticmethod
    def _reserved_hash_matches_turn_input(
        *,
        reserved_hash: str,
        thread_turn: ThreadTurn,
    ) -> bool:
        plain_hash = _plain_input_hash(thread_turn.user_input)
        return thread_turn.input_hash == plain_hash and reserved_hash in {
            plain_hash,
            canonical_json_hash(thread_turn.user_input),
        }

    async def _has_unknown_execution(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        run_id: UUID,
    ) -> bool:
        approvals = int(
            await session.scalar(
                select(func.count())
                .select_from(Approval)
                .where(
                    Approval.workspace_id == workspace_id,
                    Approval.run_id == run_id,
                    Approval.execution_status.in_(
                        {
                            ApprovalExecutionStatus.CLAIMED.value,
                            ApprovalExecutionStatus.UNKNOWN_OUTCOME.value,
                        }
                    ),
                )
            )
            or 0
        )
        pending_actions = int(
            await session.scalar(
                select(func.count())
                .select_from(RunStep)
                .where(
                    RunStep.workspace_id == workspace_id,
                    RunStep.agent_run_id == run_id,
                    RunStep.kind.in_({"ACTION", "TOOL_EXECUTE"}),
                    RunStep.status.not_in({"SUCCEEDED", "FAILED", "CANCELLED", "SKIPPED"}),
                )
            )
            or 0
        )
        return bool(approvals or pending_actions)

    async def _cleanup_episode_in_session(
        self,
        session: AsyncSession,
        *,
        episode: InteractiveEpisode,
        case: EvaluationExperimentCaseResult,
    ) -> str:
        if episode.cleanup_status == "TEARDOWN_COMPLETE":
            return "TEARDOWN_COMPLETE"
        if case.workspace_id != episode.workspace_id or case.id != episode.case_id:
            raise _error(
                "INTERACTIVE_CLEANUP_CASE_MISMATCH",
                "Cleanup requires the episode's locked parent case.",
            )
        turns = await self._turns_for_update(session, episode)
        await self._stabilize_preparation_runs(
            session,
            episode=episode,
            turns=turns,
        )
        agent_runs = await self._episode_agent_runs(session, episode, turns)
        blocker = await self._cleanup_blocker(
            session,
            episode=episode,
            turns=turns,
            agent_runs=agent_runs,
        )
        now = _now()
        episode.cleanup_attempts += 1
        episode.cleanup_started_at = episode.cleanup_started_at or now
        episode.updated_at = now
        if blocker is not None:
            episode.cleanup_status = "CLEANUP_BLOCKED"
            episode.cleanup_error = blocker
            episode.cleanup_next_attempt_at = now + _CLEANUP_RETRY_DELAY
            return "CLEANUP_BLOCKED"

        await self._cancel_pending_for_cleanup(
            session,
            episode=episode,
        )
        await self._retain_recovery_fixture_snapshot(
            session,
            episode=episode,
            now=now,
        )
        if episode.customer_id is not None and episode.customer_ref is not None:
            await session.execute(
                delete(Customer).where(
                    Customer.workspace_id == episode.workspace_id,
                    Customer.id == episode.customer_id,
                    Customer.customer_ref == episode.customer_ref,
                )
            )
        episode.cleanup_status = "TEARDOWN_COMPLETE"
        episode.cleanup_error = None
        episode.cleanup_next_attempt_at = None
        episode.cleanup_completed_at = now
        episode.updated_at = now
        return "TEARDOWN_COMPLETE"

    async def _cleanup_blocker(
        self,
        session: AsyncSession,
        *,
        episode: InteractiveEpisode,
        turns: Sequence[InteractiveEpisodeTurn],
        agent_runs: Sequence[AgentRun],
    ) -> str | None:
        now = _now()
        for run in agent_runs:
            if run.status == "NEEDS_ATTENTION":
                return "INTERACTIVE_CLEANUP_UNKNOWN_OUTCOME"
            if run.execution_owner is not None and (
                run.execution_lease_expires_at is None or run.execution_lease_expires_at > now
            ):
                return "INTERACTIVE_CLEANUP_RUN_OWNER_ACTIVE"
            if run.status in _ACTIVE_AGENT_STATUSES:
                return "INTERACTIVE_CLEANUP_RUN_NOT_TERMINAL"
            if await self._has_unknown_execution(
                session,
                workspace_id=episode.workspace_id,
                run_id=run.id,
            ):
                return "INTERACTIVE_CLEANUP_UNKNOWN_OUTCOME"

        if any(
            turn.dispatch_status in {"DISPATCH_INTENT", "UNKNOWN_OUTCOME", "RECOVERY_REQUIRED"}
            for turn in turns
        ):
            return "INTERACTIVE_CLEANUP_UNKNOWN_OUTCOME"

        return None

    async def _retain_recovery_fixture_snapshot(
        self,
        session: AsyncSession,
        *,
        episode: InteractiveEpisode,
        now: datetime,
    ) -> None:
        if episode.assertion_hash is not None:
            return
        ticket_statuses: list[str] = []
        if episode.customer_id is not None and episode.customer_ref is not None:
            customer = await session.scalar(
                select(Customer)
                .where(
                    Customer.workspace_id == episode.workspace_id,
                    Customer.id == episode.customer_id,
                    Customer.customer_ref == episode.customer_ref,
                )
                .with_for_update()
            )
            if customer is not None:
                ticket_statuses = list(
                    await session.scalars(
                        select(Ticket.status)
                        .where(
                            Ticket.workspace_id == episode.workspace_id,
                            Ticket.customer_id == customer.id,
                        )
                        .order_by(Ticket.status, Ticket.id)
                        .with_for_update()
                    )
                )
        assertion = {
            "passed": False,
            "reason": "interrupted_before_task_assertion",
            "observed": {
                "ticket_count": len(ticket_statuses),
                "ticket_statuses": ticket_statuses,
            },
        }
        episode.assertion = assertion
        episode.assertion_hash = canonical_json_hash(assertion)
        episode.assertion_source = "recovery_fixture_snapshot"
        episode.assertion_recorded_at = now
        episode.updated_at = now

    async def _stabilize_preparation_runs(
        self,
        session: AsyncSession,
        *,
        episode: InteractiveEpisode,
        turns: Sequence[InteractiveEpisodeTurn],
    ) -> None:
        """Fence expired owners and cancel only safe pre-dispatch drafts."""

        thread_id = episode.thread_id or episode.reserved_thread_id
        reverse_runs = list(
            await session.scalars(
                select(AgentRun)
                .where(
                    AgentRun.workspace_id == episode.workspace_id,
                    AgentRun.thread_id == thread_id,
                )
                .order_by(AgentRun.id)
                .with_for_update()
            )
        )
        turn_by_run = {turn.agent_run_id: turn for turn in turns if turn.agent_run_id is not None}
        safe_run_ids: list[UUID] = []
        uncertain_run_ids: list[UUID] = []
        now = _now()
        for run in reverse_runs:
            turn = turn_by_run.get(run.id)
            if (
                run.status in _ACTIVE_AGENT_STATUSES
                and run.execution_owner is not None
                and (
                    run.execution_lease_expires_at is None or run.execution_lease_expires_at <= now
                )
            ):
                uncertain_run_ids.append(run.id)
                if run.status not in {"CANCELLED"}:
                    run.status = "NEEDS_ATTENTION"
                    run.failure_code = (
                        run.failure_code or "EVALUATION_INTERACTIVE_RECOVERY_REQUIRED"
                    )
                    run.execution_owner = None
                    run.execution_lease_expires_at = None
                    run.completed_at = run.completed_at or now
                if turn is not None and turn.dispatch_status == "DISPATCH_INTENT":
                    turn.dispatch_status = "UNKNOWN_OUTCOME"
                    turn.terminal_status = "NEEDS_ATTENTION"
                    turn.failure_code = (
                        turn.failure_code or "EVALUATION_INTERACTIVE_RECOVERY_REQUIRED"
                    )
                    turn.completed_at = turn.completed_at or now
                    turn.updated_at = now
                continue
            if turn is not None and turn.dispatch_status not in {"RESERVED", "BOUND"}:
                continue
            if run.status not in _ACTIVE_AGENT_STATUSES or run.execution_owner is not None:
                continue
            if await self._has_unknown_execution(
                session,
                workspace_id=episode.workspace_id,
                run_id=run.id,
            ):
                continue
            safe_run_ids.append(run.id)

        if uncertain_run_ids:
            if episode.execution_status in {"PREPARING", "RUNNING"}:
                episode.execution_status = "RECOVERY_REQUIRED"
                episode.termination_reason = "EVALUATION_INTERACTIVE_RECOVERY_REQUIRED"
                episode.updated_at = now
            await self._cancel_pending_approvals(
                session,
                workspace_id=episode.workspace_id,
                run_ids=tuple(uncertain_run_ids),
            )
        await self._cancel_pending_approvals(
            session,
            workspace_id=episode.workspace_id,
            run_ids=tuple(safe_run_ids),
        )
        if safe_run_ids:
            await session.execute(
                update(AgentRun)
                .where(
                    AgentRun.workspace_id == episode.workspace_id,
                    AgentRun.id.in_(safe_run_ids),
                    AgentRun.status.in_(_ACTIVE_AGENT_STATUSES),
                    AgentRun.execution_owner.is_(None),
                )
                .values(
                    status="CANCELLED",
                    execution_lease_expires_at=None,
                    completed_at=now,
                )
            )

    async def _cancel_pending_for_cleanup(
        self,
        session: AsyncSession,
        *,
        episode: InteractiveEpisode,
    ) -> None:
        thread_id = episode.thread_id or episode.reserved_thread_id
        safe_run_ids = list(
            await session.scalars(
                select(AgentRun.id).where(
                    AgentRun.workspace_id == episode.workspace_id,
                    AgentRun.thread_id == thread_id,
                    AgentRun.status.in_(_FINAL_AGENT_STATUSES),
                    AgentRun.execution_owner.is_(None),
                )
            )
        )
        await self._cancel_pending_approvals(
            session,
            workspace_id=episode.workspace_id,
            run_ids=tuple(safe_run_ids),
        )

    async def _record_cleanup_failure(
        self,
        identity: CaseExecutionIdentity,
    ) -> None:
        await self._record_cleanup_failure_for_episode(
            workspace_id=identity.workspace_id,
            case_id=identity.case_id,
            parent_run_id=identity.run_id,
        )

    async def _record_cleanup_failure_for_episode(
        self,
        *,
        workspace_id: UUID,
        case_id: UUID,
        parent_run_id: UUID,
    ) -> None:
        async with self.session_factory() as session:
            run = await session.scalar(
                select(EvaluationExperimentRun)
                .where(
                    EvaluationExperimentRun.workspace_id == workspace_id,
                    EvaluationExperimentRun.id == parent_run_id,
                )
                .with_for_update()
            )
            if run is None:
                return
            case = await session.scalar(
                select(EvaluationExperimentCaseResult)
                .where(
                    EvaluationExperimentCaseResult.workspace_id == workspace_id,
                    EvaluationExperimentCaseResult.id == case_id,
                    EvaluationExperimentCaseResult.experiment_run_id == parent_run_id,
                )
                .with_for_update()
            )
            if case is None:
                return
            episode = await self._episode_for_update(
                session,
                workspace_id=workspace_id,
                case_id=case_id,
            )
            if episode is None or episode.cleanup_status == "TEARDOWN_COMPLETE":
                return
            now = _now()
            episode.cleanup_status = "TEARDOWN_FAILED"
            episode.cleanup_error = "INTERACTIVE_CLEANUP_FAILED"
            episode.cleanup_attempts += 1
            episode.cleanup_started_at = episode.cleanup_started_at or now
            episode.cleanup_next_attempt_at = now + _CLEANUP_RETRY_DELAY
            episode.updated_at = now
            await session.commit()

    async def _recover_unbound_objects(
        self,
        session: AsyncSession,
        case: EvaluationExperimentCaseResult,
        episode: InteractiveEpisode,
        turns: Sequence[InteractiveEpisodeTurn],
    ) -> None:
        variant = await self._case_variant(session, workspace_id=case.workspace_id, case=case)
        agent_version_id = variant.agent_version_id
        parent_run = await session.scalar(
            select(EvaluationExperimentRun).where(
                EvaluationExperimentRun.workspace_id == case.workspace_id,
                EvaluationExperimentRun.id == case.experiment_run_id,
            )
        )
        if episode.thread_id is None:
            thread = await session.scalar(
                select(AgentThread)
                .where(
                    AgentThread.workspace_id == case.workspace_id,
                    AgentThread.id == episode.reserved_thread_id,
                )
                .with_for_update()
            )
            if thread is not None:
                version = await session.scalar(
                    select(AgentVersion).where(
                        AgentVersion.workspace_id == case.workspace_id,
                        AgentVersion.id == agent_version_id,
                    )
                )
                if (
                    version is not None
                    and parent_run is not None
                    and thread.agent_id == version.agent_id
                    and thread.created_by == parent_run.created_by
                ):
                    episode.thread_id = thread.id
                    episode.updated_at = _now()
        if episode.thread_id is None:
            return

        for turn in turns:
            thread_turn = None
            if turn.thread_turn_id is not None:
                thread_turn = await session.scalar(
                    select(ThreadTurn)
                    .where(
                        ThreadTurn.workspace_id == case.workspace_id,
                        ThreadTurn.id == turn.thread_turn_id,
                    )
                    .with_for_update()
                )
            if thread_turn is None:
                thread_turn = await session.scalar(
                    select(ThreadTurn)
                    .where(
                        ThreadTurn.workspace_id == case.workspace_id,
                        ThreadTurn.thread_id == episode.thread_id,
                        ThreadTurn.client_token == turn.client_token,
                    )
                    .with_for_update()
                )
            if (
                thread_turn is None
                or thread_turn.thread_id != episode.thread_id
                or not self._reserved_hash_matches_turn_input(
                    reserved_hash=turn.input_hash,
                    thread_turn=thread_turn,
                )
            ):
                continue

            agent_run = None
            if turn.agent_run_id is not None:
                agent_run = await session.scalar(
                    select(AgentRun)
                    .where(
                        AgentRun.workspace_id == case.workspace_id,
                        AgentRun.id == turn.agent_run_id,
                    )
                    .with_for_update()
                )
            if agent_run is None:
                agent_run = await session.scalar(
                    select(AgentRun)
                    .where(
                        AgentRun.workspace_id == case.workspace_id,
                        AgentRun.thread_turn_id == thread_turn.id,
                    )
                    .order_by(AgentRun.created_at, AgentRun.id)
                    .with_for_update()
                )
            if agent_run is not None:
                if (
                    parent_run is None
                    or agent_run.thread_id != episode.thread_id
                    or agent_run.thread_id != episode.reserved_thread_id
                    or agent_run.thread_turn_id != thread_turn.id
                    or not self._agent_run_matches_frozen_variant(agent_run, variant)
                    or agent_run.created_by != parent_run.created_by
                    or not self._input_matches_reserved_turn(
                        reserved_hash=turn.input_hash,
                        thread_turn=thread_turn,
                        agent_run_input=agent_run.input_text,
                    )
                    or (
                        thread_turn.agent_run_id is not None
                        and thread_turn.agent_run_id != agent_run.id
                    )
                ):
                    continue
                if turn.thread_turn_id is None:
                    turn.thread_turn_id = thread_turn.id
                if turn.agent_run_id is None:
                    turn.agent_run_id = agent_run.id
                if thread_turn.agent_run_id is None:
                    thread_turn.agent_run_id = agent_run.id
                # The runtime may have committed its terminal result before the
                # worker recorded turn evidence. Recover that confirmed fact,
                # without treating it as a completed conversation or dispatching
                # anything again. Live owners and uncertain actions stay blocked.
                owner_active = agent_run.execution_owner is not None and (
                    agent_run.execution_lease_expires_at is None
                    or agent_run.execution_lease_expires_at > _now()
                )
                if (
                    turn.dispatch_status == "DISPATCH_INTENT"
                    and turn.evidence_hash is None
                    and agent_run.status in _FINAL_AGENT_STATUSES
                    and not owner_active
                    and not await self._has_unknown_execution(
                        session, workspace_id=case.workspace_id, run_id=agent_run.id
                    )
                ):
                    evidence = _safe_turn_evidence(
                        {
                            "evidence_source": "agent_run_terminal_storage",
                            "run_id": str(agent_run.id),
                            "status": agent_run.status,
                            "failure_code": agent_run.failure_code,
                            "user_input_hash": turn.input_hash,
                            "agent_output_hash": canonical_json_hash(agent_run.final_output or ""),
                            "input_tokens": agent_run.total_input_tokens,
                            "output_tokens": agent_run.total_output_tokens,
                            "cached_tokens": agent_run.total_cached_tokens,
                            "total_tokens": agent_run.total_tokens,
                            "cost_amount": agent_run.total_cost_amount,
                            "cost_currency": agent_run.cost_currency,
                        }
                    )
                    turn.evidence = evidence
                    turn.evidence_hash = canonical_json_hash(evidence)
                    turn.terminal_status = agent_run.status
                    turn.failure_code = agent_run.failure_code
                    turn.completed_at = agent_run.completed_at or _now()
                    turn.dispatch_status = "TERMINAL"
            if (
                turn.thread_turn_id is not None
                and turn.agent_run_id is not None
                and turn.dispatch_status == "RESERVED"
            ):
                turn.dispatch_status = "BOUND"
            turn.updated_at = _now()

    async def _episode_agent_runs(
        self,
        session: AsyncSession,
        episode: InteractiveEpisode,
        turns: Sequence[InteractiveEpisodeTurn],
    ) -> list[AgentRun]:
        run_ids = {turn.agent_run_id for turn in turns if turn.agent_run_id is not None}
        thread_id = episode.thread_id or episode.reserved_thread_id
        ownership = [AgentRun.thread_id == thread_id]
        if run_ids:
            ownership.append(AgentRun.id.in_(run_ids))
        return list(
            await session.scalars(
                select(AgentRun)
                .where(
                    AgentRun.workspace_id == episode.workspace_id,
                    or_(*ownership),
                )
                .order_by(AgentRun.created_at, AgentRun.id)
                .with_for_update()
            )
        )

    async def _recovery_uncertainty(
        self,
        session: AsyncSession,
        *,
        episode: InteractiveEpisode,
        turns: Sequence[InteractiveEpisodeTurn],
        agent_runs: Sequence[AgentRun],
    ) -> bool:
        now = _now()
        for run in agent_runs:
            if run.status == "NEEDS_ATTENTION":
                return True
            if run.execution_owner is not None and (
                run.execution_lease_expires_at is None or run.execution_lease_expires_at > now
            ):
                return True
            if run.status in _ACTIVE_AGENT_STATUSES:
                return True
            if await self._has_unknown_execution(
                session,
                workspace_id=episode.workspace_id,
                run_id=run.id,
            ):
                return True
        for turn in turns:
            if turn.dispatch_status in {
                "DISPATCH_INTENT",
                "UNKNOWN_OUTCOME",
                "RECOVERY_REQUIRED",
            }:
                return True
        return False

    async def _cancel_safe_drafts(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        turns: Sequence[InteractiveEpisodeTurn],
        agent_runs: Sequence[AgentRun],
    ) -> None:
        now = _now()
        safe_run_ids = {
            turn.agent_run_id
            for turn in turns
            if turn.dispatch_status in {"RESERVED", "BOUND"} and turn.agent_run_id is not None
        }
        await self._cancel_pending_approvals(
            session,
            workspace_id=workspace_id,
            run_ids=tuple(safe_run_ids),
        )
        for run in agent_runs:
            if run.id not in safe_run_ids:
                continue
            if run.execution_owner is not None and (
                run.execution_lease_expires_at is None or run.execution_lease_expires_at > now
            ):
                continue
            if run.status in {"RUNNING", "CANCEL_REQUESTED", "WAITING_APPROVAL"}:
                run.status = "CANCELLED"
                run.execution_owner = None
                run.execution_lease_expires_at = None
                run.completed_at = now

    async def _cancel_pending_approvals(
        self,
        session: AsyncSession,
        *,
        workspace_id: UUID,
        run_ids: tuple[UUID, ...],
    ) -> None:
        if not run_ids:
            return
        await session.execute(
            update(Approval)
            .where(
                Approval.workspace_id == workspace_id,
                Approval.run_id.in_(run_ids),
                Approval.decision_status == ApprovalDecisionStatus.PENDING.value,
                Approval.execution_status == ApprovalExecutionStatus.NOT_STARTED.value,
            )
            .values(
                decision_status=ApprovalDecisionStatus.CANCELLED.value,
                decided_at=_now(),
            )
        )

    async def _agent_run_for_case(
        self,
        session: AsyncSession,
        case: EvaluationExperimentCaseResult,
    ) -> AgentRun | None:
        if case.agent_run_id is None:
            return None
        return await session.scalar(
            select(AgentRun).where(
                AgentRun.workspace_id == case.workspace_id,
                AgentRun.id == case.agent_run_id,
            )
        )

    @staticmethod
    def _recovery_projection(
        episode: InteractiveEpisode,
        turns: Sequence[InteractiveEpisodeTurn],
        agent_runs: Sequence[AgentRun],
        *,
        envelope: dict[str, Any] | None,
        reason: str,
        cleanup_status: str,
    ) -> dict[str, Any]:
        first_run = agent_runs[0] if agent_runs else None
        return {
            "envelope": envelope,
            "reason": reason,
            "cleanup_status": cleanup_status,
            "turns": [
                {
                    "turn_index": turn.turn_index,
                    "client_token": turn.client_token,
                    "thread_turn_id": (
                        str(turn.thread_turn_id) if turn.thread_turn_id is not None else None
                    ),
                    "agent_run_id": (
                        str(turn.agent_run_id) if turn.agent_run_id is not None else None
                    ),
                    "dispatch_status": turn.dispatch_status,
                    "terminal_status": turn.terminal_status,
                    "failure_code": turn.failure_code,
                    "evidence_hash": turn.evidence_hash,
                }
                for turn in turns
            ],
            "agent_run_id": str(first_run.id) if first_run is not None else None,
            "observed_agent_status": (first_run.status if first_run is not None else None),
            "observed_agent_failure_code": (
                first_run.failure_code if first_run is not None else None
            ),
        }


def _plain_input_hash(value: str) -> str:
    """Match ThreadService's pre-dispatch hash for the exact stored input."""

    from hashlib import sha256

    return sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "CaseExecutionIdentity",
    "INTERACTIVE_DRIVER_VERSION",
    "INTERACTIVE_ENVIRONMENT_VERSION",
    "INTERACTIVE_LIFECYCLE_VERSION",
    "INTERACTIVE_SIMULATOR_VERSION",
    "InteractiveLifecycle",
]
