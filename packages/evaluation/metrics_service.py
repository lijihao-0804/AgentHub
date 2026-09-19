"""Persistence service for M7-D metrics and paired comparisons."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from decimal import Decimal
from statistics import mean, pstdev
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.control_plane.rbac import EVALUATION_READ
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.evaluation.metrics import (
    EvaluatorRegistry,
    aggregate_metric_values,
    compare_metric_values,
    evaluate_case,
    metric_direction,
    percentile,
)
from packages.evaluation.models import (
    EvaluationDatasetItem,
    EvaluationExperiment,
    EvaluationExperimentCaseResult,
    EvaluationExperimentComparison,
    EvaluationExperimentRun,
    EvaluationExperimentVariant,
    EvaluationMetricResult,
)


class EvaluationMetricsService:
    def __init__(self, registry: EvaluatorRegistry | None = None) -> None:
        self.registry = registry or EvaluatorRegistry()

    async def compute_run_metrics(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
        run_id: UUID,
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
            self._not_found("EVALUATION_EXPERIMENT_RUN_NOT_FOUND")
        experiment = await session.scalar(
            select(EvaluationExperiment).where(
                EvaluationExperiment.workspace_id == workspace_id,
                EvaluationExperiment.id == run.experiment_id,
            )
        )
        if experiment is None or not experiment.spec_json:
            self._integrity_error()
        try:
            self.registry.validate_manifest(experiment.evaluator_manifest)
        except ValueError as exc:
            raise AgentHubError(
                "EXPERIMENT_EVALUATOR_VERSION_MISMATCH",
                "The persisted evaluator manifest is not compatible.",
                409,
            ) from exc
        rows = list(
            await session.execute(
                select(
                    EvaluationExperimentCaseResult,
                    EvaluationDatasetItem,
                    EvaluationExperimentVariant,
                )
                .join(
                    EvaluationDatasetItem,
                    EvaluationDatasetItem.id == EvaluationExperimentCaseResult.dataset_item_id,
                )
                .join(
                    EvaluationExperimentVariant,
                    EvaluationExperimentVariant.id
                    == EvaluationExperimentCaseResult.experiment_variant_id,
                )
                .where(
                    EvaluationExperimentCaseResult.workspace_id == workspace_id,
                    EvaluationExperimentCaseResult.experiment_run_id == run_id,
                )
                .order_by(EvaluationExperimentCaseResult.created_at)
            )
        )
        if any(case.status not in {"SUCCEEDED", "FAILED", "CANCELLED"} for case, _, _ in rows):
            raise AgentHubError(
                "EVALUATION_METRICS_RUN_NOT_TERMINAL",
                "Metrics require a terminal experiment run.",
                409,
            )
        await session.execute(
            delete(EvaluationMetricResult).where(
                EvaluationMetricResult.workspace_id == workspace_id,
                EvaluationMetricResult.experiment_run_id == run_id,
            )
        )
        output: dict[str, Any] = {"run_id": str(run_id), "variants": {}}
        by_variant: dict[UUID, list[tuple[Any, Any, Any]]] = defaultdict(list)
        for row in rows:
            by_variant[row[2].id].append(row)
        for variant_id, variant_rows in by_variant.items():
            metrics = self._variant_metrics(variant_rows)
            output["variants"][str(variant_id)] = metrics
            for metric_name, metric in _metric_items(metrics):
                session.add(
                    EvaluationMetricResult(
                        workspace_id=workspace_id,
                        experiment_run_id=run_id,
                        experiment_variant_id=variant_id,
                        dimension="VARIANT",
                        category="ALL",
                        metric_name=metric_name,
                        status=str(metric["status"]),
                        value=_decimal_or_none(metric.get("value")),
                        sample_count=int(metric.get("sample_count", 0)),
                        numerator=_decimal_or_none(metric.get("numerator")),
                        denominator=_decimal_or_none(metric.get("denominator")),
                        reason=metric.get("reason"),
                        evaluator_version=str(metric.get("evaluator_version", "v1")),
                        direction=str(metric_direction(metric_name)),
                        details=metric,
                    )
                )
            for category, category_metrics in metrics.get("categories", {}).items():
                for metric_name, metric in _metric_items(category_metrics):
                    session.add(
                        EvaluationMetricResult(
                            workspace_id=workspace_id,
                            experiment_run_id=run_id,
                            experiment_variant_id=variant_id,
                            dimension="CATEGORY",
                            category=category,
                            metric_name=metric_name,
                            status=str(metric["status"]),
                            value=_decimal_or_none(metric.get("value")),
                            sample_count=int(metric.get("sample_count", 0)),
                            numerator=_decimal_or_none(metric.get("numerator")),
                            denominator=_decimal_or_none(metric.get("denominator")),
                            reason=metric.get("reason"),
                            evaluator_version=str(metric.get("evaluator_version", "v1")),
                            direction=str(metric_direction(metric_name)),
                            details=metric,
                        )
                    )
        await session.commit()
        return output

    async def create_comparison(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
        run_id: UUID,
        baseline_variant_id: UUID,
        candidate_variant_id: UUID,
    ) -> EvaluationExperimentComparison:
        self._require_read(context)
        workspace_id = self._workspace_id(context)
        run = await session.scalar(
            select(EvaluationExperimentRun).where(
                EvaluationExperimentRun.workspace_id == workspace_id,
                EvaluationExperimentRun.id == run_id,
            )
        )
        if run is None:
            self._not_found("EVALUATION_EXPERIMENT_RUN_NOT_FOUND")
        baseline = await session.scalar(
            select(EvaluationExperimentVariant).where(
                EvaluationExperimentVariant.workspace_id == workspace_id,
                EvaluationExperimentVariant.id == baseline_variant_id,
                EvaluationExperimentVariant.experiment_id == run.experiment_id,
            )
        )
        candidate = await session.scalar(
            select(EvaluationExperimentVariant).where(
                EvaluationExperimentVariant.workspace_id == workspace_id,
                EvaluationExperimentVariant.id == candidate_variant_id,
                EvaluationExperimentVariant.experiment_id == run.experiment_id,
            )
        )
        if baseline is None or candidate is None or baseline.id == candidate.id:
            raise AgentHubError(
                "EVALUATION_COMPARISON_NOT_COMPARABLE",
                "Comparison variants must belong to the same experiment and differ.",
                422,
            )
        await self.compute_run_metrics(session, context=context, run_id=run_id)
        metric_rows = list(
            await session.scalars(
                select(EvaluationMetricResult).where(
                    EvaluationMetricResult.workspace_id == workspace_id,
                    EvaluationMetricResult.experiment_run_id == run_id,
                    EvaluationMetricResult.dimension == "VARIANT",
                    EvaluationMetricResult.category == "ALL",
                    EvaluationMetricResult.experiment_variant_id.in_(
                        [
                            baseline_variant_id,
                            candidate_variant_id,
                        ]
                    ),
                )
            )
        )
        by_variant: dict[UUID, dict[str, EvaluationMetricResult]] = defaultdict(dict)
        for row in metric_rows:
            by_variant[row.experiment_variant_id][row.metric_name] = row
        names = sorted(
            set(by_variant[baseline_variant_id]).intersection(by_variant[candidate_variant_id])
        )
        metrics: dict[str, Any] = {}
        for name in names:
            before = _metric_from_row(by_variant[baseline_variant_id][name])
            after = _metric_from_row(by_variant[candidate_variant_id][name])
            metrics[name] = compare_metric_values(
                before,
                after,
                direction=metric_direction(name),
                paired=await self._paired_metric_values(
                    session,
                    workspace_id,
                    run_id,
                    baseline_variant_id,
                    candidate_variant_id,
                    name,
                ),
            ).to_dict()
        case_pairs = await self._pair_count(
            session,
            workspace_id,
            run_id,
            baseline_variant_id,
            candidate_variant_id,
        )
        comparison_status = (
            "COMPLETE"
            if case_pairs[0] > 0 and case_pairs[1] == 0
            else "INCOMPLETE"
            if case_pairs[0] > 0
            else "NOT_COMPARABLE"
        )
        comparison = await session.scalar(
            select(EvaluationExperimentComparison).where(
                EvaluationExperimentComparison.workspace_id == workspace_id,
                EvaluationExperimentComparison.experiment_run_id == run_id,
                EvaluationExperimentComparison.baseline_variant_id == baseline_variant_id,
                EvaluationExperimentComparison.candidate_variant_id == candidate_variant_id,
            )
        )
        if comparison is None:
            comparison = EvaluationExperimentComparison(
                workspace_id=workspace_id,
                experiment_run_id=run_id,
                baseline_variant_id=baseline_variant_id,
                candidate_variant_id=candidate_variant_id,
            )
            session.add(comparison)
        comparison.status = comparison_status
        comparison.evaluator_versions = {name: "v1" for name in names}
        comparison.metrics = metrics
        comparison.missing_pairs = case_pairs[1]
        await session.commit()
        return comparison

    @staticmethod
    def _variant_metrics(rows: Sequence[tuple[Any, Any, Any]]) -> dict[str, dict[str, Any]]:
        output = EvaluationMetricsService._observed_metrics(rows)
        by_category: dict[str, list[tuple[Any, Any, Any]]] = defaultdict(list)
        for row in rows:
            by_category[str(row[1].category)].append(row)
        output["categories"] = {
            category: EvaluationMetricsService._observed_metrics(category_rows)
            for category, category_rows in sorted(by_category.items())
        }
        return output

    @staticmethod
    def _observed_metrics(rows: Sequence[tuple[Any, Any, Any]]) -> dict[str, Any]:
        registry = EvaluatorRegistry()
        by_metric: dict[str, list[Any]] = defaultdict(list)
        latencies: list[float] = []
        usage = {"input_tokens": [], "output_tokens": [], "total_tokens": [], "cached_tokens": []}
        costs: dict[str, list[Decimal]] = defaultdict(list)
        repetition_latencies: dict[UUID, list[float]] = defaultdict(list)
        unexpected_failures = 0
        loop_guard_observations: list[bool] = []
        for case, item, _variant in rows:
            evaluated = evaluate_case(
                item.category, item.expected, case.observation, registry=registry
            )
            for name, metric in evaluated.items():
                by_metric[name].append(metric)
            if case.latency_ms is not None:
                latencies.append(float(case.latency_ms))
                repetition_latencies[case.dataset_item_id].append(float(case.latency_ms))
            for field, values in usage.items():
                value = getattr(case, field)
                if value is not None:
                    values.append(value)
            if case.cost_amount is not None and case.cost_currency:
                costs[case.cost_currency].append(Decimal(case.cost_amount))
            if case.status == "FAILED":
                unexpected_failures += 1
            if "loop_guard_triggered" in case.observation:
                loop_guard_observations.append(bool(case.observation["loop_guard_triggered"]))
        output = {
            name: aggregate_metric_values(name, values).to_dict()
            for name, values in by_metric.items()
        }
        output["latency_p50_ms"] = _scalar_metric(
            "latency_p50_ms", percentile(latencies, 0.5), len(latencies)
        )
        output["latency_p95_ms"] = _scalar_metric(
            "latency_p95_ms", percentile(latencies, 0.95), len(latencies)
        )
        for name, values in usage.items():
            output[name] = _scalar_metric(name, sum(values) if values else None, len(values))
            output[f"known_{name}_count"] = _scalar_metric(
                f"known_{name}_count", len(values), len(rows)
            )
        output["unknown_usage_count"] = _scalar_metric(
            "unknown_usage_count", len(rows) - len(usage["total_tokens"]), len(rows)
        )
        output["unexpected_failure_rate"] = _rate_scalar(
            "unexpected_failure_rate", unexpected_failures, len(rows)
        )
        output["loop_rate"] = _rate_scalar(
            "loop_rate", sum(loop_guard_observations), len(loop_guard_observations)
        )
        repetition_means = [mean(values) for values in repetition_latencies.values() if values]
        output["repetition_latency_mean_ms"] = _scalar_metric(
            "repetition_latency_mean_ms",
            mean(repetition_means) if repetition_means else None,
            len(repetition_means),
        )
        output["repetition_latency_stddev_ms"] = _scalar_metric(
            "repetition_latency_stddev_ms",
            pstdev(repetition_means) if len(repetition_means) > 1 else 0.0
            if repetition_means
            else None,
            len(repetition_means),
        )
        cost_by_currency: dict[str, dict[str, Any]] = {}
        task_success = output.get("task_success")
        success_count = (
            int(task_success["numerator"])
            if task_success and task_success.get("numerator") is not None
            else 0
        )
        for currency, amounts in sorted(costs.items()):
            total = sum(amounts, Decimal("0"))
            cost_by_currency[currency] = _scalar_metric(
                f"total_cost_{currency}", total, len(amounts)
            )
            output[f"total_cost_{currency}"] = cost_by_currency[currency]
            output[f"cost_per_successful_case_{currency}"] = _scalar_metric(
                f"cost_per_successful_case_{currency}",
                total / success_count if success_count else None,
                success_count,
            )
        output["cost_by_currency"] = cost_by_currency
        return output

    @staticmethod
    async def _pair_count(
        session: AsyncSession,
        workspace_id: UUID,
        run_id: UUID,
        baseline_variant_id: UUID,
        candidate_variant_id: UUID,
    ) -> tuple[int, int]:
        rows = list(
            await session.scalars(
                select(EvaluationExperimentCaseResult).where(
                    EvaluationExperimentCaseResult.workspace_id == workspace_id,
                    EvaluationExperimentCaseResult.experiment_run_id == run_id,
                    EvaluationExperimentCaseResult.experiment_variant_id.in_(
                        [
                            baseline_variant_id,
                            candidate_variant_id,
                        ]
                    ),
                )
            )
        )
        keys: dict[tuple[UUID, int], set[UUID]] = defaultdict(set)
        for row in rows:
            keys[(row.dataset_item_id, row.repetition_index)].add(row.experiment_variant_id)
        complete = sum(len(variants) == 2 for variants in keys.values())
        return complete, sum(len(variants) != 2 for variants in keys.values())

    @staticmethod
    async def _paired_metric_values(
        session: AsyncSession,
        workspace_id: UUID,
        run_id: UUID,
        baseline_variant_id: UUID,
        candidate_variant_id: UUID,
        metric_name: str,
    ) -> list[tuple[float | None, float | None]]:
        rows = list(
            await session.execute(
                select(
                    EvaluationExperimentCaseResult,
                    EvaluationDatasetItem,
                )
                .join(
                    EvaluationDatasetItem,
                    EvaluationDatasetItem.id == EvaluationExperimentCaseResult.dataset_item_id,
                )
                .where(
                    EvaluationExperimentCaseResult.workspace_id == workspace_id,
                    EvaluationExperimentCaseResult.experiment_run_id == run_id,
                    EvaluationExperimentCaseResult.experiment_variant_id.in_(
                        [baseline_variant_id, candidate_variant_id]
                    ),
                )
            )
        )
        paired: dict[tuple[UUID, int], dict[UUID, float | None]] = defaultdict(dict)
        for case, item in rows:
            metrics = evaluate_case(item.category, item.expected, case.observation)
            metric = metrics.get(metric_name)
            paired[(case.dataset_item_id, case.repetition_index)][case.experiment_variant_id] = (
                float(metric.value)
                if metric is not None
                and metric.status.value == "AVAILABLE"
                and metric.value is not None
                else None
            )
        return [
            (values.get(baseline_variant_id), values.get(candidate_variant_id))
            for values in paired.values()
            if baseline_variant_id in values and candidate_variant_id in values
        ]

    @staticmethod
    def _require_read(context: WorkspaceExecutionContext) -> None:
        if (
            EVALUATION_READ not in context.permissions
            and "workspace_read" not in context.permissions
        ):
            raise AgentHubError("FORBIDDEN", "You do not have permission.", 403)

    @staticmethod
    def _workspace_id(context: WorkspaceExecutionContext) -> UUID:
        try:
            return UUID(context.workspace_id)
        except ValueError:
            raise AgentHubError("INVALID_WORKSPACE", "Workspace context is invalid.", 500) from None

    @staticmethod
    def _not_found(code: str) -> None:
        raise AgentHubError(code, "The evaluation run was not found.", 404)

    @staticmethod
    def _integrity_error() -> None:
        raise AgentHubError(
            "EXPERIMENT_REPRODUCIBILITY_VIOLATION",
            "The experiment definition is invalid.",
            409,
        )


def _metric_from_row(row: EvaluationMetricResult):
    from packages.evaluation.metrics import MetricStatus, MetricValue

    return MetricValue(
        row.metric_name,
        MetricStatus(row.status),
        float(row.value) if row.value is not None else None,
        row.sample_count,
        float(row.numerator) if row.numerator is not None else None,
        float(row.denominator) if row.denominator is not None else None,
        row.reason,
        row.evaluator_version,
    )


def _scalar_metric(name: str, value: Any, sample_count: int) -> dict[str, Any]:
    if value is None:
        return {
            "name": name,
            "status": "NOT_AVAILABLE",
            "value": None,
            "sample_count": 0,
            "reason": "no_available_samples",
            "evaluator_version": "v1",
        }
    return {
        "name": name,
        "status": "AVAILABLE",
        "value": str(value) if isinstance(value, Decimal) else value,
        "sample_count": sample_count,
        "evaluator_version": "v1",
    }


def _rate_scalar(name: str, numerator: int, denominator: int) -> dict[str, Any]:
    if denominator == 0:
        return {
            "name": name,
            "status": "NOT_APPLICABLE",
            "value": None,
            "sample_count": 0,
            "reason": "zero_denominator",
            "evaluator_version": "v1",
        }
    return {
        "name": name,
        "status": "AVAILABLE",
        "value": numerator / denominator,
        "sample_count": denominator,
        "numerator": numerator,
        "denominator": denominator,
        "evaluator_version": "v1",
    }


def _metric_items(metrics: dict[str, Any]):
    return (
        (name, value)
        for name, value in metrics.items()
        if isinstance(value, dict) and "status" in value
    )


def _decimal_or_none(value: Any) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None


__all__ = ["EvaluationMetricsService"]
