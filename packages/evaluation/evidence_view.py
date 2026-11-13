"""Read-only, privacy-preserving evidence for one persisted evaluation case."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.agent_runtime.models import AgentRun, RunStep
from packages.approvals.models import Approval
from packages.control_plane.rbac import EVALUATION_READ
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.evaluation.metrics import EvaluatorRegistry, MetricStatus, MetricValue
from packages.evaluation.models import (
    EvaluationDatasetItem,
    EvaluationExperiment,
    EvaluationExperimentCaseResult,
    EvaluationExperimentRun,
    EvaluationExperimentVariant,
    InteractiveEpisode,
    InteractiveEpisodeTurn,
)
from packages.evaluation.taxonomy import classify_failure

EVIDENCE_VIEW_SCHEMA_VERSION = 1
_UNSET = object()


class EvaluationEvidenceViewService:
    """Load authorized evidence rows and project only the public safe contract."""

    def __init__(self, registry: EvaluatorRegistry | None = None) -> None:
        self.registry = registry or EvaluatorRegistry()

    async def summarize_case_results(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
        run_id: UUID,
        cases: Sequence[EvaluationExperimentCaseResult],
    ) -> dict[UUID, dict[str, Any]]:
        """Batch safe list summaries for one already-authorized run page."""

        self._require_read(context)
        if not cases:
            return {}
        workspace_id = self._workspace_id(context)
        run_experiment = (
            await session.execute(
                select(EvaluationExperimentRun, EvaluationExperiment)
                .join(
                    EvaluationExperiment,
                    (EvaluationExperiment.workspace_id == EvaluationExperimentRun.workspace_id)
                    & (EvaluationExperiment.id == EvaluationExperimentRun.experiment_id),
                )
                .where(
                    EvaluationExperimentRun.workspace_id == workspace_id,
                    EvaluationExperimentRun.id == run_id,
                )
            )
        ).first()
        if run_experiment is None:
            return {}
        _, experiment = run_experiment

        run_cases = [
            case
            for case in cases
            if case.workspace_id == workspace_id and case.experiment_run_id == run_id
        ]
        if not run_cases:
            return {}

        item_ids = {case.dataset_item_id for case in run_cases}
        variant_ids = {case.experiment_variant_id for case in run_cases}
        items = {
            item.id: item
            for item in await session.scalars(
                select(EvaluationDatasetItem).where(
                    EvaluationDatasetItem.workspace_id == workspace_id,
                    EvaluationDatasetItem.dataset_version_id == experiment.dataset_version_id,
                    EvaluationDatasetItem.id.in_(item_ids),
                )
            )
        }
        variants = {
            variant.id: variant
            for variant in await session.scalars(
                select(EvaluationExperimentVariant).where(
                    EvaluationExperimentVariant.workspace_id == workspace_id,
                    EvaluationExperimentVariant.experiment_id == experiment.id,
                    EvaluationExperimentVariant.id.in_(variant_ids),
                )
            )
        }

        manifest = experiment.evaluator_manifest
        frozen_registry: EvaluatorRegistry | None = None
        manifest_error: str | None = None
        if not isinstance(manifest, Mapping):
            manifest_error = "evaluator_manifest_missing"
        else:
            try:
                frozen_registry = self.registry.for_manifest(manifest)
            except (AttributeError, KeyError, TypeError, ValueError):
                manifest_error = "evaluator_input_invalid"

        summaries: dict[UUID, dict[str, Any]] = {}
        for case in run_cases:
            item = items.get(case.dataset_item_id)
            variant = variants.get(case.experiment_variant_id)
            summary: dict[str, Any] = {
                "case_key": _string_or_none(item.case_key) if item is not None else None,
                "category": _string_or_none(item.category) if item is not None else None,
                "variant_label": (_string_or_none(variant.label) if variant is not None else None),
                "task_success": None,
            }
            if item is not None and frozen_registry is not None:
                expected = item.expected if isinstance(item.expected, Mapping) else {}
                task_success, _ = _evaluate_task_success(
                    category=_string_or_none(item.category),
                    expected=expected,
                    observation=(case.observation if isinstance(case.observation, Mapping) else {}),
                    registry=frozen_registry,
                )
                summary["task_success"] = task_success
            elif item is not None and manifest_error is not None:
                summary["task_success"] = _task_success_unavailable(manifest_error)
            summaries[case.id] = summary
        return summaries

    async def get_case_evidence(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
        run_id: UUID,
        case_id: UUID,
    ) -> dict[str, Any]:
        self._require_read(context)
        workspace_id = self._workspace_id(context)

        run = await session.scalar(
            select(EvaluationExperimentRun).where(
                EvaluationExperimentRun.workspace_id == workspace_id,
                EvaluationExperimentRun.id == run_id,
            )
        )
        if run is None:
            raise AgentHubError(
                "EVALUATION_EXPERIMENT_RUN_NOT_FOUND",
                "The experiment run was not found.",
                404,
            )

        case = await session.scalar(
            select(EvaluationExperimentCaseResult).where(
                EvaluationExperimentCaseResult.workspace_id == workspace_id,
                EvaluationExperimentCaseResult.experiment_run_id == run.id,
                EvaluationExperimentCaseResult.id == case_id,
            )
        )
        if case is None:
            raise AgentHubError(
                "EVALUATION_CASE_RESULT_NOT_FOUND",
                "The evaluation case result was not found.",
                404,
            )

        item = await session.scalar(
            select(EvaluationDatasetItem).where(
                EvaluationDatasetItem.workspace_id == workspace_id,
                EvaluationDatasetItem.id == case.dataset_item_id,
            )
        )
        variant = await session.scalar(
            select(EvaluationExperimentVariant).where(
                EvaluationExperimentVariant.workspace_id == workspace_id,
                EvaluationExperimentVariant.id == case.experiment_variant_id,
            )
        )
        experiment = await session.scalar(
            select(EvaluationExperiment).where(
                EvaluationExperiment.workspace_id == workspace_id,
                EvaluationExperiment.id == run.experiment_id,
            )
        )
        episode = await session.scalar(
            select(InteractiveEpisode).where(
                InteractiveEpisode.workspace_id == workspace_id,
                InteractiveEpisode.case_id == case.id,
            )
        )
        turns = list(
            await session.scalars(
                select(InteractiveEpisodeTurn)
                .where(
                    InteractiveEpisodeTurn.workspace_id == workspace_id,
                    InteractiveEpisodeTurn.case_id == case.id,
                )
                .order_by(
                    InteractiveEpisodeTurn.turn_index,
                    InteractiveEpisodeTurn.id,
                )
            )
        )

        agent_run_ids = self._ordered_agent_run_ids(case, turns)
        agent_statuses: dict[UUID, str] = {}
        steps: list[RunStep] = []
        approvals: list[Approval] = []
        if agent_run_ids:
            status_rows = await session.execute(
                select(AgentRun.id, AgentRun.status).where(
                    AgentRun.workspace_id == workspace_id,
                    AgentRun.id.in_(agent_run_ids),
                )
            )
            agent_statuses = {run_id_value: status for run_id_value, status in status_rows}
            steps = list(
                await session.scalars(
                    select(RunStep).where(
                        RunStep.workspace_id == workspace_id,
                        RunStep.agent_run_id.in_(agent_run_ids),
                    )
                )
            )
            approvals = list(
                await session.scalars(
                    select(Approval)
                    .where(
                        Approval.workspace_id == workspace_id,
                        Approval.run_id.in_(agent_run_ids),
                    )
                    .order_by(Approval.run_id, Approval.created_at, Approval.id)
                )
            )
            run_order = {agent_run_id: index for index, agent_run_id in enumerate(agent_run_ids)}
            steps.sort(
                key=lambda step: (
                    run_order.get(step.agent_run_id, len(run_order)),
                    step.sequence_number,
                    str(step.id),
                )
            )

        return build_evidence_view(
            run=run,
            case=case,
            item=item,
            variant=variant,
            experiment=experiment,
            episode=episode,
            turns=turns,
            agent_statuses=agent_statuses,
            steps=steps,
            approvals=approvals,
            registry=self.registry,
        )

    @staticmethod
    def _ordered_agent_run_ids(
        case: EvaluationExperimentCaseResult,
        turns: Sequence[InteractiveEpisodeTurn],
    ) -> list[UUID]:
        ordered: list[UUID] = []
        seen: set[UUID] = set()
        for agent_run_id in [*(turn.agent_run_id for turn in turns), case.agent_run_id]:
            if agent_run_id is not None and agent_run_id not in seen:
                ordered.append(agent_run_id)
                seen.add(agent_run_id)
        return ordered

    @staticmethod
    def _require_read(context: WorkspaceExecutionContext) -> None:
        if EVALUATION_READ not in context.permissions:
            raise AgentHubError("FORBIDDEN", "You do not have permission.", 403)

    @staticmethod
    def _workspace_id(context: WorkspaceExecutionContext) -> UUID:
        try:
            return UUID(context.workspace_id)
        except ValueError:
            raise AgentHubError("INVALID_WORKSPACE", "Workspace context is invalid.", 500) from None


def build_evidence_view(
    *,
    run: EvaluationExperimentRun,
    case: EvaluationExperimentCaseResult,
    item: EvaluationDatasetItem | None,
    variant: EvaluationExperimentVariant | None,
    experiment: EvaluationExperiment | None,
    episode: InteractiveEpisode | None,
    turns: Sequence[InteractiveEpisodeTurn],
    agent_statuses: Mapping[UUID, str],
    steps: Sequence[RunStep],
    approvals: Sequence[Approval | Mapping[str, Any]] = (),
    registry: EvaluatorRegistry,
) -> dict[str, Any]:
    """Build the JSON-safe response from already authorized, persisted rows."""

    observation = case.observation if isinstance(case.observation, Mapping) else {}
    expected = item.expected if item is not None and isinstance(item.expected, Mapping) else {}
    category = _string_or_none(item.category) if item is not None else None
    task_success, task_success_value = _task_success(
        category=category,
        expected=expected,
        observation=observation,
        experiment=experiment,
        registry=registry,
    )
    projected_steps = _project_steps(steps, approvals)

    return {
        "schema_version": EVIDENCE_VIEW_SCHEMA_VERSION,
        "case_id": str(case.id),
        "case_key": _string_or_none(item.case_key) if item is not None else None,
        "category": category,
        "variant_label": _string_or_none(variant.label) if variant is not None else None,
        "task_success": task_success,
        "evidence_level": _string_or_none(run.evidence_level),
        "execution_cleanup_status": _historical_cleanup_status(observation),
        "termination_reason": _historical_termination_reason(observation),
        "episode": _project_episode(episode, turns, agent_statuses),
        "steps": projected_steps,
        "failure_labels": _project_failure_labels(
            observation=observation,
            expected=expected,
            task_success=task_success_value,
            steps=projected_steps,
        ),
    }


def _task_success(
    *,
    category: str | None,
    expected: Mapping[str, Any],
    observation: Mapping[str, Any],
    experiment: EvaluationExperiment | None,
    registry: EvaluatorRegistry,
) -> tuple[dict[str, Any], bool | None]:
    if category is None:
        return _task_success_unavailable("case_category_missing"), None
    if experiment is None:
        return _task_success_unavailable("experiment_missing"), None
    manifest = experiment.evaluator_manifest
    if not isinstance(manifest, Mapping):
        return _task_success_unavailable("evaluator_manifest_missing"), None
    try:
        frozen_registry = registry.for_manifest(manifest)
    except (AttributeError, KeyError, TypeError, ValueError):
        return _task_success_unavailable("evaluator_input_invalid"), None
    return _evaluate_task_success(
        category=category,
        expected=expected,
        observation=observation,
        registry=frozen_registry,
    )


def _evaluate_task_success(
    *,
    category: str | None,
    expected: Mapping[str, Any],
    observation: Mapping[str, Any],
    registry: EvaluatorRegistry,
) -> tuple[dict[str, Any], bool | None]:
    if category is None:
        return _task_success_unavailable("case_category_missing"), None
    try:
        evaluated = registry.evaluate(category, expected, observation)
    except (AttributeError, KeyError, TypeError, ValueError):
        return _task_success_unavailable("evaluator_input_invalid"), None
    metric = evaluated.get("task_success")
    if not isinstance(metric, MetricValue):
        return _task_success_unavailable("task_success_missing"), None
    value = _finite_number(metric.value)
    task_value = (
        float(value) >= 1.0
        if metric.status == MetricStatus.AVAILABLE and value is not None
        else None
    )
    return {
        "status": str(metric.status),
        "value": value,
        "reason": _string_or_none(metric.reason),
    }, task_value


def _task_success_unavailable(reason: str) -> dict[str, Any]:
    return {
        "status": MetricStatus.NOT_AVAILABLE.value,
        "value": None,
        "reason": reason,
    }


def _project_episode(
    episode: InteractiveEpisode | None,
    turns: Sequence[InteractiveEpisodeTurn],
    agent_statuses: Mapping[UUID, str],
) -> dict[str, Any] | None:
    if episode is None:
        return None
    return {
        "episode_id": str(episode.case_id),
        "execution_status": episode.execution_status,
        "cleanup_status": episode.cleanup_status,
        "cleanup_error": _string_or_none(episode.cleanup_error),
        "termination_reason": _string_or_none(episode.termination_reason),
        "thread_id": str(episode.thread_id) if episode.thread_id is not None else None,
        "observation_hash": _string_or_none(episode.observation_hash),
        "assertion_hash": _string_or_none(episode.assertion_hash),
        "turns": [
            {
                "turn_index": turn.turn_index,
                "agent_run_id": (str(turn.agent_run_id) if turn.agent_run_id is not None else None),
                "status": (
                    _string_or_none(agent_statuses.get(turn.agent_run_id))
                    if turn.agent_run_id is not None
                    else None
                ),
            }
            for turn in turns
        ],
    }


def _project_steps(
    steps: Sequence[RunStep],
    approvals: Sequence[Approval | Mapping[str, Any]],
) -> list[dict[str, Any]]:
    approval_facts = [_approval_fact(item) for item in approvals]
    approval_by_logical = {
        str(item["logical_action_id"]): item
        for item in approval_facts
        if item["logical_action_id"] is not None
    }
    wait_to_approval, approval_orders = _match_approvals_to_waits(steps, approval_facts)
    represented_actions = _represented_action_ids(steps)
    proposal_hashes = _proposal_hash_index(steps)
    run_positions: dict[UUID, int] = {}
    for step in steps:
        run_positions.setdefault(step.agent_run_id, len(run_positions))
    for approval in approval_facts:
        run_id = approval["run_id"]
        if isinstance(run_id, UUID):
            run_positions.setdefault(run_id, len(run_positions))

    projected: list[tuple[int, int | float, int, int, dict[str, Any]]] = []
    for step in steps:
        metadata = step.safe_metadata if isinstance(step.safe_metadata, Mapping) else {}
        run_position = run_positions.get(step.agent_run_id, len(run_positions))
        step_order = step.sequence_number
        calls: list[dict[str, Any]]
        if step.kind == "TOOL_EXECUTE":
            calls = _project_tool_execute(step, metadata, proposal_hashes)
        elif step.kind == "ACTION":
            calls = _project_action(step, metadata, approval_by_logical)
        elif step.kind == "TOOL_PROPOSAL":
            calls = _project_parallel_calls(step, metadata)
        elif step.kind == "APPROVAL_WAIT":
            approval = wait_to_approval.get(step.id)
            calls = [_project_approval_wait(step, metadata, approval)]
        else:
            calls = [_base_step(step, metadata)]
        for call_index, payload in enumerate(calls):
            projected.append((run_position, step_order, 0, call_index, payload))

    for approval_index, approval in enumerate(approval_facts):
        logical_action_id = approval["logical_action_id"]
        run_id = approval["run_id"]
        if (
            logical_action_id is None
            or logical_action_id in represented_actions
            or approval["execution_status"] in {None, "NOT_STARTED"}
            or not isinstance(run_id, UUID)
        ):
            continue
        recorded_order = approval_orders.get(str(approval["id"]))
        step_order = recorded_order if recorded_order is not None else 0
        projected.append(
            (
                run_positions[run_id],
                recorded_order if recorded_order is not None else math.inf,
                1,
                approval_index,
                _approval_execution_payload(approval, step_order=step_order),
            )
        )

    projected.sort(key=lambda item: item[:4])
    return [item[4] for item in projected]


def _project_parallel_calls(
    step: RunStep,
    metadata: Mapping[str, Any],
) -> list[dict[str, Any]]:
    identities = _metadata_string_list(metadata, "tool_identities")
    if not identities:
        return [_base_step(step, metadata)]
    hashes = _metadata_string_list(metadata, "arguments_hashes")
    aligned_hashes = hashes if len(hashes) == len(identities) else []
    return [
        _step_payload(
            step,
            metadata,
            tool_name=identity,
            arguments_hash=aligned_hashes[index] if aligned_hashes else None,
            status=step.status if len(identities) == 1 else None,
            error_code=(
                _string_or_none(metadata.get("error_code")) if len(identities) == 1 else None
            ),
        )
        for index, identity in enumerate(identities)
    ]


def _project_tool_execute(
    step: RunStep,
    metadata: Mapping[str, Any],
    proposal_hashes: Mapping[tuple[UUID, str], Mapping[str, str]],
) -> list[dict[str, Any]]:
    results = _metadata_mappings(metadata, "tool_results")
    if results:
        return [
            _step_payload(
                step,
                metadata,
                tool_name=_string_or_none(result.get("tool_identity")),
                arguments_hash=_tool_result_hash(
                    metadata,
                    result,
                    step.agent_run_id,
                    proposal_hashes,
                ),
                status=_string_or_none(result.get("status")),
                error_code=_string_or_none(result.get("error_code")),
            )
            for result in results
        ]

    identities = _metadata_string_list(metadata, "tool_identities")
    if not identities:
        return [_base_step(step, metadata)]
    hashes = _metadata_string_list(metadata, "arguments_hashes")
    aligned_hashes = hashes if len(hashes) == len(identities) else []
    call_ids = _metadata_string_list(metadata, "tool_call_ids")
    aligned_call_ids = call_ids if len(call_ids) == len(identities) else []
    return [
        _step_payload(
            step,
            metadata,
            tool_name=identity,
            arguments_hash=(
                aligned_hashes[index]
                if aligned_hashes
                else _proposal_hash(
                    step.agent_run_id,
                    aligned_call_ids[index] if aligned_call_ids else None,
                    identity,
                    proposal_hashes,
                )
            ),
            status=step.status if len(identities) == 1 else None,
            error_code=(
                _string_or_none(metadata.get("error_code")) if len(identities) == 1 else None
            ),
        )
        for index, identity in enumerate(identities)
    ]


def _project_action(
    step: RunStep,
    metadata: Mapping[str, Any],
    approval_by_logical: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    results = _metadata_mappings(metadata, "action_results")
    if results:
        return [
            _step_payload(
                step,
                metadata,
                tool_name=_string_or_none(result.get("tool_identity")),
                logical_action_id=_string_or_none(result.get("logical_action_id")),
                arguments_hash=_action_result_hash(
                    metadata,
                    result,
                    approval_by_logical,
                ),
                status=(
                    _string_or_none(result.get("status"))
                    or (step.status if len(results) == 1 else None)
                ),
                error_code=(
                    _string_or_none(result.get("error_code"))
                    or (_string_or_none(metadata.get("error_code")) if len(results) == 1 else None)
                ),
            )
            for result in results
        ]

    logical_ids = _metadata_string_list(metadata, "logical_action_ids")
    if not logical_ids:
        return [_base_step(step, metadata)]
    identities = _metadata_string_list(metadata, "tool_identities")
    hashes = _metadata_string_list(metadata, "arguments_hashes")
    aligned_identities = identities if len(identities) == len(logical_ids) else []
    aligned_hashes = hashes if len(hashes) == len(logical_ids) else []
    return [
        _step_payload(
            step,
            metadata,
            tool_name=aligned_identities[index] if aligned_identities else None,
            logical_action_id=logical_action_id,
            arguments_hash=(
                aligned_hashes[index]
                if aligned_hashes
                else approval_by_logical.get(logical_action_id, {}).get("arguments_hash")
            ),
            status=step.status if len(logical_ids) == 1 else None,
            error_code=(
                _string_or_none(metadata.get("error_code")) if len(logical_ids) == 1 else None
            ),
        )
        for index, logical_action_id in enumerate(logical_ids)
    ]


def _project_approval_wait(
    step: RunStep,
    metadata: Mapping[str, Any],
    approval: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if approval is None:
        return _base_step(step, metadata)
    return _step_payload(
        step,
        metadata,
        tool_name=_string_or_none(approval["tool_identity"]),
        logical_action_id=_string_or_none(approval["logical_action_id"]),
        arguments_hash=_string_or_none(approval["arguments_hash"]),
        status=_string_or_none(approval["decision_status"]),
        error_code=None,
    )


def _approval_execution_payload(
    approval: Mapping[str, Any],
    *,
    step_order: int,
) -> dict[str, Any]:
    return {
        "step_order": step_order,
        "step_type": "APPROVAL_EXECUTION",
        "agent_run_id": str(approval["run_id"]),
        "logical_action_id": _string_or_none(approval["logical_action_id"]),
        "arguments_hash": _string_or_none(approval["arguments_hash"]),
        "status": _string_or_none(approval["execution_status"]),
        "error_code": _string_or_none(approval["failure_code"]),
        "tool_name": _string_or_none(approval["tool_identity"]),
        "policy_decision": None,
    }


def _base_step(step: RunStep, metadata: Mapping[str, Any]) -> dict[str, Any]:
    identities = _metadata_string_list(metadata, "tool_identities")
    logical_ids = _metadata_string_list(metadata, "logical_action_ids")
    hashes = _metadata_string_list(metadata, "arguments_hashes")
    return _step_payload(
        step,
        metadata,
        tool_name=(
            _string_or_none(metadata.get("tool_name"))
            or (identities[0] if len(identities) == 1 else None)
        ),
        logical_action_id=(
            _string_or_none(metadata.get("logical_action_id"))
            or (logical_ids[0] if len(logical_ids) == 1 else None)
        ),
        arguments_hash=(
            _string_or_none(metadata.get("arguments_hash"))
            or (hashes[0] if len(hashes) == 1 else None)
        ),
    )


def _step_payload(
    step: RunStep,
    metadata: Mapping[str, Any],
    *,
    tool_name: str | None,
    logical_action_id: str | None = None,
    arguments_hash: str | None = None,
    status: str | None | object = _UNSET,
    error_code: str | None | object = _UNSET,
) -> dict[str, Any]:
    return {
        "step_order": step.sequence_number,
        "step_type": step.kind,
        "agent_run_id": str(step.agent_run_id),
        "logical_action_id": logical_action_id,
        "arguments_hash": arguments_hash,
        "status": (_string_or_none(step.status) if status is _UNSET else status),
        "error_code": (
            _string_or_none(metadata.get("error_code")) if error_code is _UNSET else error_code
        ),
        "tool_name": tool_name,
        "policy_decision": _string_or_none(metadata.get("policy_decision")),
    }


def _approval_fact(item: Approval | Mapping[str, Any]) -> dict[str, Any]:
    def value(key: str) -> Any:
        if isinstance(item, Mapping):
            return item.get(key)
        return getattr(item, key, None)

    return {
        "id": str(value("id")) if value("id") is not None else None,
        "run_id": value("run_id"),
        "logical_action_id": _string_or_none(value("logical_action_id")),
        "tool_identity": _string_or_none(value("tool_identity")),
        "arguments_hash": _string_or_none(value("canonical_args_hash") or value("arguments_hash")),
        "decision_status": _string_or_none(value("decision_status")),
        "execution_status": _string_or_none(value("execution_status")),
        "failure_code": _string_or_none(value("failure_code")),
    }


def _match_approvals_to_waits(
    steps: Sequence[RunStep],
    approvals: Sequence[Mapping[str, Any]],
) -> tuple[dict[UUID, Mapping[str, Any]], dict[str, int]]:
    waits: dict[tuple[UUID, str], list[RunStep]] = {}
    for step in steps:
        if step.kind != "APPROVAL_WAIT":
            continue
        metadata = step.safe_metadata if isinstance(step.safe_metadata, Mapping) else {}
        identities = _metadata_string_list(metadata, "tool_identities")
        if len(identities) != 1:
            continue
        waits.setdefault((step.agent_run_id, identities[0]), []).append(step)

    approvals_by_identity: dict[tuple[UUID, str], list[Mapping[str, Any]]] = {}
    for approval in approvals:
        run_id = approval.get("run_id")
        tool_identity = approval.get("tool_identity")
        if isinstance(run_id, UUID) and tool_identity is not None:
            approvals_by_identity.setdefault((run_id, tool_identity), []).append(approval)

    wait_to_approval: dict[UUID, Mapping[str, Any]] = {}
    approval_orders: dict[str, int] = {}
    for key, identity_waits in waits.items():
        identity_approvals = approvals_by_identity.get(key, [])
        # A wait row records only the tool identity. Pair it with an Approval only
        # when that identity is unique; ordering is not a durable approval foreign key.
        if len(identity_waits) != 1 or len(identity_approvals) != 1:
            continue
        wait = identity_waits[0]
        approval = identity_approvals[0]
        wait_to_approval[wait.id] = approval
        approval_id = approval.get("id")
        if approval_id is not None:
            approval_orders[str(approval_id)] = wait.sequence_number
    return wait_to_approval, approval_orders


def _represented_action_ids(steps: Sequence[RunStep]) -> set[str]:
    represented: set[str] = set()
    for step in steps:
        if step.kind != "ACTION":
            continue
        metadata = step.safe_metadata if isinstance(step.safe_metadata, Mapping) else {}
        results = _metadata_mappings(metadata, "action_results")
        for result in results:
            logical_action_id = _string_or_none(result.get("logical_action_id"))
            if logical_action_id is not None:
                represented.add(logical_action_id)
        if not results:
            represented.update(_metadata_string_list(metadata, "logical_action_ids"))
    return represented


def _tool_result_hash(
    metadata: Mapping[str, Any],
    result: Mapping[str, Any],
    agent_run_id: UUID,
    proposal_hashes: Mapping[tuple[UUID, str], Mapping[str, str]],
) -> str | None:
    direct = _string_or_none(result.get("arguments_hash"))
    if direct is not None:
        return direct
    return _ordered_call_hash(
        metadata,
        call_id=_string_or_none(result.get("tool_call_id")),
        identity=_string_or_none(result.get("tool_identity")),
    ) or _proposal_hash(
        agent_run_id,
        _string_or_none(result.get("tool_call_id")),
        _string_or_none(result.get("tool_identity")),
        proposal_hashes,
    )


def _action_result_hash(
    metadata: Mapping[str, Any],
    result: Mapping[str, Any],
    approval_by_logical: Mapping[str, Mapping[str, Any]],
) -> str | None:
    direct = _string_or_none(result.get("arguments_hash"))
    if direct is not None:
        return direct
    logical_action_id = _string_or_none(result.get("logical_action_id"))
    if logical_action_id is not None:
        approval = approval_by_logical.get(logical_action_id)
        if approval is not None:
            approval_hash = _string_or_none(approval.get("arguments_hash"))
            if approval_hash is not None:
                return approval_hash
    return _ordered_call_hash(
        metadata,
        call_id=_string_or_none(result.get("tool_call_id")),
        identity=_string_or_none(result.get("tool_identity")),
    )


def _ordered_call_hash(
    metadata: Mapping[str, Any],
    *,
    call_id: str | None,
    identity: str | None,
) -> str | None:
    if call_id is None or identity is None:
        return None
    identities = _metadata_string_list(metadata, "tool_identities")
    hashes = _metadata_string_list(metadata, "arguments_hashes")
    if not identities or len(identities) != len(hashes):
        return None

    call_ids = _metadata_string_list(metadata, "tool_call_ids")
    if call_id is not None and len(call_ids) == len(identities):
        positions = [index for index, value in enumerate(call_ids) if value == call_id]
        if len(positions) == 1:
            index = positions[0]
            return hashes[index] if identities[index] == identity else None
    return None


def _proposal_hash_index(
    steps: Sequence[RunStep],
) -> dict[tuple[UUID, str], Mapping[str, str]]:
    indexed: dict[tuple[UUID, str], Mapping[str, str]] = {}
    ambiguous: set[tuple[UUID, str]] = set()
    for step in steps:
        if step.kind != "TOOL_PROPOSAL":
            continue
        metadata = step.safe_metadata if isinstance(step.safe_metadata, Mapping) else {}
        call_ids = _metadata_string_list(metadata, "tool_call_ids")
        identities = _metadata_string_list(metadata, "tool_identities")
        hashes = _metadata_string_list(metadata, "arguments_hashes")
        if not (len(call_ids) == len(identities) == len(hashes)):
            continue
        for call_id, identity, arguments_hash in zip(call_ids, identities, hashes, strict=True):
            key = (step.agent_run_id, call_id)
            if key in indexed:
                ambiguous.add(key)
                continue
            indexed[key] = {
                "tool_identity": identity,
                "arguments_hash": arguments_hash,
            }
    for key in ambiguous:
        indexed.pop(key, None)
    return indexed


def _proposal_hash(
    agent_run_id: UUID,
    call_id: str | None,
    identity: str | None,
    proposal_hashes: Mapping[tuple[UUID, str], Mapping[str, str]],
) -> str | None:
    if call_id is None or identity is None:
        return None
    proposal = proposal_hashes.get((agent_run_id, call_id))
    if proposal is None or proposal.get("tool_identity") != identity:
        return None
    return proposal.get("arguments_hash")


def _project_failure_labels(
    *,
    observation: Mapping[str, Any],
    expected: Mapping[str, Any],
    task_success: bool | None,
    steps: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    labels = observation.get("failure_taxonomy")
    if not isinstance(labels, list):
        labels = classify_failure(expected, observation, task_success=task_success)

    index_to_order = _trajectory_index_to_step_order(observation)
    step_order_counts: dict[int, int] = {}
    for step in steps:
        step_order = _integer_or_none(step.get("step_order"))
        if step_order is not None:
            step_order_counts[step_order] = step_order_counts.get(step_order, 0) + 1
    grouped: dict[str, list[int]] = {}
    for label in labels:
        if not isinstance(label, Mapping):
            continue
        code = _string_or_none(label.get("code"))
        if code is None:
            continue
        orders = grouped.setdefault(code, [])
        for step_order in _label_step_orders(label, index_to_order):
            if step_order_counts.get(step_order) == 1 and step_order not in orders:
                orders.append(step_order)
    return [
        {"code": code, "step_orders": sorted(step_orders)} for code, step_orders in grouped.items()
    ]


def _trajectory_index_to_step_order(
    observation: Mapping[str, Any],
) -> dict[int, int]:
    indexed_orders: dict[int, set[int]] = {}
    trajectory_steps = observation.get("trajectory_steps")
    if isinstance(trajectory_steps, list):
        for position, step in enumerate(trajectory_steps):
            if not isinstance(step, Mapping):
                continue
            index = _integer_or_none(step.get("index"))
            step_order = _integer_or_none(step.get("sequence_number"))
            if step_order is not None:
                indexed_orders.setdefault(index if index is not None else position, set()).add(
                    step_order
                )
    return {
        index: next(iter(orders)) for index, orders in indexed_orders.items() if len(orders) == 1
    }


def _label_step_orders(
    label: Mapping[str, Any],
    index_to_order: Mapping[int, int],
) -> list[int]:
    recorded_orders = label.get("step_orders")
    if isinstance(recorded_orders, Sequence) and not isinstance(recorded_orders, (str, bytes)):
        return [
            order for value in recorded_orders if (order := _integer_or_none(value)) is not None
        ]
    step_index = _integer_or_none(label.get("step_index"))
    if step_index is None:
        return []
    step_order = index_to_order.get(step_index)
    return [step_order] if step_order is not None else []


def _historical_cleanup_status(observation: Mapping[str, Any]) -> str | None:
    for key in ("execution_cleanup_status", "environment_lifecycle_status"):
        value = _string_or_none(observation.get(key))
        if value is not None:
            return value
    return None


def _historical_termination_reason(observation: Mapping[str, Any]) -> str | None:
    direct = _string_or_none(observation.get("termination_reason"))
    if direct is not None:
        return direct
    recovery = observation.get("interactive_recovery")
    if isinstance(recovery, Mapping):
        recovered_reason = _string_or_none(recovery.get("reason"))
        if recovered_reason is not None:
            return recovered_reason
    return _string_or_none(observation.get("stop_reason"))


def _metadata_string_list(metadata: Mapping[str, Any], key: str) -> list[str]:
    values = metadata.get(key)
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return []
    if not all(isinstance(value, str) and value for value in values):
        return []
    return list(values)


def _metadata_mappings(metadata: Mapping[str, Any], key: str) -> list[Mapping[str, Any]]:
    values = metadata.get(key)
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return []
    return [value for value in values if isinstance(value, Mapping)]


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _integer_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _finite_number(value: Any) -> float | int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        number = float(value)
    elif isinstance(value, (float, int)):
        number = float(value)
    else:
        return None
    if not math.isfinite(number):
        return None
    return int(number) if number.is_integer() else number


__all__ = [
    "EVIDENCE_VIEW_SCHEMA_VERSION",
    "EvaluationEvidenceViewService",
    "build_evidence_view",
]
