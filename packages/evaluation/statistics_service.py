"""Read-only reliability and uncertainty summary for one experiment run (AE-2).

The existing metrics endpoint reports aggregated point estimates.  This service rebuilds
the per-case trial outcomes from persisted case results and the frozen evaluator
manifest, then reports ``pass^k``, a small-sample interval, variance and consistency.
It writes nothing, so it can be called on a historical run without changing it.
"""

from __future__ import annotations

from statistics import mean
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from packages.control_plane.rbac import EVALUATION_READ
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.evaluation.metrics import EvaluatorRegistry, MetricStatus
from packages.evaluation.metrics_service import EvaluationMetricsService
from packages.evaluation.statistics import (
    TrialOutcome,
    cluster_bootstrap_interval,
    paired_bootstrap_interval,
    statistical_summary,
)
from packages.evaluation.taxonomy import TAXONOMY_VERSION, classify_failure, summarize_labels


class EvaluationStatisticsService:
    def __init__(self, registry: EvaluatorRegistry | None = None) -> None:
        self.registry = registry or EvaluatorRegistry()

    async def compute(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
        run_id: UUID,
    ) -> dict[str, Any]:
        self._require_read(context)
        workspace_id = self._workspace_id(context)
        metrics_service = EvaluationMetricsService(self.registry)
        run = await metrics_service._load_run(session, workspace_id, run_id)
        experiment = await metrics_service._load_experiment(
            session, workspace_id, run.experiment_id
        )
        try:
            registry = self.registry.for_manifest(experiment.evaluator_manifest)
        except ValueError:
            raise AgentHubError(
                "EXPERIMENT_EVALUATOR_VERSION_MISMATCH",
                "The persisted evaluator manifest is not compatible.",
                409,
            ) from None
        rows = await metrics_service._load_and_validate_case_set(
            session, workspace_id, run, experiment
        )

        outcomes_by_variant: dict[UUID, list[TrialOutcome]] = {}
        labels: dict[UUID, str] = {}
        for case, item, variant in rows:
            evaluated = registry.evaluate(item.category, item.expected, case.observation)
            task = evaluated.get("task_success")
            success: bool | None = None
            if (
                task is not None
                and task.status == MetricStatus.AVAILABLE
                and task.value is not None
            ):
                success = float(task.value) >= 1.0
            output_hash = case.observation.get("output_hash")
            outcomes_by_variant.setdefault(variant.id, []).append(
                TrialOutcome(
                    case_key=item.case_key,
                    trial_index=case.repetition_index,
                    success=success,
                    output_hash=str(output_hash) if isinstance(output_hash, str) else None,
                )
            )
            labels[variant.id] = variant.label

        variants: dict[str, Any] = {}
        for variant_id, outcomes in outcomes_by_variant.items():
            summary = statistical_summary(outcomes, k=run.repetitions)
            case_all_success = summary.pop("case_all_success", {})
            case_success_rates = summary.pop("case_success_rates", {})
            if case_all_success:
                summary["pass_hat_k_interval"] = _interval(
                    [float(value) for value in case_all_success.values()]
                )
            if case_success_rates:
                summary["success_rate_interval_bootstrap"] = _interval(
                    [float(value) for value in case_success_rates.values()]
                )
            summary["variant_id"] = str(variant_id)
            summary["variant_label"] = labels[variant_id]
            variants[str(variant_id)] = summary

        return {
            "run_id": str(run_id),
            "experiment_id": str(run.experiment_id),
            "repetitions": run.repetitions,
            "split": run.split,
            "purpose": run.purpose,
            "evidence_level": run.evidence_level,
            "variants": variants,
        }

    async def failure_taxonomy(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
        run_id: UUID,
    ) -> dict[str, Any]:
        """Aggregate deterministic failure labels per variant.

        A persisted label list is reused when present; otherwise the same rules are
        applied on read, so a run executed before AE-5 still gets a taxonomy.
        """

        self._require_read(context)
        workspace_id = self._workspace_id(context)
        metrics_service = EvaluationMetricsService(self.registry)
        run = await metrics_service._load_run(session, workspace_id, run_id)
        experiment = await metrics_service._load_experiment(
            session, workspace_id, run.experiment_id
        )
        registry = self.registry.for_manifest(experiment.evaluator_manifest)
        rows = await metrics_service._load_and_validate_case_set(
            session, workspace_id, run, experiment
        )

        per_variant: dict[UUID, dict[str, Any]] = {}
        for case, item, variant in rows:
            evaluated = registry.evaluate(item.category, item.expected, case.observation)
            task = evaluated.get("task_success")
            task_success: bool | None = None
            if (
                task is not None
                and task.status == MetricStatus.AVAILABLE
                and task.value is not None
            ):
                task_success = float(task.value) >= 1.0
            labels = case.observation.get("failure_taxonomy")
            if not isinstance(labels, list):
                labels = classify_failure(
                    item.expected, case.observation, task_success=task_success
                )
            bucket = per_variant.setdefault(
                variant.id,
                {
                    "variant_label": variant.label,
                    "case_count": 0,
                    "labeled_case_count": 0,
                    "counts": {},
                },
            )
            bucket["case_count"] += 1
            if labels:
                bucket["labeled_case_count"] += 1
            for code, count in summarize_labels(labels).items():
                bucket["counts"][code] = bucket["counts"].get(code, 0) + count

        return {
            "run_id": str(run_id),
            "experiment_id": str(run.experiment_id),
            "evidence_level": run.evidence_level,
            "taxonomy_version": TAXONOMY_VERSION,
            "variants": {
                str(variant_id): {
                    **bucket,
                    "counts": dict(sorted(bucket["counts"].items())),
                }
                for variant_id, bucket in per_variant.items()
            },
        }

    async def paired_comparison(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
        run_id: UUID,
        baseline_variant_id: UUID,
        candidate_variant_id: UUID,
    ) -> dict[str, Any]:
        """Paired bootstrap of the candidate-minus-baseline success-rate delta."""

        self._require_read(context)
        workspace_id = self._workspace_id(context)
        metrics_service = EvaluationMetricsService(self.registry)
        run = await metrics_service._load_run(session, workspace_id, run_id)
        experiment = await metrics_service._load_experiment(
            session, workspace_id, run.experiment_id
        )
        registry = self.registry.for_manifest(experiment.evaluator_manifest)
        rows = await metrics_service._load_and_validate_case_set(
            session, workspace_id, run, experiment
        )

        per_variant: dict[UUID, dict[str, list[float]]] = {}
        for case, item, variant in rows:
            evaluated = registry.evaluate(item.category, item.expected, case.observation)
            task = evaluated.get("task_success")
            value: float | None = None
            if (
                task is not None
                and task.status == MetricStatus.AVAILABLE
                and task.value is not None
            ):
                value = float(task.value)
            per_variant.setdefault(variant.id, {}).setdefault(item.case_key, []).append(
                value  # type: ignore[arg-type]
            )

        baseline = per_variant.get(baseline_variant_id, {})
        candidate = per_variant.get(candidate_variant_id, {})
        pairs: list[tuple[float, float]] = []
        for case_key in set(baseline).intersection(candidate):
            baseline_values = [value for value in baseline[case_key] if value is not None]
            candidate_values = [value for value in candidate[case_key] if value is not None]
            if baseline_values and candidate_values:
                pairs.append((mean(baseline_values), mean(candidate_values)))
        delta = mean([after - before for before, after in pairs]) if pairs else None
        interval = paired_bootstrap_interval(pairs)
        return {
            "run_id": str(run_id),
            "baseline_variant_id": str(baseline_variant_id),
            "candidate_variant_id": str(candidate_variant_id),
            "pair_count": len(pairs),
            "delta": delta,
            "interval": (
                {
                    "method": "paired_bootstrap",
                    "lower": interval[0],
                    "upper": interval[1],
                    "bootstrap_count": 2000,
                }
                if interval
                else None
            ),
        }

    def _require_read(self, context: WorkspaceExecutionContext) -> None:
        if EVALUATION_READ not in context.permissions:
            raise AgentHubError("FORBIDDEN", "Evaluation read permission is required.", 403)

    def _workspace_id(self, context: WorkspaceExecutionContext) -> UUID:
        try:
            return UUID(context.workspace_id)
        except (TypeError, ValueError):
            raise AgentHubError("FORBIDDEN", "Invalid workspace context.", 403) from None


def _interval(values: list[float]) -> dict[str, Any] | None:
    interval = cluster_bootstrap_interval(values)
    if interval is None:
        return None
    return {
        "method": "cluster_bootstrap",
        "lower": interval[0],
        "upper": interval[1],
        "bootstrap_count": 2000,
    }


__all__ = ["EvaluationStatisticsService"]
