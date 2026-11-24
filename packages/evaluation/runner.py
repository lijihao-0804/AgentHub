"""Durable M7-C experiment execution primitives.

The database is the source of truth.  Celery only transports an experiment run id;
workers rebuild the frozen execution plan from the persisted M7-B records.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy import case as sql_case
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.frozen import parse_frozen_agent_spec
from packages.agent_runtime.models import AgentRun, AgentVersion
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.config.settings import Settings
from packages.core.errors.exceptions import AgentHubError
from packages.evaluation.case_budget_execution import reservation_unavailable
from packages.evaluation.drivers import (
    AgentRuntimeEvaluationDriver,
    CaseExecutionDriver,
    CaseExecutionObservation,
    DeterministicEvaluationDriver,
    PreparedCaseExecution,
    _model_attempt,
    _safe_model_attempt_projection,
)
from packages.evaluation.interactive_lifecycle import CaseExecutionIdentity, InteractiveLifecycle
from packages.evaluation.interactive_observation import observation_from_envelope, with_cleanup
from packages.evaluation.models import (
    EvaluationCaseResultStatus,
    EvaluationDatasetItem,
    EvaluationDatasetVersion,
    EvaluationExperiment,
    EvaluationExperimentCaseResult,
    EvaluationExperimentRun,
    EvaluationExperimentRunStatus,
    EvaluationExperimentVariant,
    PricingSnapshot,
)
from packages.evaluation.pricing import (
    PricingChainError,
    load_pricing_snapshots,
    pricing_snapshot_ids_from_metadata,
    resolve_case_pricing,
    validate_frozen_pricing_chain,
)
from packages.evaluation.reproducibility import (
    experiment_spec_hash,
    normalize_knowledge_snapshots,
    variant_hash,
)
from packages.knowledge.models import KnowledgeSnapshot
from packages.observability import NoopTraceSink
from packages.observability.contracts import TraceSink, TraceSpan

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunProgress:
    total: int
    pending: int
    running: int
    completed: int
    failed: int
    cancelled: int

    @property
    def progress(self) -> float:
        return (
            1.0 if self.total == 0 else (self.completed + self.failed + self.cancelled) / self.total
        )


def _now() -> datetime:
    return datetime.now(UTC)


def _execution_key(run_id: UUID, variant_id: UUID, item_id: UUID, repetition_index: int) -> str:
    raw = f"{run_id}:{variant_id}:{item_id}:{repetition_index}".encode()
    return hashlib.sha256(raw).hexdigest()


class ExperimentRunner:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        driver: CaseExecutionDriver | None = None,
        trace_sink: TraceSink | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.driver = driver or DeterministicEvaluationDriver()
        self.trace_sink = trace_sink or NoopTraceSink()

    async def prepare_run(self, session: AsyncSession, *, run_id: UUID) -> int:
        run = await session.scalar(
            select(EvaluationExperimentRun)
            .where(EvaluationExperimentRun.id == run_id)
            .with_for_update()
        )
        if run is None:
            return 0
        if run.status not in {
            EvaluationExperimentRunStatus.QUEUED,
            EvaluationExperimentRunStatus.RUNNING,
        }:
            await session.rollback()
            return 0
        variants = list(
            await session.scalars(
                select(EvaluationExperimentVariant)
                .where(
                    EvaluationExperimentVariant.workspace_id == run.workspace_id,
                    EvaluationExperimentVariant.experiment_id == run.experiment_id,
                )
                .order_by(EvaluationExperimentVariant.ordinal, EvaluationExperimentVariant.id)
            )
        )
        items = list(
            await session.scalars(
                select(EvaluationDatasetItem)
                .where(
                    EvaluationDatasetItem.workspace_id == run.workspace_id,
                    EvaluationDatasetItem.dataset_version_id == run.dataset_version_id,
                    EvaluationDatasetItem.split == run.split,
                )
                .order_by(EvaluationDatasetItem.ordinal, EvaluationDatasetItem.id)
            )
        )
        rows = [
            {
                "id": uuid4(),
                "workspace_id": run.workspace_id,
                "experiment_run_id": run.id,
                "experiment_variant_id": variant.id,
                "dataset_item_id": item.id,
                "repetition_index": repetition_index,
                "case_execution_key": _execution_key(run.id, variant.id, item.id, repetition_index),
                "status": EvaluationCaseResultStatus.PENDING,
                "observation": {},
            }
            for item in items
            for variant in variants
            for repetition_index in range(run.repetitions)
        ]
        if rows:
            await session.execute(
                insert(EvaluationExperimentCaseResult)
                .values(rows)
                .on_conflict_do_nothing(
                    index_elements=[
                        "workspace_id",
                        "experiment_run_id",
                        "experiment_variant_id",
                        "dataset_item_id",
                        "repetition_index",
                    ]
                )
            )
        await session.commit()
        return len(rows)

    async def bind_case_agent_run(
        self,
        session: AsyncSession,
        *,
        case_id: UUID,
        run_id: UUID,
        agent_run_id: UUID,
        owner: str,
        generation: int,
        preparation_observation: dict[str, Any] | None = None,
    ) -> bool:
        values: dict[str, Any] = {"agent_run_id": agent_run_id}
        if preparation_observation is not None:
            values["observation"] = preparation_observation
        result = await session.execute(
            update(EvaluationExperimentCaseResult)
            .where(
                EvaluationExperimentCaseResult.id == case_id,
                EvaluationExperimentCaseResult.experiment_run_id == run_id,
                EvaluationExperimentCaseResult.status == EvaluationCaseResultStatus.RUNNING,
                EvaluationExperimentCaseResult.lease_owner == owner,
                EvaluationExperimentCaseResult.lease_generation == generation,
                or_(
                    EvaluationExperimentCaseResult.agent_run_id.is_(None),
                    EvaluationExperimentCaseResult.agent_run_id == agent_run_id,
                ),
                _current_run_fence(run_id, owner, generation),
            )
            .values(**values)
        )
        await session.commit()
        return result.rowcount == 1

    async def claim_run(
        self,
        session: AsyncSession,
        *,
        run_id: UUID,
        owner: str,
        lease_seconds: int,
    ) -> bool:
        now = _now()
        result = await session.execute(
            update(EvaluationExperimentRun)
            .where(
                EvaluationExperimentRun.id == run_id,
                or_(
                    EvaluationExperimentRun.status == EvaluationExperimentRunStatus.QUEUED,
                    and_(
                        EvaluationExperimentRun.status == EvaluationExperimentRunStatus.RUNNING,
                        EvaluationExperimentRun.lease_expires_at.is_not(None),
                        EvaluationExperimentRun.lease_expires_at <= now,
                    ),
                ),
            )
            .values(
                status=EvaluationExperimentRunStatus.RUNNING,
                lease_owner=owner,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                heartbeat_at=now,
                started_at=func.coalesce(EvaluationExperimentRun.started_at, now),
                attempt_count=EvaluationExperimentRun.attempt_count + 1,
                lease_generation=EvaluationExperimentRun.lease_generation + 1,
            )
        )
        await session.commit()
        return result.rowcount == 1

    async def recover_inflight_cases(self, session: AsyncSession, *, run_id: UUID) -> None:
        parent = await session.scalar(
            select(EvaluationExperimentRun)
            .where(EvaluationExperimentRun.id == run_id)
            .with_for_update()
        )
        recovered_results = []
        cases = list(
            await session.scalars(
                select(EvaluationExperimentCaseResult)
                .where(
                    EvaluationExperimentCaseResult.experiment_run_id == run_id,
                    EvaluationExperimentCaseResult.status == EvaluationCaseResultStatus.RUNNING,
                )
                .with_for_update()
            )
        )
        terminal_statuses = {"SUCCEEDED", "FAILED", "NEEDS_ATTENTION", "CANCELLED"}
        for case in cases:
            item = await session.scalar(
                select(EvaluationDatasetItem).where(
                    EvaluationDatasetItem.workspace_id == case.workspace_id,
                    EvaluationDatasetItem.id == case.dataset_item_id,
                )
            )
            if item is not None and item.category == "INTERACTIVE":
                recovered = await InteractiveLifecycle(self.session_factory).recover_case(
                    session, case
                )
                envelope = recovered.get("envelope")
                if recovered.get("agent_run_id"):
                    recovered_run_id = UUID(recovered["agent_run_id"])
                    if case.agent_run_id is not None and case.agent_run_id != recovered_run_id:
                        envelope = None
                    else:
                        case.agent_run_id = recovered_run_id
                case.observed_agent_status = recovered.get("observed_agent_status")
                case.observed_agent_failure_code = recovered.get("observed_agent_failure_code")
                if envelope is not None and parent is not None:
                    case.lease_owner = parent.lease_owner
                    case.lease_generation = parent.lease_generation
                    recovered_results.append(
                        (
                            case.id,
                            parent.lease_owner,
                            parent.lease_generation,
                            with_cleanup(envelope, recovered["cleanup_status"]),
                        )
                    )
                else:
                    case.status = EvaluationCaseResultStatus.FAILED
                    case.failure_code = "EVALUATION_INTERACTIVE_RECOVERY_REQUIRED"
                    case.safe_failure_message = "The interactive episode requires reconciliation."
                    case.observation = _interactive_recovery_projection(recovered)
                    case.completed_at = _now()
                continue
            if item is not None and item.category == "MULTI_STEP" and "scenario" in item.input:
                # A linked first turn being terminal cannot prove the whole
                # conversation finished. Never replay fixtures or mark partial
                # scenario work successful after a worker lease is lost.
                case.status = EvaluationCaseResultStatus.FAILED
                case.failure_code = "EVALUATION_SCENARIO_RECOVERY_REQUIRED"
                case.safe_failure_message = "The scenario requires durable outcome reconciliation."
                case.completed_at = _now()
                continue
            if case.agent_run_id is None:
                case.status = EvaluationCaseResultStatus.FAILED
                case.failure_code = "EVALUATION_AGENT_RUN_MISSING"
                case.safe_failure_message = "The evaluation case lost its durable AgentRun link."
                case.completed_at = _now()
                continue
            agent_run = await session.scalar(
                select(AgentRun).where(
                    AgentRun.workspace_id == case.workspace_id,
                    AgentRun.id == case.agent_run_id,
                )
            )
            if agent_run is None:
                case.status = EvaluationCaseResultStatus.FAILED
                case.failure_code = "EVALUATION_AGENT_RUN_MISSING"
                case.safe_failure_message = "The linked AgentRun was not found."
                case.completed_at = _now()
                continue
            if agent_run.status not in terminal_statuses:
                case.status = EvaluationCaseResultStatus.FAILED
                case.failure_code = "EVALUATION_AGENT_RUN_RECOVERY_REQUIRED"
                case.safe_failure_message = (
                    "The linked AgentRun was not terminal after lease recovery."
                )
                case.completed_at = _now()
                continue
            case.status = EvaluationCaseResultStatus.SUCCEEDED
            case.failure_code = None
            case.safe_failure_message = None
            case.observed_agent_status = agent_run.status
            case.observed_agent_failure_code = agent_run.failure_code
            case.observation = {
                "agent_run_id": str(agent_run.id),
                "agent_status": agent_run.status,
                "observed_agent_status": agent_run.status,
                "observed_agent_failure_code": agent_run.failure_code,
            }
            case.completed_at = _now()
        await session.commit()

        for case_id, owner, generation, envelope in recovered_results:
            await self.complete_case(
                session,
                case_id=case_id,
                run_id=run_id,
                owner=owner,
                generation=generation,
                observation=observation_from_envelope(envelope),
            )

    async def heartbeat(
        self,
        session: AsyncSession,
        *,
        run_id: UUID,
        owner: str,
        generation: int | None = None,
        lease_seconds: int,
    ) -> bool:
        now = _now()
        result = await session.execute(
            update(EvaluationExperimentRun)
            .where(
                EvaluationExperimentRun.id == run_id,
                EvaluationExperimentRun.status == EvaluationExperimentRunStatus.RUNNING,
                EvaluationExperimentRun.lease_owner == owner,
                EvaluationExperimentRun.lease_expires_at > now,
                *(
                    (EvaluationExperimentRun.lease_generation == generation,)
                    if generation is not None
                    else ()
                ),
            )
            .values(heartbeat_at=now, lease_expires_at=now + timedelta(seconds=lease_seconds))
        )
        await session.commit()
        return result.rowcount == 1

    async def claim_case(
        self,
        session: AsyncSession,
        *,
        run_id: UUID,
        owner: str,
        generation: int | None = None,
    ) -> EvaluationExperimentCaseResult | None:
        if generation is None:
            generation = await session.scalar(
                select(EvaluationExperimentRun.lease_generation).where(
                    EvaluationExperimentRun.id == run_id,
                    EvaluationExperimentRun.lease_owner == owner,
                    EvaluationExperimentRun.status == EvaluationExperimentRunStatus.RUNNING,
                )
            )
        if generation is None:
            await session.rollback()
            return None
        row = await session.scalar(
            select(EvaluationExperimentCaseResult)
            .where(
                EvaluationExperimentCaseResult.experiment_run_id == run_id,
                EvaluationExperimentCaseResult.status == EvaluationCaseResultStatus.PENDING,
                EvaluationExperimentRun.id == run_id,
                EvaluationExperimentRun.lease_owner == owner,
                EvaluationExperimentRun.lease_generation == generation,
                EvaluationExperimentRun.status == EvaluationExperimentRunStatus.RUNNING,
                EvaluationExperimentRun.lease_expires_at > _now(),
            )
            .join(
                EvaluationExperimentRun,
                and_(
                    EvaluationExperimentRun.workspace_id
                    == EvaluationExperimentCaseResult.workspace_id,
                    EvaluationExperimentRun.id == EvaluationExperimentCaseResult.experiment_run_id,
                ),
            )
            .join(
                EvaluationDatasetItem,
                and_(
                    EvaluationDatasetItem.workspace_id
                    == EvaluationExperimentCaseResult.workspace_id,
                    EvaluationDatasetItem.id == EvaluationExperimentCaseResult.dataset_item_id,
                ),
            )
            .join(
                EvaluationExperimentVariant,
                and_(
                    EvaluationExperimentVariant.workspace_id
                    == EvaluationExperimentCaseResult.workspace_id,
                    EvaluationExperimentVariant.id
                    == EvaluationExperimentCaseResult.experiment_variant_id,
                ),
            )
            .order_by(
                EvaluationDatasetItem.ordinal,
                EvaluationExperimentVariant.ordinal,
                EvaluationExperimentCaseResult.repetition_index,
            )
            .with_for_update(of=EvaluationExperimentCaseResult, skip_locked=True)
        )
        if row is None:
            await session.rollback()
            return None
        row.status = EvaluationCaseResultStatus.RUNNING
        row.started_at = _now()
        row.lease_owner = owner
        row.lease_generation = generation
        await session.commit()
        return row

    async def complete_case(
        self,
        session: AsyncSession,
        *,
        case_id: UUID,
        observation: CaseExecutionObservation,
        run_id: UUID | None = None,
        owner: str | None = None,
        generation: int | None = None,
    ) -> bool:
        variant_id = await session.scalar(
            select(EvaluationExperimentCaseResult.experiment_variant_id).where(
                EvaluationExperimentCaseResult.id == case_id
            )
        )
        variant = await session.scalar(
            select(EvaluationExperimentVariant).where(EvaluationExperimentVariant.id == variant_id)
        )
        pricing_status = "PRICING_BINDING_MISSING"
        cost_amount = None
        cost_currency = None
        if variant is not None:
            try:
                version = await session.scalar(
                    select(AgentVersion).where(
                        AgentVersion.workspace_id == variant.workspace_id,
                        AgentVersion.id == variant.agent_version_id,
                    )
                )
                if version is None:
                    raise PricingChainError("The frozen AgentVersion is missing.")
                parsed = parse_frozen_agent_spec(
                    version.resolved_spec,
                    workspace_id=variant.workspace_id,
                )
                snapshot_ids = pricing_snapshot_ids_from_metadata(
                    variant.variant_metadata,
                    model_count=len(parsed.model_plan.chain),
                    primary_snapshot_id=variant.pricing_snapshot_id,
                )
                snapshots = await load_pricing_snapshots(
                    session,
                    workspace_id=variant.workspace_id,
                    snapshot_ids=snapshot_ids,
                )
                validate_frozen_pricing_chain(
                    variant.variant_metadata,
                    parsed.model_plan,
                    snapshots,
                    primary_snapshot_id=variant.pricing_snapshot_id,
                    primary_snapshot_hash=variant.pricing_snapshot_hash,
                )

                computation = resolve_case_pricing(
                    model_plan=parsed.model_plan,
                    snapshots=snapshots,
                    model_attempts=tuple(
                        _model_attempt(attempt) for attempt in observation.model_attempts
                    ),
                    model_attempts_complete=observation.model_attempts_complete,
                    aggregate_billing_complete=observation.aggregate_billing_complete,
                    input_tokens=observation.input_tokens,
                    output_tokens=observation.output_tokens,
                    cached_tokens=observation.cached_tokens,
                )
                pricing_status = computation.unavailable_reason or "KNOWN"
                cost_amount = computation.amount
                cost_currency = computation.currency
            except (AgentHubError, PricingChainError, TypeError, ValueError):
                pricing_status = "PRICING_CHAIN_UNAVAILABLE"

        observation_projection = {
            **observation.observation,
            "model_attempts": [
                _safe_model_attempt_projection(attempt) for attempt in observation.model_attempts
            ],
            "model_attempt_collection_complete": observation.model_attempts_complete,
            "aggregate_billing_complete": observation.aggregate_billing_complete,
            "cost_status": pricing_status,
        }
        predicates = [
            EvaluationExperimentCaseResult.id == case_id,
            EvaluationExperimentCaseResult.status == EvaluationCaseResultStatus.RUNNING,
        ]
        if run_id is not None:
            predicates.extend(
                [
                    EvaluationExperimentCaseResult.experiment_run_id == run_id,
                    EvaluationExperimentCaseResult.lease_owner == owner,
                    EvaluationExperimentCaseResult.lease_generation == generation,
                    _current_run_fence(run_id, owner, generation),
                ]
            )
        result = await session.execute(
            update(EvaluationExperimentCaseResult)
            .where(*predicates)
            .values(
                status=EvaluationCaseResultStatus.SUCCEEDED,
                agent_run_id=observation.agent_run_id,
                latency_ms=observation.latency_ms,
                input_tokens=observation.input_tokens,
                output_tokens=observation.output_tokens,
                total_tokens=observation.total_tokens,
                cached_tokens=observation.cached_tokens,
                cost_amount=cost_amount,
                cost_currency=cost_currency,
                failure_code=None,
                safe_failure_message=None,
                observed_agent_status=observation.observation.get("observed_agent_status")
                or observation.observation.get("status"),
                observed_agent_failure_code=observation.observation.get(
                    "observed_agent_failure_code"
                )
                or observation.failure_code,
                observation=observation_projection,
                completed_at=_now(),
            )
        )
        await session.commit()
        return result.rowcount == 1

    async def fail_case(
        self,
        session: AsyncSession,
        *,
        case_id: UUID,
        failure_code: str,
        safe_message: str,
        run_id: UUID | None = None,
        owner: str | None = None,
        generation: int | None = None,
    ) -> bool:
        predicates = [
            EvaluationExperimentCaseResult.id == case_id,
            EvaluationExperimentCaseResult.status == EvaluationCaseResultStatus.RUNNING,
        ]
        if run_id is not None:
            predicates.extend(
                [
                    EvaluationExperimentCaseResult.experiment_run_id == run_id,
                    EvaluationExperimentCaseResult.lease_owner == owner,
                    EvaluationExperimentCaseResult.lease_generation == generation,
                    _current_run_fence(run_id, owner, generation),
                ]
            )
        result = await session.execute(
            update(EvaluationExperimentCaseResult)
            .where(*predicates)
            .values(
                status=EvaluationCaseResultStatus.FAILED,
                failure_code=failure_code,
                safe_failure_message=safe_message,
                completed_at=_now(),
            )
        )
        await session.commit()
        return result.rowcount == 1

    async def progress(self, session: AsyncSession, *, run_id: UUID) -> RunProgress:
        rows = await session.execute(
            select(EvaluationExperimentCaseResult.status, func.count())
            .where(EvaluationExperimentCaseResult.experiment_run_id == run_id)
            .group_by(EvaluationExperimentCaseResult.status)
        )
        counts = {str(status): int(count) for status, count in rows}
        return RunProgress(
            total=sum(counts.values()),
            pending=counts.get(EvaluationCaseResultStatus.PENDING, 0),
            running=counts.get(EvaluationCaseResultStatus.RUNNING, 0),
            completed=counts.get(EvaluationCaseResultStatus.SUCCEEDED, 0),
            failed=counts.get(EvaluationCaseResultStatus.FAILED, 0),
            cancelled=counts.get(EvaluationCaseResultStatus.CANCELLED, 0),
        )

    async def execute(self, *, run_id: UUID, owner: str, settings: Settings) -> None:
        async with self.session_factory() as session:
            initial = await session.scalar(
                select(EvaluationExperimentRun).where(EvaluationExperimentRun.id == run_id)
            )
        if initial is None or initial.status in {
            EvaluationExperimentRunStatus.CANCELLED,
            EvaluationExperimentRunStatus.FAILED,
            EvaluationExperimentRunStatus.SUCCEEDED,
        }:
            return
        async with self.session_factory() as session:
            await self.prepare_run(session, run_id=run_id)
        async with self.session_factory() as session:
            if not await self.claim_run(
                session,
                run_id=run_id,
                owner=owner,
                lease_seconds=settings.evaluation_runner_lease_seconds,
            ):
                return
        async with self.session_factory() as session:
            claimed_run = await session.scalar(
                select(EvaluationExperimentRun).where(
                    EvaluationExperimentRun.id == run_id,
                    EvaluationExperimentRun.lease_owner == owner,
                    EvaluationExperimentRun.status == EvaluationExperimentRunStatus.RUNNING,
                )
            )
            if claimed_run is None:
                return
            generation = claimed_run.lease_generation
        async with self.session_factory() as session:
            await self.recover_inflight_cases(session, run_id=run_id)
        try:
            async with self.session_factory() as session:
                await self._validate_execution_plan(session, run_id=run_id)
        except AgentHubError as error:
            message = (
                error.message
                if error.code == "INTERACTIVE_COST_RESERVATION_UNAVAILABLE"
                else "The frozen evaluation plan failed reproducibility validation."
            )
            async with self.session_factory() as session:
                await self._fail_run(
                    session,
                    run_id=run_id,
                    owner=owner,
                    generation=generation,
                    failure_code=error.code,
                    safe_message=message,
                )
            return
        run_span = await _safe_start_span(
            self.trace_sink,
            "evaluation.experiment.run",
            {
                "workspace_id": str(claimed_run.workspace_id),
                "experiment_id": str(claimed_run.experiment_id),
                "run_id": str(run_id),
                "attempt_count": claimed_run.attempt_count,
            },
        )
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(
                run_id=run_id,
                owner=owner,
                generation=generation,
                settings=settings,
                execution_task=asyncio.current_task(),
            )
        )
        span_status, span_failure = "completed", None
        try:
            while True:
                async with self.session_factory() as session:
                    run = await session.scalar(
                        select(EvaluationExperimentRun).where(EvaluationExperimentRun.id == run_id)
                    )
                    if (
                        run is None
                        or run.status
                        not in {
                            EvaluationExperimentRunStatus.RUNNING,
                            EvaluationExperimentRunStatus.CANCEL_REQUESTED,
                        }
                        or run.lease_owner != owner
                        or run.lease_generation != generation
                        or run.lease_expires_at is None
                        or run.lease_expires_at <= _now()
                    ):
                        span_status, span_failure = "lease_lost", "EVALUATION_LEASE_LOST"
                        return
                    if run.status == EvaluationExperimentRunStatus.CANCEL_REQUESTED:
                        span_status, span_failure = (
                            "cancel_requested",
                            "EVALUATION_CANCEL_REQUESTED",
                        )
                        await self._finish_run(
                            session, run_id=run_id, owner=owner, generation=generation
                        )
                        return
                    case = await self.claim_case(
                        session, run_id=run_id, owner=owner, generation=generation
                    )
                    if case is None:
                        finished = await self._finish_run(
                            session, run_id=run_id, owner=owner, generation=generation
                        )
                        if finished:
                            return
                        # A concurrent delivery may briefly hold a pending case.
                        # Keep the current lease alive rather than interpreting
                        # SKIP LOCKED as proof that all work has completed.
                        await asyncio.sleep(0.05)
                        continue
                    variant = await session.scalar(
                        select(EvaluationExperimentVariant).where(
                            EvaluationExperimentVariant.workspace_id == run.workspace_id,
                            EvaluationExperimentVariant.id == case.experiment_variant_id,
                        )
                    )
                    item = await session.scalar(
                        select(EvaluationDatasetItem).where(
                            EvaluationDatasetItem.workspace_id == run.workspace_id,
                            EvaluationDatasetItem.id == case.dataset_item_id,
                        )
                    )
                if variant is None or item is None:
                    async with self.session_factory() as session:
                        await self.fail_case(
                            session,
                            case_id=case.id,
                            run_id=run_id,
                            owner=owner,
                            generation=generation,
                            failure_code="EVALUATION_PLAN_INVALID",
                            safe_message="The persisted evaluation plan is invalid.",
                        )
                    continue
                try:
                    prepared = await self._prepare_driver(
                        run=run,
                        variant=variant,
                        item=item,
                        case_identity=CaseExecutionIdentity(
                            workspace_id=run.workspace_id,
                            run_id=run.id,
                            case_id=case.id,
                            variant_id=variant.id,
                            item_id=item.id,
                            repetition_index=case.repetition_index,
                            owner=owner,
                            generation=generation,
                        ),
                    )
                    if prepared.agent_run_id is not None:
                        async with self.session_factory() as session:
                            linked = await self.bind_case_agent_run(
                                session,
                                case_id=case.id,
                                run_id=run_id,
                                agent_run_id=prepared.agent_run_id,
                                owner=owner,
                                generation=generation,
                                preparation_observation=prepared.preparation_observation,
                            )
                        if not linked:
                            return
                    case_span = await _safe_start_span(
                        self.trace_sink,
                        "evaluation.case",
                        {
                            "workspace_id": str(run.workspace_id),
                            "run_id": str(run.id),
                            "case_id": str(case.id),
                            "variant_id": str(variant.id),
                            "dataset_item_id": str(item.id),
                            "category": str(item.category),
                        },
                    )
                    try:
                        result = await prepared.execute()
                    except BaseException as error:
                        failure = (
                            "EVALUATION_EXECUTION_INTERRUPTED"
                            if isinstance(error, asyncio.CancelledError)
                            else "EVALUATION_CASE_FAILED"
                        )
                        await _safe_end_span(
                            case_span,
                            {"status": "FAILED", "failure_code": failure},
                            status="error",
                            failure_code=failure,
                        )
                        raise
                    await _safe_end_span(
                        case_span,
                        {
                            "status": "SUCCEEDED",
                            "latency_ms": result.latency_ms,
                            "observed_agent_status": result.observation.get(
                                "observed_agent_status"
                            ),
                            "observed_agent_failure_code": result.observation.get(
                                "observed_agent_failure_code"
                            ),
                        },
                        status="ok",
                        failure_code=None,
                    )
                    async with self.session_factory() as session:
                        await self.complete_case(
                            session,
                            case_id=case.id,
                            observation=result,
                            run_id=run_id,
                            owner=owner,
                            generation=generation,
                        )
                except Exception as exc:
                    logger.warning(
                        "evaluation_case_failed",
                        extra={"case_id": str(case.id), "error_type": type(exc).__name__},
                    )
                    async with self.session_factory() as session:
                        await self.fail_case(
                            session,
                            case_id=case.id,
                            run_id=run_id,
                            owner=owner,
                            generation=generation,
                            failure_code=(getattr(exc, "code", None) or "EVALUATION_CASE_FAILED"),
                            safe_message="The evaluation case failed.",
                        )
        except asyncio.CancelledError:
            span_status, span_failure = "interrupted", "EVALUATION_EXECUTION_INTERRUPTED"
            raise
        except BaseException:
            span_status, span_failure = "failed", "EVALUATION_EXECUTION_INTERRUPTED"
            raise
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            await _safe_end_span(
                run_span,
                {"status": span_status},
                status="error" if span_failure is not None else "ok",
                failure_code=span_failure,
            )

    async def _prepare_driver(
        self,
        *,
        run: EvaluationExperimentRun,
        variant: EvaluationExperimentVariant,
        item: EvaluationDatasetItem,
        case_identity: CaseExecutionIdentity | None = None,
    ) -> PreparedCaseExecution:
        prepare = getattr(self.driver, "prepare", None)
        if prepare is not None:
            if "case_identity" in inspect.signature(prepare).parameters:
                return await prepare(
                    run=run, variant=variant, item=item, case_identity=case_identity
                )
            return await prepare(run=run, variant=variant, item=item)
        execute = self.driver.execute
        parameters = inspect.signature(execute).parameters
        if "session" in parameters:
            return PreparedCaseExecution(
                agent_run_id=None,
                execute=lambda: execute(None, run=run, variant=variant, item=item),
            )
        return PreparedCaseExecution(
            agent_run_id=None,
            execute=lambda: execute(run=run, variant=variant, item=item),
        )

    async def _heartbeat_loop(
        self,
        *,
        run_id: UUID,
        owner: str,
        generation: int,
        settings: Settings,
        execution_task: asyncio.Task | None = None,
    ) -> None:
        while True:
            await asyncio.sleep(settings.evaluation_runner_heartbeat_seconds)
            try:
                async with self.session_factory() as session:
                    owned = await self.heartbeat(
                        session,
                        run_id=run_id,
                        owner=owner,
                        generation=generation,
                        lease_seconds=settings.evaluation_runner_lease_seconds,
                    )
            except SQLAlchemyError:
                logger.warning("evaluation_heartbeat_unavailable", extra={"run_id": str(run_id)})
                owned = False
            if not owned:
                if execution_task is not None:
                    execution_task.cancel()
                return

    async def _validate_execution_plan(self, session: AsyncSession, *, run_id: UUID) -> None:
        run = await session.scalar(
            select(EvaluationExperimentRun).where(EvaluationExperimentRun.id == run_id)
        )
        if run is None:
            raise AgentHubError(
                "EXPERIMENT_REPRODUCIBILITY_VIOLATION",
                "The evaluation run was not found.",
                409,
            )
        experiment = await session.scalar(
            select(EvaluationExperiment).where(
                EvaluationExperiment.workspace_id == run.workspace_id,
                EvaluationExperiment.id == run.experiment_id,
            )
        )
        dataset_version = await session.scalar(
            select(EvaluationDatasetVersion).where(
                EvaluationDatasetVersion.workspace_id == run.workspace_id,
                EvaluationDatasetVersion.id == run.dataset_version_id,
            )
        )
        if (
            experiment is None
            or experiment.status != "READY"
            or not experiment.spec_json
            or experiment.spec_hash is None
            or experiment.spec_hash != run.experiment_spec_hash
            or experiment_spec_hash(experiment.spec_json) != run.experiment_spec_hash
            or dataset_version is None
            or dataset_version.content_hash != run.dataset_hash
            or dataset_version.id != experiment.dataset_version_id
            or experiment.dataset_content_hash != run.dataset_hash
            or experiment.split != run.split
            or experiment.purpose != run.purpose
            or experiment.repetitions != run.repetitions
            or experiment.build_sha != run.git_commit
        ):
            raise AgentHubError(
                "EXPERIMENT_REPRODUCIBILITY_VIOLATION",
                "The persisted experiment run no longer matches its frozen definition.",
                409,
            )
        variants = list(
            await session.scalars(
                select(EvaluationExperimentVariant).where(
                    EvaluationExperimentVariant.workspace_id == run.workspace_id,
                    EvaluationExperimentVariant.experiment_id == run.experiment_id,
                )
            )
        )
        capped_items = list(
            await session.scalars(
                select(EvaluationDatasetItem).where(
                    EvaluationDatasetItem.workspace_id == run.workspace_id,
                    EvaluationDatasetItem.dataset_version_id == run.dataset_version_id,
                    EvaluationDatasetItem.split == run.split,
                    EvaluationDatasetItem.category == "INTERACTIVE",
                    EvaluationDatasetItem.input.has_key("max_cost_cny"),
                )
            )
        )
        preflight = getattr(self.driver, "preflight_case_budget", None)
        if capped_items and preflight is None:
            raise reservation_unavailable()
        for variant in variants:
            await self._validate_variant(session, run.workspace_id, variant)
            for item in capped_items:
                await preflight(
                    session,
                    variant=variant,
                    item=item,
                    evaluator_manifest=experiment.evaluator_manifest,
                )

    async def _validate_variant(
        self,
        session: AsyncSession,
        workspace_id: UUID,
        variant: EvaluationExperimentVariant,
    ) -> None:
        from packages.agent_runtime.models import AgentVersion

        version = await session.scalar(
            select(AgentVersion).where(
                AgentVersion.workspace_id == workspace_id,
                AgentVersion.id == variant.agent_version_id,
            )
        )
        pricing = await session.scalar(
            select(PricingSnapshot).where(
                PricingSnapshot.workspace_id == workspace_id,
                PricingSnapshot.id == variant.pricing_snapshot_id,
            )
        )
        if version is None or pricing is None:
            raise AgentHubError(
                "EXPERIMENT_REPRODUCIBILITY_VIOLATION",
                "A frozen evaluation dependency is missing.",
                409,
            )
        if canonical_json_hash(version.resolved_spec) != variant.resolved_spec_hash:
            raise AgentHubError(
                "EXPERIMENT_REPRODUCIBILITY_VIOLATION",
                "The AgentVersion hash no longer matches the variant.",
                409,
            )
        try:
            parsed = parse_frozen_agent_spec(version.resolved_spec, workspace_id=workspace_id)
        except AgentHubError as exc:
            raise AgentHubError(
                "EXPERIMENT_REPRODUCIBILITY_VIOLATION",
                "The frozen AgentVersion is invalid.",
                409,
            ) from exc
        try:
            snapshot_ids = pricing_snapshot_ids_from_metadata(
                variant.variant_metadata,
                model_count=len(parsed.model_plan.chain),
                primary_snapshot_id=variant.pricing_snapshot_id,
            )
            pricing_snapshots = await load_pricing_snapshots(
                session,
                workspace_id=workspace_id,
                snapshot_ids=snapshot_ids,
            )
            validate_frozen_pricing_chain(
                variant.variant_metadata,
                parsed.model_plan,
                pricing_snapshots,
                primary_snapshot_id=variant.pricing_snapshot_id,
                primary_snapshot_hash=variant.pricing_snapshot_hash,
            )
        except (AgentHubError, PricingChainError) as exc:
            raise AgentHubError(
                "EXPERIMENT_REPRODUCIBILITY_VIOLATION",
                "The frozen pricing chain no longer matches the AgentVersion.",
                409,
            ) from exc
        snapshots = normalize_knowledge_snapshots(variant.effective_knowledge_snapshots)
        for binding in snapshots:
            snapshot = await session.scalar(
                select(KnowledgeSnapshot).where(
                    KnowledgeSnapshot.workspace_id == workspace_id,
                    KnowledgeSnapshot.knowledge_base_id == UUID(binding["knowledge_base_id"]),
                    KnowledgeSnapshot.id == UUID(binding["snapshot_id"]),
                )
            )
            if snapshot is None or snapshot.content_hash != binding["snapshot_content_hash"]:
                raise AgentHubError(
                    "EXPERIMENT_REPRODUCIBILITY_VIOLATION",
                    "A frozen knowledge snapshot changed.",
                    409,
                )
        if (
            variant_hash(
                agent_version_id=str(variant.agent_version_id),
                resolved_spec_hash=variant.resolved_spec_hash,
                effective_knowledge_snapshots=snapshots,
                pricing_snapshot_id=str(variant.pricing_snapshot_id),
                pricing_snapshot_hash=variant.pricing_snapshot_hash,
                variant_metadata=variant.variant_metadata,
            )
            != variant.variant_hash
        ):
            raise AgentHubError(
                "EXPERIMENT_REPRODUCIBILITY_VIOLATION",
                "The frozen variant hash is invalid.",
                409,
            )

    async def _fail_run(
        self,
        session: AsyncSession,
        *,
        run_id: UUID,
        owner: str,
        generation: int,
        failure_code: str,
        safe_message: str,
    ) -> None:
        result = await session.execute(
            update(EvaluationExperimentRun)
            .where(
                EvaluationExperimentRun.id == run_id,
                EvaluationExperimentRun.status == EvaluationExperimentRunStatus.RUNNING,
                EvaluationExperimentRun.lease_owner == owner,
                EvaluationExperimentRun.lease_generation == generation,
            )
            .values(
                status=EvaluationExperimentRunStatus.FAILED,
                failure_code=failure_code,
                safe_failure_message=safe_message,
                completed_at=_now(),
                lease_owner=None,
                lease_expires_at=None,
                heartbeat_at=None,
            )
        )
        if result.rowcount == 1:
            # A plan-wide rejection dispatched none of the pending cases. Give
            # each a terminal, inspectable outcome along with its parent.
            await session.execute(
                update(EvaluationExperimentCaseResult)
                .where(
                    EvaluationExperimentCaseResult.experiment_run_id == run_id,
                    EvaluationExperimentCaseResult.status == EvaluationCaseResultStatus.PENDING,
                )
                .values(
                    status=EvaluationCaseResultStatus.FAILED,
                    failure_code=failure_code,
                    safe_failure_message=safe_message,
                    completed_at=_now(),
                )
            )
        await session.commit()

    async def cancel(self, session: AsyncSession, *, run_id: UUID) -> bool:
        current_status = await session.scalar(
            select(EvaluationExperimentRun.status).where(EvaluationExperimentRun.id == run_id)
        )
        result = await session.execute(
            update(EvaluationExperimentRun)
            .where(
                EvaluationExperimentRun.id == run_id,
                EvaluationExperimentRun.status.in_(
                    [
                        EvaluationExperimentRunStatus.QUEUED,
                        EvaluationExperimentRunStatus.RUNNING,
                    ]
                ),
            )
            .values(
                status=sql_case(
                    (
                        EvaluationExperimentRun.status == EvaluationExperimentRunStatus.QUEUED,
                        EvaluationExperimentRunStatus.CANCELLED,
                    ),
                    else_=EvaluationExperimentRunStatus.CANCEL_REQUESTED,
                ),
                completed_at=sql_case(
                    (
                        EvaluationExperimentRun.status == EvaluationExperimentRunStatus.QUEUED,
                        _now(),
                    ),
                    else_=EvaluationExperimentRun.completed_at,
                ),
            )
        )
        if current_status == EvaluationExperimentRunStatus.QUEUED:
            await session.execute(
                update(EvaluationExperimentCaseResult)
                .where(
                    EvaluationExperimentCaseResult.experiment_run_id == run_id,
                    EvaluationExperimentCaseResult.status == EvaluationCaseResultStatus.PENDING,
                )
                .values(status=EvaluationCaseResultStatus.CANCELLED, completed_at=_now())
            )
        await session.commit()
        return result.rowcount == 1

    async def _finish_run(
        self,
        session: AsyncSession,
        *,
        run_id: UUID,
        owner: str,
        generation: int | None = None,
    ) -> bool:
        predicates = [
            EvaluationExperimentRun.id == run_id,
            EvaluationExperimentRun.lease_owner == owner,
            EvaluationExperimentRun.lease_expires_at > _now(),
            EvaluationExperimentRun.status.in_(
                (
                    EvaluationExperimentRunStatus.RUNNING,
                    EvaluationExperimentRunStatus.CANCEL_REQUESTED,
                )
            ),
        ]
        if generation is not None:
            predicates.append(EvaluationExperimentRun.lease_generation == generation)
        run = await session.scalar(
            select(EvaluationExperimentRun).where(*predicates).with_for_update()
        )
        if run is None:
            return False
        counts = await self.progress(session, run_id=run_id)
        if counts.running or (
            counts.pending and run.status != EvaluationExperimentRunStatus.CANCEL_REQUESTED
        ):
            await session.rollback()
            return False
        if run.status == EvaluationExperimentRunStatus.CANCEL_REQUESTED:
            await session.execute(
                update(EvaluationExperimentCaseResult)
                .where(
                    EvaluationExperimentCaseResult.experiment_run_id == run_id,
                    EvaluationExperimentCaseResult.status == EvaluationCaseResultStatus.PENDING,
                )
                .values(status=EvaluationCaseResultStatus.CANCELLED, completed_at=_now())
            )
            run.status = EvaluationExperimentRunStatus.CANCELLED
        elif counts.failed:
            run.status = EvaluationExperimentRunStatus.FAILED
        else:
            run.status = EvaluationExperimentRunStatus.SUCCEEDED
        run.completed_at = _now()
        run.lease_owner = None
        run.lease_expires_at = None
        run.heartbeat_at = None
        await session.commit()
        return True


async def reconcile_experiment_runs(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    queue: Any,
    settings: Settings,
) -> tuple[UUID, ...]:
    now = _now()
    grace_cutoff = now - timedelta(seconds=settings.evaluation_enqueue_grace_seconds)
    requeued: list[UUID] = []
    async with session_factory() as session:
        runs = list(
            await session.scalars(
                select(EvaluationExperimentRun)
                .where(
                    or_(
                        and_(
                            EvaluationExperimentRun.status == EvaluationExperimentRunStatus.QUEUED,
                            EvaluationExperimentRun.created_at <= grace_cutoff,
                        ),
                        and_(
                            EvaluationExperimentRun.status == EvaluationExperimentRunStatus.RUNNING,
                            EvaluationExperimentRun.lease_expires_at.is_not(None),
                            EvaluationExperimentRun.lease_expires_at <= now,
                        ),
                        EvaluationExperimentRun.status
                        == EvaluationExperimentRunStatus.CANCEL_REQUESTED,
                    )
                )
                .order_by(EvaluationExperimentRun.created_at, EvaluationExperimentRun.id)
                .limit(settings.evaluation_reconciliation_batch_size)
                .with_for_update(skip_locked=True)
            )
        )
        for run in runs:
            if run.status == EvaluationExperimentRunStatus.CANCEL_REQUESTED:
                running_cases = await session.scalar(
                    select(func.count(EvaluationExperimentCaseResult.id)).where(
                        EvaluationExperimentCaseResult.workspace_id == run.workspace_id,
                        EvaluationExperimentCaseResult.experiment_run_id == run.id,
                        EvaluationExperimentCaseResult.status == EvaluationCaseResultStatus.RUNNING,
                    )
                )
                if running_cases:
                    lease_is_stale = run.lease_expires_at is None or run.lease_expires_at <= now
                    if not lease_is_stale:
                        continue
                    inflight = list(
                        await session.scalars(
                            select(EvaluationExperimentCaseResult)
                            .where(
                                EvaluationExperimentCaseResult.workspace_id == run.workspace_id,
                                EvaluationExperimentCaseResult.experiment_run_id == run.id,
                                EvaluationExperimentCaseResult.status
                                == EvaluationCaseResultStatus.RUNNING,
                            )
                            .with_for_update()
                        )
                    )
                    for case in inflight:
                        category = await session.scalar(
                            select(EvaluationDatasetItem.category).where(
                                EvaluationDatasetItem.workspace_id == case.workspace_id,
                                EvaluationDatasetItem.id == case.dataset_item_id,
                            )
                        )
                        if category == "INTERACTIVE":
                            recovered = await InteractiveLifecycle(session_factory).recover_case(
                                session, case, cancelled=True
                            )
                            envelope = recovered.get("envelope")
                            case.status = EvaluationCaseResultStatus.CANCELLED
                            case.failure_code = "EVALUATION_INTERACTIVE_CANCELLED"
                            case.safe_failure_message = (
                                "The interactive episode was cancelled; cleanup is independent."
                            )
                            if recovered.get("agent_run_id"):
                                case.agent_run_id = UUID(recovered["agent_run_id"])
                            case.observed_agent_status = recovered.get("observed_agent_status")
                            case.observed_agent_failure_code = recovered.get(
                                "observed_agent_failure_code"
                            )
                            if envelope is not None:
                                case.observation = with_cleanup(
                                    envelope, recovered["cleanup_status"]
                                )["observation"]
                                case.observation = {
                                    **case.observation,
                                    "stop_reason": "cancelled",
                                    "execution_complete": False,
                                }
                            else:
                                case.observation = _interactive_recovery_projection(recovered)
                            case.completed_at = now
                            continue
                        if case.agent_run_id is None:
                            case.status = EvaluationCaseResultStatus.CANCELLED
                            case.completed_at = now
                        else:
                            agent_run = await session.scalar(
                                select(AgentRun).where(
                                    AgentRun.workspace_id == case.workspace_id,
                                    AgentRun.id == case.agent_run_id,
                                )
                            )
                            if agent_run is not None and agent_run.status in {
                                "SUCCEEDED",
                                "FAILED",
                                "NEEDS_ATTENTION",
                                "CANCELLED",
                            }:
                                case.status = EvaluationCaseResultStatus.SUCCEEDED
                                case.observed_agent_status = agent_run.status
                                case.observed_agent_failure_code = agent_run.failure_code
                                case.observation = {
                                    "agent_run_id": str(agent_run.id),
                                    "observed_agent_status": agent_run.status,
                                    "observed_agent_failure_code": agent_run.failure_code,
                                }
                            else:
                                case.status = EvaluationCaseResultStatus.FAILED
                                case.failure_code = "EVALUATION_CANCEL_RECOVERY_REQUIRED"
                                case.safe_failure_message = (
                                    "The linked AgentRun remained non-terminal during cancellation."
                                )
                            case.completed_at = now
                await session.execute(
                    update(EvaluationExperimentCaseResult)
                    .where(
                        EvaluationExperimentCaseResult.workspace_id == run.workspace_id,
                        EvaluationExperimentCaseResult.experiment_run_id == run.id,
                        EvaluationExperimentCaseResult.status == EvaluationCaseResultStatus.PENDING,
                    )
                    .values(
                        status=EvaluationCaseResultStatus.CANCELLED,
                        completed_at=now,
                    )
                )
                run.status = EvaluationExperimentRunStatus.CANCELLED
                run.completed_at = now
                run.lease_owner = None
                run.lease_expires_at = None
                run.heartbeat_at = None
                continue
            run.status = EvaluationExperimentRunStatus.QUEUED
            run.lease_owner = None
            run.lease_expires_at = None
            run.heartbeat_at = None
            requeued.append(run.id)
        await session.commit()
    for run_id in requeued:
        try:
            await queue.enqueue(run_id)
        except Exception:
            logger.warning(
                "evaluation_reconciliation_enqueue_failed", extra={"run_id": str(run_id)}
            )
    await InteractiveLifecycle(session_factory).reconcile_cleanup(
        limit=settings.evaluation_reconciliation_batch_size
    )
    return tuple(requeued)


def _current_run_fence(run_id: UUID, owner: str | None, generation: int | None):
    return (
        select(EvaluationExperimentRun.id)
        .where(
            EvaluationExperimentRun.id == run_id,
            EvaluationExperimentRun.lease_owner == owner,
            EvaluationExperimentRun.lease_generation == generation,
            EvaluationExperimentRun.status == EvaluationExperimentRunStatus.RUNNING,
            EvaluationExperimentRun.lease_expires_at > _now(),
        )
        .exists()
    )


def _interactive_recovery_projection(recovered: dict[str, Any]) -> dict[str, Any]:
    return {
        "category": "INTERACTIVE",
        "stop_reason": "recovery_required",
        "execution_complete": False,
        "agent_execution_complete": False,
        "environment_lifecycle_status": recovered.get("cleanup_status", "CLEANUP_BLOCKED"),
        "turns": recovered.get("turns", []),
        "interactive_recovery": {
            "reason": recovered.get("reason", "EVALUATION_INTERACTIVE_RECOVERY_REQUIRED"),
            "cleanup_status": recovered.get("cleanup_status", "CLEANUP_BLOCKED"),
        },
    }


__all__ = [
    "AgentRuntimeEvaluationDriver",
    "CaseExecutionDriver",
    "CaseExecutionObservation",
    "DeterministicEvaluationDriver",
    "ExperimentRunner",
    "RunProgress",
    "reconcile_experiment_runs",
]


async def _safe_start_span(
    sink: TraceSink, name: str, attributes: dict[str, Any]
) -> TraceSpan | None:
    try:
        return await sink.start_span(name, attributes)
    except Exception:
        logger.warning("evaluation_trace_start_failed", exc_info=True)
        return None


async def _safe_end_span(
    span: TraceSpan | None,
    attributes: dict[str, Any],
    *,
    status: str,
    failure_code: str | None,
) -> None:
    if span is None:
        return
    try:
        await span.end(attributes=attributes, status=status, failure_code=failure_code)
    except Exception:
        logger.warning("evaluation_trace_end_failed", exc_info=True)
