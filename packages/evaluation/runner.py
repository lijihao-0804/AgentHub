"""Durable M7-C experiment execution primitives.

The database is the source of truth.  Celery only transports an experiment run id;
workers rebuild the frozen execution plan from the persisted M7-B records.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.models import AgentRun
from packages.agent_runtime.runtime import AgentRunService
from packages.approvals.contracts import ApprovalDecisionStatus
from packages.approvals.models import Approval
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.config.settings import Settings
from packages.evaluation.models import (
    EvaluationCaseResultStatus,
    EvaluationDatasetItem,
    EvaluationExperimentCaseResult,
    EvaluationExperimentRun,
    EvaluationExperimentRunStatus,
    EvaluationExperimentVariant,
    PricingSnapshot,
)
from packages.knowledge.contracts import KnowledgeRetriever, RetrievalQuery

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CaseExecutionObservation:
    observation: dict[str, Any]
    agent_run_id: UUID | None = None
    latency_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    cached_tokens: int | None = None
    cost_amount: Decimal | None = None
    cost_currency: str | None = None
    failure_code: str | None = None
    safe_failure_message: str | None = None


class CaseExecutionDriver(Protocol):
    async def execute(
        self,
        session: AsyncSession,
        *,
        run: EvaluationExperimentRun,
        variant: EvaluationExperimentVariant,
        item: EvaluationDatasetItem,
    ) -> CaseExecutionObservation: ...


class DeterministicEvaluationDriver:
    """Offline-safe driver used by synthetic evaluation workspaces and CI.

    It records only hashes and typed identities.  Production integrations can inject a
    driver backed by AgentRunService without changing durable claim or result semantics.
    """

    async def execute(
        self,
        session: AsyncSession,
        *,
        run: EvaluationExperimentRun,
        variant: EvaluationExperimentVariant,
        item: EvaluationDatasetItem,
    ) -> CaseExecutionObservation:
        del session, run
        started = time.perf_counter()
        category = str(item.category)
        observation: dict[str, Any] = {
            "category": category,
            "input_hash": canonical_json_hash(item.input),
            "variant_hash": variant.variant_hash,
        }
        if category == "RETRIEVAL":
            observation["citation_ids"] = list(item.expected.get("relevant_chunk_ids", []))
        elif category == "KNOWLEDGE_QA":
            observation["citation_ids"] = list(item.expected.get("citations", []))
            observation["answer_hash"] = canonical_json_hash(item.expected.get("answer", ""))
        elif category == "TOOL":
            observation["tool_identity"] = item.expected.get("tool_identity")
            observation["arguments_hash"] = canonical_json_hash(item.expected.get("arguments", {}))
        elif category == "NO_ANSWER":
            observation["answer_hash"] = canonical_json_hash(item.expected.get("answer", ""))
        elif category == "APPROVAL":
            observation["decision"] = item.expected.get("decision")
        elif category == "MULTI_STEP":
            observation["step_count"] = len(item.expected.get("steps", []))
        elif category == "FAILURE":
            observation["expected_status"] = item.expected.get("status")
            observation["expected_failure_code"] = item.expected.get("failure_code")
        return CaseExecutionObservation(
            observation=observation,
            latency_ms=max(0, round((time.perf_counter() - started) * 1000)),
        )


class AgentRuntimeEvaluationDriver:
    """Production driver that delegates execution to the frozen Agent Runtime.

    The context factory re-resolves workspace membership for the run creator.  Evaluation
    never fabricates an elevated context, and approval cases use ApprovalService.decide plus
    AgentRunService.resume rather than mutating approval rows directly.
    """

    def __init__(
        self,
        agent_run_service: AgentRunService,
        context_factory: Callable[[EvaluationExperimentRun], Awaitable[Any]],
        *,
        retriever: KnowledgeRetriever | None = None,
    ) -> None:
        self.agent_run_service = agent_run_service
        self.context_factory = context_factory
        self.retriever = retriever

    async def execute(
        self,
        session: AsyncSession,
        *,
        run: EvaluationExperimentRun,
        variant: EvaluationExperimentVariant,
        item: EvaluationDatasetItem,
    ) -> CaseExecutionObservation:
        context = await self.context_factory(run)
        started = time.perf_counter()
        if item.category == "RETRIEVAL":
            if self.retriever is None or not variant.effective_knowledge_snapshots:
                raise RuntimeError("EVALUATION_RETRIEVAL_NOT_CONFIGURED")
            binding = variant.effective_knowledge_snapshots[0]
            result = await self.retriever.retrieve_with_trace(
                context,
                RetrievalQuery(
                    text=str(item.input["query"]),
                    knowledge_base_id=str(binding["knowledge_base_id"]),
                    knowledge_snapshot_id=str(binding["snapshot_id"]),
                ),
            )
            return CaseExecutionObservation(
                observation={
                    "category": item.category,
                    "chunk_ids": [e.chunk_id for e in result.evidence],
                    "variant_hash": variant.variant_hash,
                },
                latency_ms=max(0, round((time.perf_counter() - started) * 1000)),
            )

        result = await self.agent_run_service.run(
            context,
            agent_version_id=variant.agent_version_id,
            input_text=_case_input_text(item),
        )
        if result.status == "WAITING_APPROVAL":
            approval = await session.scalar(
                select(Approval).where(
                    Approval.workspace_id == run.workspace_id,
                    Approval.run_id == result.run_id,
                )
            )
            if approval is None:
                raise RuntimeError("EVALUATION_APPROVAL_NOT_FOUND")
            expected_decision = str(item.expected.get("decision", "DENIED")).upper()
            decision = (
                ApprovalDecisionStatus.APPROVED
                if expected_decision in {"APPROVED", "APPROVE"}
                else ApprovalDecisionStatus.DENIED
            )
            if self.agent_run_service.approval_service is None:
                raise RuntimeError("EVALUATION_APPROVAL_SERVICE_NOT_CONFIGURED")
            await self.agent_run_service.approval_service.decide(
                context, approval.id, decision=decision
            )
            result = await self.agent_run_service.resume(
                context, run_id=result.run_id, approval_id=approval.id
            )
        return CaseExecutionObservation(
            observation={
                "category": item.category,
                "agent_run_id": str(result.run_id),
                "status": result.status,
                "model_step_count": result.model_step_count,
                "tool_call_count": result.tool_call_count,
                "output_hash": canonical_json_hash(result.final_output or ""),
                "variant_hash": variant.variant_hash,
            },
            agent_run_id=result.run_id,
            latency_ms=max(0, round((time.perf_counter() - started) * 1000)),
            failure_code=result.failure_code if result.status != "SUCCEEDED" else None,
        )


def _case_input_text(item: EvaluationDatasetItem) -> str:
    for field in ("query", "question", "request", "action", "task", "scenario"):
        value = item.input.get(field)
        if isinstance(value, str):
            return value
    raise RuntimeError("EVALUATION_CASE_INPUT_INVALID")


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
    ) -> None:
        self.session_factory = session_factory
        self.driver = driver or DeterministicEvaluationDriver()

    async def prepare_run(self, session: AsyncSession, *, run_id: UUID) -> int:
        run = await session.scalar(
            select(EvaluationExperimentRun)
            .where(EvaluationExperimentRun.id == run_id)
            .with_for_update()
        )
        if run is None:
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
            )
        )
        await session.commit()
        return result.rowcount == 1

    async def recover_inflight_cases(self, session: AsyncSession, *, run_id: UUID) -> None:
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
            if case.agent_run_id is None:
                case.status = EvaluationCaseResultStatus.PENDING
                case.started_at = None
                continue
            agent_run = await session.scalar(
                select(AgentRun).where(
                    AgentRun.workspace_id == case.workspace_id,
                    AgentRun.id == case.agent_run_id,
                )
            )
            if agent_run is None or agent_run.status not in terminal_statuses:
                continue
            case.status = (
                EvaluationCaseResultStatus.SUCCEEDED
                if agent_run.status == "SUCCEEDED"
                else EvaluationCaseResultStatus.FAILED
            )
            case.failure_code = agent_run.failure_code
            case.safe_failure_message = (
                "The linked agent run completed with an unsuccessful status."
                if agent_run.status != "SUCCEEDED"
                else None
            )
            case.observation = {
                "agent_run_id": str(agent_run.id),
                "agent_status": agent_run.status,
            }
            case.completed_at = _now()
        await session.commit()

    async def heartbeat(
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
                EvaluationExperimentRun.status == EvaluationExperimentRunStatus.RUNNING,
                EvaluationExperimentRun.lease_owner == owner,
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
    ) -> EvaluationExperimentCaseResult | None:
        del owner
        row = await session.scalar(
            select(EvaluationExperimentCaseResult)
            .where(
                EvaluationExperimentCaseResult.experiment_run_id == run_id,
                EvaluationExperimentCaseResult.status == EvaluationCaseResultStatus.PENDING,
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
            .with_for_update(skip_locked=True)
        )
        if row is None:
            await session.rollback()
            return None
        row.status = EvaluationCaseResultStatus.RUNNING
        row.started_at = _now()
        await session.commit()
        return row

    async def complete_case(
        self,
        session: AsyncSession,
        *,
        case_id: UUID,
        observation: CaseExecutionObservation,
    ) -> bool:
        cost_amount = observation.cost_amount
        cost_currency = observation.cost_currency
        if cost_amount is None and (
            observation.input_tokens is not None or observation.output_tokens is not None
        ):
            variant_id = await session.scalar(
                select(EvaluationExperimentCaseResult.experiment_variant_id).where(
                    EvaluationExperimentCaseResult.id == case_id
                )
            )
            variant = await session.scalar(
                select(EvaluationExperimentVariant).where(
                    EvaluationExperimentVariant.id == variant_id
                )
            )
            pricing = (
                await session.scalar(
                    select(PricingSnapshot).where(
                        PricingSnapshot.workspace_id == variant.workspace_id,
                        PricingSnapshot.id == variant.pricing_snapshot_id,
                    )
                )
                if variant is not None
                else None
            )
            if pricing is not None:
                input_cost = Decimal(observation.input_tokens or 0) / Decimal(1_000_000)
                output_cost = Decimal(observation.output_tokens or 0) / Decimal(1_000_000)
                cost_amount = (
                    input_cost * Decimal(pricing.input_price_per_1m)
                    + output_cost * Decimal(pricing.output_price_per_1m)
                ).quantize(Decimal("0.00000001"))
                cost_currency = pricing.currency
        result = await session.execute(
            update(EvaluationExperimentCaseResult)
            .where(
                EvaluationExperimentCaseResult.id == case_id,
                EvaluationExperimentCaseResult.status == EvaluationCaseResultStatus.RUNNING,
            )
            .values(
                status=(
                    EvaluationCaseResultStatus.FAILED
                    if observation.failure_code
                    else EvaluationCaseResultStatus.SUCCEEDED
                ),
                agent_run_id=observation.agent_run_id,
                latency_ms=observation.latency_ms,
                input_tokens=observation.input_tokens,
                output_tokens=observation.output_tokens,
                total_tokens=observation.total_tokens,
                cached_tokens=observation.cached_tokens,
                cost_amount=cost_amount,
                cost_currency=cost_currency,
                failure_code=observation.failure_code,
                safe_failure_message=observation.safe_failure_message,
                observation=observation.observation,
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
    ) -> bool:
        result = await session.execute(
            update(EvaluationExperimentCaseResult)
            .where(
                EvaluationExperimentCaseResult.id == case_id,
                EvaluationExperimentCaseResult.status == EvaluationCaseResultStatus.RUNNING,
            )
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
            await self.recover_inflight_cases(session, run_id=run_id)

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
                ):
                    return
                if run.status == EvaluationExperimentRunStatus.CANCEL_REQUESTED:
                    await self._finish_run(session, run_id=run_id, owner=owner)
                    return
                case = await self.claim_case(session, run_id=run_id, owner=owner)
                if case is None:
                    await self._finish_run(session, run_id=run_id, owner=owner)
                    return
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
                        failure_code="EVALUATION_PLAN_INVALID",
                        safe_message="The persisted evaluation plan is invalid.",
                    )
                continue
            try:
                async with self.session_factory() as session:
                    result = await self.driver.execute(session, run=run, variant=variant, item=item)
                async with self.session_factory() as session:
                    await self.complete_case(session, case_id=case.id, observation=result)
            except Exception:
                logger.warning(
                    "evaluation_case_failed", extra={"case_id": str(case.id)}, exc_info=True
                )
                async with self.session_factory() as session:
                    await self.fail_case(
                        session,
                        case_id=case.id,
                        failure_code="EVALUATION_CASE_FAILED",
                        safe_message="The evaluation case failed.",
                    )
            async with self.session_factory() as session:
                await self.heartbeat(
                    session,
                    run_id=run_id,
                    owner=owner,
                    lease_seconds=settings.evaluation_runner_lease_seconds,
                )

    async def cancel(self, session: AsyncSession, *, run_id: UUID) -> bool:
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
            .values(status=EvaluationExperimentRunStatus.CANCEL_REQUESTED)
        )
        await session.commit()
        return result.rowcount == 1

    async def _finish_run(self, session: AsyncSession, *, run_id: UUID, owner: str) -> None:
        run = await session.scalar(
            select(EvaluationExperimentRun)
            .where(
                EvaluationExperimentRun.id == run_id,
                EvaluationExperimentRun.lease_owner == owner,
            )
            .with_for_update()
        )
        if run is None:
            return
        counts = await self.progress(session, run_id=run_id)
        if counts.running:
            await session.rollback()
            return
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
                            EvaluationExperimentRun.status
                            == EvaluationExperimentRunStatus.QUEUED,
                            EvaluationExperimentRun.created_at <= grace_cutoff,
                        ),
                        and_(
                            EvaluationExperimentRun.status == EvaluationExperimentRunStatus.RUNNING,
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
                        EvaluationExperimentCaseResult.status
                        == EvaluationCaseResultStatus.RUNNING,
                    )
                )
                if running_cases:
                    continue
                await session.execute(
                    update(EvaluationExperimentCaseResult)
                    .where(
                        EvaluationExperimentCaseResult.workspace_id == run.workspace_id,
                        EvaluationExperimentCaseResult.experiment_run_id == run.id,
                        EvaluationExperimentCaseResult.status
                        == EvaluationCaseResultStatus.PENDING,
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
    return tuple(requeued)


__all__ = [
    "AgentRuntimeEvaluationDriver",
    "CaseExecutionDriver",
    "CaseExecutionObservation",
    "DeterministicEvaluationDriver",
    "ExperimentRunner",
    "RunProgress",
    "reconcile_experiment_runs",
]
