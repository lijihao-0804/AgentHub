"""Evaluation case execution drivers extracted from runner.py."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.agent_runtime.frozen import parse_frozen_agent_spec
from packages.agent_runtime.models import AgentRun, AgentVersion, RunStep
from packages.agent_runtime.runtime import (
    AgentRunExecutionOverrides,
    AgentRunService,
)
from packages.approvals.contracts import ApprovalDecisionStatus
from packages.approvals.models import Approval
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.errors.exceptions import AgentHubError
from packages.evaluation.business import inspect_support_outcome, support_condition_from_expected
from packages.evaluation.judge import (
    JUDGE_OBSERVATION_KEY,
    AnswerQualityJudge,
    JudgeEnsemble,
    JudgeRequest,
    sequence_of_text,
)
from packages.evaluation.models import (
    EvaluationDatasetItem,
    EvaluationExperimentRun,
    EvaluationExperimentVariant,
)
from packages.evaluation.observations import observation_identity, retrieval_observation
from packages.evaluation.pricing import (
    FROZEN_PRICING_CHAIN_KEY,
    ModelCallAttempt,
)
from packages.evaluation.taxonomy import classify_failure
from packages.evaluation.trajectory import build_trajectory_observation
from packages.knowledge.contracts import KnowledgeRetriever, RetrievalQuery
from packages.observability.timing import capture_timings

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
    model_attempts: tuple[dict[str, Any], ...] = ()
    model_attempts_complete: bool = False
    aggregate_billing_complete: bool = True
    failure_code: str | None = None
    safe_failure_message: str | None = None


@dataclass(frozen=True)
class PreparedCaseExecution:
    agent_run_id: UUID | None
    execute: Callable[[], Awaitable[CaseExecutionObservation]]
    preparation_observation: dict[str, Any] | None = None


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
            **observation_identity("synthetic", {"all": "expected_fixture"}),
            "category": category,
            "input_hash": canonical_json_hash(item.input),
            "variant_hash": variant.variant_hash,
        }
        if category == "RETRIEVAL":
            relevant = list(item.expected.get("relevant_chunk_ids", []))
            observation["candidate_chunk_ids"] = [*relevant, f"noise-{item.case_key}"]
            observation["final_chunk_ids"] = relevant
            observation["candidate_top_k"] = 20
            observation["final_top_k"] = 5
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
        judge: AnswerQualityJudge | JudgeEnsemble | None = None,
    ) -> None:
        self.agent_run_service = agent_run_service
        self.context_factory = context_factory
        self.retriever = retriever
        self.approval_context_factory = approval_context_factory
        self.judge = judge

    def judge_request(self, item: EvaluationDatasetItem, final_output: Any) -> JudgeRequest | None:
        """Build the judge input for an open-ended dataset item, or None to skip it.

        Only items that opt in with ``expected["open_ended"] is True`` are judged: the
        deterministic evaluators remain the only scorer for every other item.
        """
        if self.judge is None or item.expected.get("open_ended") is not True:
            return None
        if not isinstance(final_output, str) or not final_output.strip():
            return None
        reference = item.expected.get("answer")
        return JudgeRequest(
            question=_case_input_text(item),
            answer=final_output,
            reference_answer=reference if isinstance(reference, str) else None,
            evidence=sequence_of_text(item.expected.get("evidence")),
        )

    async def judge_answer(
        self, item: EvaluationDatasetItem, final_output: Any
    ) -> dict[str, Any] | None:
        request = self.judge_request(item, final_output)
        if request is None or self.judge is None:
            return None
        return await self.judge.judge(request)

    async def prepare(
        self,
        *,
        run: EvaluationExperimentRun,
        variant: EvaluationExperimentVariant,
        item: EvaluationDatasetItem,
    ) -> PreparedCaseExecution:
        context = await self.context_factory(run)
        if str(context.workspace_id) != str(run.workspace_id):
            raise AgentHubError(
                "FORBIDDEN", "The evaluation context belongs to another workspace.", 403
            )
        if item.category == "MULTI_STEP" and "scenario" in item.input:
            raise AgentHubError(
                "EVALUATION_SCENARIO_DRIVER_NOT_CONFIGURED",
                "A scenario execution driver is required for this dataset.",
                422,
            )
        postcondition = support_condition_from_expected(
            item.expected, workspace_id=context.workspace_id
        )
        started = time.perf_counter()
        if item.category == "RETRIEVAL":
            if self.retriever is None or not variant.effective_knowledge_snapshots:
                raise RuntimeError("EVALUATION_RETRIEVAL_NOT_CONFIGURED")
            binding = variant.effective_knowledge_snapshots[0]

            async def execute_retrieval() -> CaseExecutionObservation:
                query = RetrievalQuery(
                    text=str(item.input["query"]),
                    knowledge_base_id=str(binding["knowledge_base_id"]),
                    knowledge_snapshot_id=str(binding["snapshot_id"]),
                )
                result = await self.retriever.retrieve_with_trace(context, query)
                return CaseExecutionObservation(
                    observation=retrieval_observation(
                        result, query, variant_hash=variant.variant_hash
                    ),
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
                list(variant.effective_knowledge_snapshots),
                wiki_publications=list(variant.effective_wiki_publications or []),
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
                step_rows = list(
                    await session.scalars(
                        select(RunStep)
                        .where(
                            RunStep.workspace_id == run.workspace_id,
                            RunStep.agent_run_id == result.run_id,
                            RunStep.kind.in_(("MODEL", "AUXILIARY_MODEL")),
                        )
                        .order_by(RunStep.sequence_number, RunStep.id)
                    )
                )
                trajectory_rows = list(
                    await session.scalars(
                        select(RunStep)
                        .where(
                            RunStep.workspace_id == run.workspace_id,
                            RunStep.agent_run_id == result.run_id,
                            RunStep.kind.in_(
                                (
                                    "TOOL_PROPOSAL",
                                    "TOOL_EXECUTE",
                                    "POLICY",
                                    "APPROVAL_WAIT",
                                    "ACTION",
                                    "RECONCILIATION",
                                    "GUARD",
                                    "FINISH",
                                )
                            ),
                        )
                        .order_by(RunStep.sequence_number, RunStep.id)
                    )
                )
                approval_rows = list(
                    await session.scalars(
                        select(Approval)
                        .where(
                            Approval.workspace_id == run.workspace_id,
                            Approval.run_id == result.run_id,
                        )
                        .order_by(Approval.created_at, Approval.id)
                    )
                )
                version = await session.scalar(
                    select(AgentVersion).where(
                        AgentVersion.workspace_id == run.workspace_id,
                        AgentVersion.id == variant.agent_version_id,
                    )
                )
            frozen_fallback_chain = FROZEN_PRICING_CHAIN_KEY in variant.variant_metadata
            try:
                frozen_model_plan = parse_frozen_agent_spec(
                    version.resolved_spec if version is not None else None,
                    workspace_id=run.workspace_id,
                ).model_plan
            except (AgentHubError, TypeError):
                frozen_model_plan = None
            model_attempts, _ = _model_attempts_from_steps(
                step_rows,
                require_complete_receipt=frozen_fallback_chain,
            )
            model_attempts_complete = _pricing_receipt_complete(
                step_rows,
                frozen_fallback_chain=frozen_fallback_chain,
                max_attempts=(
                    frozen_model_plan.retry_policy.max_attempts
                    if frozen_model_plan is not None
                    else 0
                ),
            )
            observation: dict[str, Any] = {
                **observation_identity("runtime", {"status": "agent_run", "usage": "agent_run"}),
                "category": item.category,
                "agent_run_id": str(result.run_id),
                "status": result.status,
                "observed_agent_status": result.status,
                "observed_agent_failure_code": result.failure_code,
                "model_step_count": result.model_step_count,
                "tool_call_count": result.tool_call_count,
                "output_hash": (
                    canonical_json_hash(result.final_output)
                    if result.final_output is not None
                    else None
                ),
                "variant_hash": variant.variant_hash,
            }
            trajectory_steps = [
                {
                    "index": index,
                    "sequence_number": row.sequence_number,
                    "kind": row.kind,
                    "status": row.status,
                    "error_code": (row.safe_metadata or {}).get("error_code"),
                    "tool_identities": (row.safe_metadata or {}).get("tool_identities") or [],
                    "arguments_hashes": (row.safe_metadata or {}).get("arguments_hashes") or [],
                    "tool_results": (row.safe_metadata or {}).get("tool_results") or [],
                    "logical_action_ids": (row.safe_metadata or {}).get("logical_action_ids") or [],
                    "action_results": (row.safe_metadata or {}).get("action_results") or [],
                }
                for index, row in enumerate(trajectory_rows)
            ]
            approval_facts = [
                {
                    "approval_id": str(row.id),
                    "logical_action_id": row.logical_action_id,
                    "tool_identity": row.tool_identity,
                    "arguments_hash": row.canonical_args_hash,
                    "decision_status": row.decision_status,
                    "execution_status": row.execution_status,
                    "failure_code": row.failure_code,
                }
                for row in approval_rows
            ]
            cost_amount = persisted.total_cost_amount if persisted else result.total_cost_amount
            observation.update(
                build_trajectory_observation(
                    steps=trajectory_steps,
                    approvals=approval_facts,
                    terminal={
                        "status": result.status,
                        "failure_code": result.failure_code,
                    },
                    counters={
                        "model_step_count": result.model_step_count,
                        "tool_call_count": result.tool_call_count,
                    },
                    budget={
                        "input_tokens": (
                            persisted.total_input_tokens if persisted else result.total_input_tokens
                        ),
                        "output_tokens": (
                            persisted.total_output_tokens
                            if persisted
                            else result.total_output_tokens
                        ),
                        "total_tokens": (
                            persisted.total_tokens if persisted else result.total_tokens
                        ),
                        "cached_tokens": (
                            persisted.total_cached_tokens
                            if persisted
                            else result.total_cached_tokens
                        ),
                        "cost_amount": str(cost_amount) if cost_amount is not None else None,
                        "cost_currency": (
                            persisted.cost_currency if persisted else result.cost_currency
                        ),
                    },
                    output_hash=observation["output_hash"],
                    category=item.category,
                    expected=item.expected,
                    field_sources={
                        **observation.get("field_sources", {}),
                        "trajectory": "run_steps+approvals",
                    },
                )
            )
            observation["failure_taxonomy"] = classify_failure(item.expected, observation)
            # Execution safety booleans are derived from the unified trajectory evidence in
            # build_trajectory_observation; do not re-derive them from approval rows alone.
            if postcondition is not None:
                async with self.agent_run_service.session_factory() as session:
                    observation["business_outcome"] = await inspect_support_outcome(
                        session, context, postcondition, run_id=prepared_run.id
                    )
                observation["field_sources"]["business_outcome"] = "workspace_scoped_ticket_query"
            # Supplementary only: the verdict carries scores, identities and hashes, never
            # the judged text, so the observation stays the safe projection it already was.
            judgement = await self.judge_answer(item, result.final_output)
            if judgement is not None:
                observation[JUDGE_OBSERVATION_KEY] = judgement
            return CaseExecutionObservation(
                observation=observation,
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
                model_attempts=model_attempts,
                model_attempts_complete=model_attempts_complete,
                aggregate_billing_complete=model_attempts_complete,
            )

        return PreparedCaseExecution(
            agent_run_id=prepared_run.id, execute=lambda: _timed_execution(execute_agent)
        )

    async def execute(
        self,
        *,
        run: EvaluationExperimentRun,
        variant: EvaluationExperimentVariant,
        item: EvaluationDatasetItem,
    ) -> CaseExecutionObservation:
        prepared = await self.prepare(run=run, variant=variant, item=item)
        return await prepared.execute()


async def _timed_execution(
    execute: Callable[[], Awaitable[CaseExecutionObservation]],
) -> CaseExecutionObservation:
    from dataclasses import replace

    with capture_timings() as timing:
        result = await execute()
    projection = timing.projection()
    projection["billing"]["collection_complete"] = timing.model_calls == result.observation.get(
        "model_step_count"
    )
    return replace(
        result,
        observation={
            **result.observation,
            "timing": projection,
            "field_sources": {
                **result.observation.get("field_sources", {}),
                "timing": "request_local_trace_span",
            },
        },
    )


def _case_input_text(item: EvaluationDatasetItem) -> str:
    for field in ("query", "question", "request", "action", "task", "scenario"):
        value = item.input.get(field)
        if isinstance(value, str):
            return value
    raise RuntimeError("EVALUATION_CASE_INPUT_INVALID")


def _model_attempts_from_steps(
    steps: list[RunStep],
    *,
    require_complete_receipt: bool,
) -> tuple[tuple[dict[str, Any], ...], bool]:
    """Project persisted model attempts without trusting aggregate billing columns.

    Older runs have MODEL rows with no actual provider/model metadata. Their
    aggregate tokens remain usable only for a primary-only variant.
    """

    attempts: list[dict[str, Any]] = []
    # Current persisted RunSteps do not carry a provider receipt proving that
    # pre-success retries/failures were captured. A frozen fallback chain must
    # therefore remain NOT_AVAILABLE until such receipts are bridged by core.
    complete = bool(steps) and not require_complete_receipt
    for step in steps:
        metadata = step.safe_metadata if isinstance(step.safe_metadata, dict) else {}
        provider = metadata.get("provider")
        model = metadata.get("model")
        source = metadata.get("usage_source")
        if source is None and step.kind == "MODEL":
            source = "generation"
        attempt_status = metadata.get("attempt_status") or step.status
        cost_status = metadata.get("cost_status")
        attempts.append(
            {
                "source": source,
                "provider": provider,
                "model": model,
                "input_tokens": metadata.get("input_tokens"),
                "output_tokens": metadata.get("output_tokens"),
                "total_tokens": metadata.get("total_tokens"),
                "cached_tokens": metadata.get("cached_tokens"),
                "attempt_status": attempt_status,
                "cost_status": cost_status,
            }
        )
        if (
            not isinstance(source, str)
            or not source
            or not isinstance(provider, str)
            or not provider
            or not isinstance(model, str)
            or not model
            or not isinstance(attempt_status, str)
            or not attempt_status
        ):
            complete = False
    return tuple(attempts), complete


def _pricing_receipt_complete(
    steps: list[RunStep],
    *,
    frozen_fallback_chain: bool,
    max_attempts: int = 1,
) -> bool:
    """Return true only when the persisted attempt set is provably exhaustive.

    Current RunSteps expose successful generation calls and selector attempts,
    but failed provider calls that precede a fallback have no durable receipt.
    Fallback variants therefore stay NOT_AVAILABLE. A primary-only run may use
    its aggregate billing columns only when it has no auxiliary model activity.
    """

    if max_attempts != 1:
        return False
    if not steps or any(step.kind != "MODEL" for step in steps):
        return False
    if frozen_fallback_chain:
        return False
    _, complete = _model_attempts_from_steps(
        steps,
        require_complete_receipt=False,
    )
    return complete


def _model_attempt(attempt: dict[str, Any]) -> ModelCallAttempt:
    return ModelCallAttempt(
        source=attempt.get("source"),
        provider=attempt.get("provider"),
        model=attempt.get("model"),
        input_tokens=attempt.get("input_tokens"),
        output_tokens=attempt.get("output_tokens"),
        total_tokens=attempt.get("total_tokens"),
        cached_tokens=attempt.get("cached_tokens"),
        attempt_status=attempt.get("attempt_status"),
        cost_status=attempt.get("cost_status"),
    )


def _safe_model_attempt_projection(attempt: dict[str, Any]) -> dict[str, Any]:
    return {
        key: attempt.get(key)
        for key in (
            "source",
            "provider",
            "model",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cached_tokens",
            "attempt_status",
            "cost_status",
        )
    }


