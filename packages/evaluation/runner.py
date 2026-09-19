"""Durable M7-C experiment execution primitives.

The database is the source of truth.  Celery only transports an experiment run id;
workers rebuild the frozen execution plan from the persisted M7-B records.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy import case as sql_case
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from packages.agent_runtime.frozen import parse_frozen_agent_spec
from packages.agent_runtime.models import AgentRun
from packages.agent_runtime.runtime import (
    AgentRunExecutionOverrides,
    AgentRunService,
)
from packages.approvals.contracts import ApprovalDecisionStatus
from packages.approvals.models import Approval
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.config.settings import Settings
from packages.core.errors.exceptions import AgentHubError
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
from packages.evaluation.reproducibility import (
    experiment_spec_hash,
    normalize_knowledge_snapshots,
    pricing_snapshot_content_hash,
    variant_hash,
)
from packages.knowledge.contracts import KnowledgeRetriever, RetrievalQuery
from packages.knowledge.models import KnowledgeSnapshot
from packages.observability import NoopTraceSink
from packages.observability.contracts import TraceSink, TraceSpan

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


@dataclass(frozen=True)
class PreparedCaseExecution:
    agent_run_id: UUID | None
    execute: Callable[[], Awaitable[CaseExecutionObservation]]


class CaseExecutionDriver(Protocol):
    async def execute(
        self,
        session: AsyncSession | None = None,
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
        session: AsyncSession | None = None,
        *,
        run: EvaluationExperimentRun,
        variant: EvaluationExperimentVariant,
        item: EvaluationDatasetItem,
    ) -> CaseExecutionObservation:
        del run, session
        started = time.perf_counter()
        category = str(item.category)
        observation: dict[str, Any] = {
            "category": category,
            "input_hash": canonical_json_hash(item.input),
            "variant_hash": variant.variant_hash,
        }
        if category == "RETRIEVAL":
            relevant = list(item.expected.get("relevant_chunk_ids", []))
            observation["candidate_chunk_ids"] = [*relevant, f"noise-{item.case_key}"]
            observation["final_chunk_ids"] = relevant
            observation["citation_ids"] = relevant
        elif category == "KNOWLEDGE_QA":
            observation["citation_ids"] = list(item.expected.get("citations", []))
            observation["answer_hash"] = canonical_json_hash(item.expected.get("answer", ""))
        elif category == "TOOL":
            observation["tool_identity"] = item.expected.get("tool_identity")
            observation["arguments_hash"] = canonical_json_hash(item.expected.get("arguments", {}))
            observation["tool_sequence"] = list(
                item.expected.get("tool_sequence", [item.expected.get("tool_identity")])
            )
        elif category == "NO_ANSWER":
            observation["answerable"] = False
        elif category == "APPROVAL":
            observation["approval_required"] = item.expected.get("approval_required", True)
            observation["approval_decision"] = item.expected.get("decision")
            observation["action_executed"] = False
            observation["unauthorized_execution"] = False
            observation["duplicate_side_effect"] = False
            observation["unknown_outcome_semantics_ok"] = True
        elif category == "MULTI_STEP":
            observation["steps"] = list(item.expected.get("steps", []))
            if "terminal_status" in item.expected:
                observation["terminal_status"] = item.expected["terminal_status"]
        elif category == "FAILURE":
            expected_status = item.expected.get("status")
            expected_failure_code = item.expected.get("failure_code")
            observation["expected_status"] = expected_status
            observation["expected_failure_code"] = expected_failure_code
            observation["observed_agent_status"] = expected_status
            observation["observed_agent_failure_code"] = expected_failure_code
        return CaseExecutionObservation(
            observation=observation,
            latency_ms=max(0, round((time.perf_counter() - started) * 1000)),
        )

    async def prepare(
        self,
        *,
        run: EvaluationExperimentRun,
        variant: EvaluationExperimentVariant,
        item: EvaluationDatasetItem,
    ) -> PreparedCaseExecution:
        return PreparedCaseExecution(
            agent_run_id=None,
            execute=lambda: self.execute(run=run, variant=variant, item=item),
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
        approval_context_factory: Callable[[EvaluationExperimentRun], Awaitable[Any]] | None = None,
    ) -> None:
        self.agent_run_service = agent_run_service
        self.context_factory = context_factory
        self.retriever = retriever
        self.approval_context_factory = approval_context_factory

    async def prepare(
        self,
        *,
        run: EvaluationExperimentRun,
        variant: EvaluationExperimentVariant,
        item: EvaluationDatasetItem,
    ) -> PreparedCaseExecution:
        context = await self.context_factory(run)
        started = time.perf_counter()
        if item.category == "RETRIEVAL":
            if self.retriever is None or not variant.effective_knowledge_snapshots:
                raise RuntimeError("EVALUATION_RETRIEVAL_NOT_CONFIGURED")
            binding = variant.effective_knowledge_snapshots[0]

            async def execute_retrieval() -> CaseExecutionObservation:
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

            return PreparedCaseExecution(agent_run_id=None, execute=execute_retrieval)

        if item.category == "APPROVAL":
            expected_decision = item.expected.get("decision")
            if str(expected_decision).upper() not in {"APPROVED", "DENIED"}:
                raise RuntimeError("EVALUATION_CASE_INVALID")
        prepared_run = await self.agent_run_service.prepare_run(
            context,
            agent_version_id=variant.agent_version_id,
            input_text=_case_input_text(item),
            execution_overrides=AgentRunExecutionOverrides.for_evaluation(
                list(variant.effective_knowledge_snapshots)
            ),
        )

        async def execute_agent() -> CaseExecutionObservation:
            result = await self.agent_run_service.execute_prepared_run(
                context, run_id=prepared_run.id
            )
            if result.status == "WAITING_APPROVAL":
                async with self.agent_run_service.session_factory() as session:
                    approval = await session.scalar(
                        select(Approval).where(
                            Approval.workspace_id == run.workspace_id,
                            Approval.run_id == result.run_id,
                        )
                    )
                if approval is None:
                    raise RuntimeError("EVALUATION_APPROVAL_NOT_FOUND")
                expected_decision = str(item.expected["decision"]).upper()
                decision = (
                    ApprovalDecisionStatus.APPROVED
                    if expected_decision == "APPROVED"
                    else ApprovalDecisionStatus.DENIED
                )
                if self.agent_run_service.approval_service is None:
                    raise RuntimeError("EVALUATION_APPROVAL_SERVICE_NOT_CONFIGURED")
                approval_context = (
                    await self.approval_context_factory(run)
                    if self.approval_context_factory is not None
                    else context
                )
                await self.agent_run_service.approval_service.decide(
                    approval_context, approval.id, decision=decision
                )
                result = await self.agent_run_service.resume(
                    context, run_id=result.run_id, approval_id=approval.id
                )
                if result.status == "WAITING_APPROVAL":
                    raise RuntimeError("EVALUATION_MULTIPLE_APPROVALS_NOT_SUPPORTED")
            async with self.agent_run_service.session_factory() as session:
                persisted = await session.scalar(
                    select(AgentRun).where(
                        AgentRun.workspace_id == run.workspace_id,
                        AgentRun.id == result.run_id,
                    )
                )
            return CaseExecutionObservation(
                observation={
                    "category": item.category,
                    "agent_run_id": str(result.run_id),
                    "status": result.status,
                    "observed_agent_status": result.status,
                    "observed_agent_failure_code": result.failure_code,
                    "model_step_count": result.model_step_count,
                    "tool_call_count": result.tool_call_count,
                    "output_hash": canonical_json_hash(result.final_output or ""),
                    "variant_hash": variant.variant_hash,
                },
                agent_run_id=result.run_id,
                latency_ms=max(0, round((time.perf_counter() - started) * 1000)),
                input_tokens=(
                    persisted.total_input_tokens if persisted else result.total_input_tokens
                ),
                output_tokens=(
                    persisted.total_output_tokens if persisted else result.total_output_tokens
                ),
                total_tokens=persisted.total_tokens if persisted else result.total_tokens,
                cached_tokens=(
                    persisted.total_cached_tokens if persisted else result.total_cached_tokens
                ),
                cost_amount=persisted.total_cost_amount if persisted else result.total_cost_amount,
                cost_currency=persisted.cost_currency if persisted else result.cost_currency,
            )

        return PreparedCaseExecution(agent_run_id=prepared_run.id, execute=execute_agent)

    async def execute(
        self,
        *,
        run: EvaluationExperimentRun,
        variant: EvaluationExperimentVariant,
        item: EvaluationDatasetItem,
    ) -> CaseExecutionObservation:
        prepared = await self.prepare(run=run, variant=variant, item=item)
        return await prepared.execute()


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
    ) -> bool:
        result = await session.execute(
            update(EvaluationExperimentCaseResult)
            .where(
                EvaluationExperimentCaseResult.id == case_id,
                EvaluationExperimentCaseResult.experiment_run_id == run_id,
                EvaluationExperimentCaseResult.status == EvaluationCaseResultStatus.RUNNING,
                EvaluationExperimentCaseResult.lease_owner == owner,
                EvaluationExperimentCaseResult.lease_generation == generation,
                EvaluationExperimentCaseResult.agent_run_id.is_(None),
            )
            .values(agent_run_id=agent_run_id)
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
            .with_for_update(skip_locked=True)
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
        cost_amount = None
        cost_currency = None
        if (
            observation.input_tokens is not None
            and observation.output_tokens is not None
            and observation.cached_tokens is not None
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
                cached_tokens = observation.cached_tokens
                assert cached_tokens is not None
                normal_input_tokens = max(0, observation.input_tokens - cached_tokens)
                input_cost = Decimal(normal_input_tokens) / Decimal(1_000_000)
                cached_cost = Decimal(cached_tokens) / Decimal(1_000_000)
                output_cost = Decimal(observation.output_tokens) / Decimal(1_000_000)
                cached_price = pricing.cached_input_price_per_1m
                effective_cached_price = (
                    Decimal(cached_price)
                    if cached_price is not None
                    else Decimal(pricing.input_price_per_1m)
                )
                cost_amount = (
                    input_cost * Decimal(pricing.input_price_per_1m)
                    + cached_cost * effective_cached_price
                    + output_cost * Decimal(pricing.output_price_per_1m)
                ).quantize(Decimal("0.00000001"))
                cost_currency = pricing.currency
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
            async with self.session_factory() as session:
                await self._fail_run(
                    session,
                    run_id=run_id,
                    owner=owner,
                    generation=generation,
                    failure_code=error.code,
                    safe_message="The frozen evaluation plan failed reproducibility validation.",
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
            )
        )
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
                    ):
                        return
                    if run.status == EvaluationExperimentRunStatus.CANCEL_REQUESTED:
                        await self._finish_run(
                            session, run_id=run_id, owner=owner, generation=generation
                        )
                        return
                    case = await self.claim_case(
                        session, run_id=run_id, owner=owner, generation=generation
                    )
                    if case is None:
                        await self._finish_run(
                            session, run_id=run_id, owner=owner, generation=generation
                        )
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
                            run_id=run_id,
                            owner=owner,
                            generation=generation,
                            failure_code="EVALUATION_PLAN_INVALID",
                            safe_message="The persisted evaluation plan is invalid.",
                        )
                    continue
                try:
                    prepared = await self._prepare_driver(run=run, variant=variant, item=item)
                    if prepared.agent_run_id is not None:
                        async with self.session_factory() as session:
                            linked = await self.bind_case_agent_run(
                                session,
                                case_id=case.id,
                                run_id=run_id,
                                agent_run_id=prepared.agent_run_id,
                                owner=owner,
                                generation=generation,
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
                    except Exception:
                        await _safe_end_span(
                            case_span,
                            {"status": "FAILED", "failure_code": "EVALUATION_CASE_FAILED"},
                            status="error",
                            failure_code="EVALUATION_CASE_FAILED",
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
                        "evaluation_case_failed", extra={"case_id": str(case.id)}, exc_info=True
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
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            await _safe_end_span(
                run_span,
                {"status": "completed"},
                status="ok",
                failure_code=None,
            )

    async def _prepare_driver(
        self,
        *,
        run: EvaluationExperimentRun,
        variant: EvaluationExperimentVariant,
        item: EvaluationDatasetItem,
    ) -> PreparedCaseExecution:
        prepare = getattr(self.driver, "prepare", None)
        if prepare is not None:
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
        self, *, run_id: UUID, owner: str, generation: int, settings: Settings
    ) -> None:
        while True:
            await asyncio.sleep(settings.evaluation_runner_heartbeat_seconds)
            async with self.session_factory() as session:
                if not await self.heartbeat(
                    session,
                    run_id=run_id,
                    owner=owner,
                    generation=generation,
                    lease_seconds=settings.evaluation_runner_lease_seconds,
                ):
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
        for variant in variants:
            await self._validate_variant(session, run.workspace_id, variant)

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
        model = parsed.model_plan.primary
        if model.provider != pricing.provider or model.model != pricing.model:
            raise AgentHubError(
                "EXPERIMENT_REPRODUCIBILITY_VIOLATION",
                "The frozen pricing model no longer matches the AgentVersion.",
                409,
            )
        if pricing_snapshot_content_hash(pricing) != variant.pricing_snapshot_hash:
            raise AgentHubError(
                "EXPERIMENT_REPRODUCIBILITY_VIOLATION",
                "The frozen pricing snapshot changed.",
                409,
            )
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
        await session.execute(
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
    ) -> None:
        predicates = [
            EvaluationExperimentRun.id == run_id,
            EvaluationExperimentRun.lease_owner == owner,
        ]
        if generation is not None:
            predicates.append(EvaluationExperimentRun.lease_generation == generation)
        run = await session.scalar(
            select(EvaluationExperimentRun).where(*predicates).with_for_update()
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
