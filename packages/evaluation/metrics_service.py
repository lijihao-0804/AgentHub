"""Immutable M7-D metric snapshots and paired comparisons."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from decimal import Decimal
from statistics import mean, pstdev
from typing import Any
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.control_plane.rbac import EVALUATION_READ, EVALUATION_RUN
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.evaluation.metrics import (
    EvaluatorRegistry,
    MetricAggregationKind,
    MetricDirection,
    MetricStatus,
    MetricValue,
    aggregate_metric_values,
    compare_metric_values,
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
    EvaluationMetricSnapshot,
)

_TERMINAL_RUNS = {"SUCCEEDED", "FAILED", "CANCELLED"}
_TERMINAL_CASES = {"SUCCEEDED", "FAILED", "CANCELLED"}


class EvaluationMetricsService:
    def __init__(self, registry: EvaluatorRegistry | None = None) -> None:
        self.registry = registry or EvaluatorRegistry()

    async def materialize_metrics(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
        run_id: UUID,
    ) -> dict[str, Any]:
        self._require_mutation(context)
        workspace_id = self._workspace_id(context)
        run = await self._load_run(session, workspace_id, run_id)
        existing = await session.scalar(
            select(EvaluationMetricSnapshot)
            .where(
                EvaluationMetricSnapshot.workspace_id == workspace_id,
                EvaluationMetricSnapshot.experiment_run_id == run_id,
            )
            .with_for_update()
        )
        if existing is not None:
            return await self._read_snapshot(session, existing)

        experiment = await self._load_experiment(session, workspace_id, run.experiment_id)
        self._validate_manifest(experiment)
        # The judge identity frozen with the experiment decides the evaluator version of
        # every answer_quality row written below.
        self.registry.bind_judge_manifest(experiment.evaluator_manifest)
        rows = await self._load_and_validate_case_set(session, workspace_id, run, experiment)
        output = self._metrics_output(run_id, rows)
        metric_records = self._metric_records(workspace_id, run_id, output)
        for record in metric_records:
            session.add(record)
        await session.flush()

        snapshot = EvaluationMetricSnapshot(
            workspace_id=workspace_id,
            experiment_run_id=run_id,
            evaluator_manifest=experiment.evaluator_manifest,
            evaluator_manifest_hash=canonical_json_hash(experiment.evaluator_manifest),
            case_result_set_hash=self._case_result_set_hash(rows),
            metrics_hash=self._metric_rows_hash(metric_records),
            created_by=self._user_id(context),
        )
        session.add(snapshot)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            await session.refresh(snapshot)
            existing = await session.scalar(
                select(EvaluationMetricSnapshot).where(
                    EvaluationMetricSnapshot.workspace_id == workspace_id,
                    EvaluationMetricSnapshot.experiment_run_id == run_id,
                )
            )
            if existing is None:
                raise
            return await self._read_snapshot(session, existing)
        return self._payload(snapshot, output)

    async def get_persisted_metrics(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
        run_id: UUID,
    ) -> dict[str, Any]:
        self._require_read(context)
        workspace_id = self._workspace_id(context)
        await self._load_run(session, workspace_id, run_id)
        snapshot = await session.scalar(
            select(EvaluationMetricSnapshot).where(
                EvaluationMetricSnapshot.workspace_id == workspace_id,
                EvaluationMetricSnapshot.experiment_run_id == run_id,
            )
        )
        if snapshot is None:
            raise AgentHubError(
                "EVALUATION_METRICS_NOT_MATERIALIZED",
                "Metrics have not been materialized for this experiment run.",
                404,
            )
        return await self._read_snapshot(session, snapshot)

    async def compute_run_metrics(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
        run_id: UUID,
    ) -> dict[str, Any]:
        return await self.get_persisted_metrics(session, context=context, run_id=run_id)

    async def create_comparison(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
        run_id: UUID,
        baseline_variant_id: UUID,
        candidate_variant_id: UUID,
    ) -> EvaluationExperimentComparison:
        self._require_mutation(context)
        workspace_id = self._workspace_id(context)
        run = await self._load_run(session, workspace_id, run_id)
        snapshot = await session.scalar(
            select(EvaluationMetricSnapshot).where(
                EvaluationMetricSnapshot.workspace_id == workspace_id,
                EvaluationMetricSnapshot.experiment_run_id == run_id,
            )
        )
        if snapshot is None:
            raise AgentHubError(
                "EVALUATION_METRICS_NOT_MATERIALIZED",
                "Materialize metrics before creating a comparison.",
                409,
            )
        await self._read_snapshot(session, snapshot)
        baseline, candidate = await self._load_comparison_variants(
            session, workspace_id, run, baseline_variant_id, candidate_variant_id
        )
        existing = await session.scalar(
            select(EvaluationExperimentComparison).where(
                EvaluationExperimentComparison.workspace_id == workspace_id,
                EvaluationExperimentComparison.experiment_run_id == run_id,
                EvaluationExperimentComparison.baseline_variant_id == baseline_variant_id,
                EvaluationExperimentComparison.candidate_variant_id == candidate_variant_id,
            )
        )
        if existing is not None:
            self._verify_comparison_hash(existing)
            self._verify_comparison_binding(existing, snapshot)
            self._verify_comparison_variants(existing, baseline, candidate, snapshot)
            return existing

        metric_rows = list(
            await session.scalars(
                select(EvaluationMetricResult).where(
                    EvaluationMetricResult.workspace_id == workspace_id,
                    EvaluationMetricResult.experiment_run_id == run_id,
                    EvaluationMetricResult.dimension == "VARIANT",
                    EvaluationMetricResult.category == "ALL",
                    EvaluationMetricResult.experiment_variant_id.in_(
                        [baseline_variant_id, candidate_variant_id]
                    ),
                )
            )
        )
        by_variant: dict[UUID, dict[str, EvaluationMetricResult]] = defaultdict(dict)
        for row in metric_rows:
            by_variant[row.experiment_variant_id][row.metric_name] = row
            self._register_persisted_definition(row)
        names = sorted(
            set(by_variant[baseline_variant_id]).intersection(by_variant[candidate_variant_id])
        )
        metrics: dict[str, Any] = {}
        evaluator_mismatch = False
        for name in names:
            before_row = by_variant[baseline_variant_id][name]
            after_row = by_variant[candidate_variant_id][name]
            before = _metric_from_row(before_row)
            after = _metric_from_row(after_row)
            if before_row.evaluator_version != after_row.evaluator_version:
                evaluator_mismatch = True
                metrics[name] = _not_comparable_metric(before, after, "evaluator_version_mismatch")
                continue
            if _currency(before_row) != _currency(after_row):
                metrics[name] = _not_comparable_metric(before, after, "currency_mismatch")
                continue
            metrics[name] = compare_metric_values(
                before,
                after,
                direction=self.registry.direction_for(name),
                paired=await self._paired_metric_values(
                    session,
                    workspace_id,
                    run_id,
                    baseline_variant_id,
                    candidate_variant_id,
                    name,
                ),
            ).to_dict()

        complete_pairs, incomplete_pairs = await self._pair_count(
            session, workspace_id, run, baseline_variant_id, candidate_variant_id
        )
        status = (
            "NOT_COMPARABLE"
            if evaluator_mismatch or complete_pairs == 0
            else "INCOMPLETE"
            if incomplete_pairs
            else "COMPLETE"
        )
        evaluator_versions = {
            name: by_variant[baseline_variant_id][name].evaluator_version for name in names
        }
        comparison = EvaluationExperimentComparison(
            workspace_id=workspace_id,
            experiment_run_id=run_id,
            baseline_variant_id=baseline_variant_id,
            candidate_variant_id=candidate_variant_id,
            metric_snapshot_id=snapshot.id,
            metric_snapshot_hash=snapshot.metrics_hash,
            baseline_variant_hash=baseline.variant_hash,
            candidate_variant_hash=candidate.variant_hash,
            evaluator_manifest_hash=snapshot.evaluator_manifest_hash,
            status=status,
            evaluator_versions=evaluator_versions,
            metrics=metrics,
            missing_pairs=incomplete_pairs,
            paired_pairs=complete_pairs,
            created_by=self._user_id(context),
        )
        comparison.comparison_hash = canonical_json_hash(
            self._comparison_hash_input(
                run_id,
                snapshot.metrics_hash,
                baseline.variant_hash,
                candidate.variant_hash,
                snapshot.evaluator_manifest_hash,
                evaluator_versions,
                metrics,
                complete_pairs,
                incomplete_pairs,
                status,
            )
        )
        session.add(comparison)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            await session.refresh(snapshot)
            await session.refresh(baseline)
            await session.refresh(candidate)
            existing = await session.scalar(
                select(EvaluationExperimentComparison).where(
                    EvaluationExperimentComparison.workspace_id == workspace_id,
                    EvaluationExperimentComparison.experiment_run_id == run_id,
                    EvaluationExperimentComparison.baseline_variant_id == baseline_variant_id,
                    EvaluationExperimentComparison.candidate_variant_id == candidate_variant_id,
                )
            )
            if existing is None:
                raise
            self._verify_comparison_hash(existing)
            self._verify_comparison_binding(existing, snapshot)
            self._verify_comparison_variants(existing, baseline, candidate, snapshot)
            return existing
        return comparison

    async def get_comparisons(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
        run_id: UUID,
    ) -> list[EvaluationExperimentComparison]:
        self._require_read(context)
        workspace_id = self._workspace_id(context)
        await self._load_run(session, workspace_id, run_id)
        snapshot = await session.scalar(
            select(EvaluationMetricSnapshot).where(
                EvaluationMetricSnapshot.workspace_id == workspace_id,
                EvaluationMetricSnapshot.experiment_run_id == run_id,
            )
        )
        if snapshot is None:
            raise AgentHubError(
                "EVALUATION_COMPARISON_NOT_FOUND",
                "No comparison artifact exists for this run.",
                404,
            )
        await self._read_snapshot(session, snapshot)
        comparisons = list(
            await session.scalars(
                select(EvaluationExperimentComparison)
                .where(
                    EvaluationExperimentComparison.workspace_id == workspace_id,
                    EvaluationExperimentComparison.experiment_run_id == run_id,
                )
                .order_by(EvaluationExperimentComparison.created_at)
            )
        )
        for comparison in comparisons:
            self._verify_comparison_hash(comparison)
            self._verify_comparison_binding(comparison, snapshot)
        return comparisons

    async def get_comparison(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
        run_id: UUID,
        comparison_id: UUID,
    ) -> EvaluationExperimentComparison:
        self._require_read(context)
        workspace_id = self._workspace_id(context)
        snapshot = await session.scalar(
            select(EvaluationMetricSnapshot).where(
                EvaluationMetricSnapshot.workspace_id == workspace_id,
                EvaluationMetricSnapshot.experiment_run_id == run_id,
            )
        )
        if snapshot is None:
            raise AgentHubError(
                "EVALUATION_COMPARISON_NOT_FOUND",
                "No comparison artifact exists for this run.",
                404,
            )
        await self._read_snapshot(session, snapshot)
        comparison = await session.scalar(
            select(EvaluationExperimentComparison).where(
                EvaluationExperimentComparison.workspace_id == workspace_id,
                EvaluationExperimentComparison.experiment_run_id == run_id,
                EvaluationExperimentComparison.id == comparison_id,
            )
        )
        if comparison is None:
            self._not_found("EVALUATION_COMPARISON_NOT_FOUND")
        self._verify_comparison_hash(comparison)
        self._verify_comparison_binding(comparison, snapshot)
        return comparison

    async def _read_snapshot(
        self, session: AsyncSession, snapshot: EvaluationMetricSnapshot
    ) -> dict[str, Any]:
        rows = list(
            await session.scalars(
                select(EvaluationMetricResult)
                .where(
                    EvaluationMetricResult.workspace_id == snapshot.workspace_id,
                    EvaluationMetricResult.experiment_run_id == snapshot.experiment_run_id,
                )
                .order_by(
                    EvaluationMetricResult.dimension,
                    EvaluationMetricResult.category,
                    EvaluationMetricResult.metric_name,
                )
            )
        )
        if canonical_json_hash(snapshot.evaluator_manifest) != snapshot.evaluator_manifest_hash:
            self._integrity_error("EVALUATION_METRICS_INTEGRITY_ERROR")
        self.registry.bind_judge_manifest(snapshot.evaluator_manifest)
        run_rows = await self._case_rows(session, snapshot.workspace_id, snapshot.experiment_run_id)
        if self._case_result_set_hash(run_rows) != snapshot.case_result_set_hash:
            self._integrity_error("EVALUATION_METRICS_INTEGRITY_ERROR")
        if self._metric_rows_hash(rows) != snapshot.metrics_hash:
            self._integrity_error("EVALUATION_METRICS_INTEGRITY_ERROR")
        return self._payload(snapshot, _output_from_metric_rows(snapshot.experiment_run_id, rows))

    async def _load_and_validate_case_set(
        self,
        session: AsyncSession,
        workspace_id: UUID,
        run: EvaluationExperimentRun,
        experiment: EvaluationExperiment,
    ) -> list[tuple[Any, Any, Any]]:
        variants = list(
            await session.scalars(
                select(EvaluationExperimentVariant).where(
                    EvaluationExperimentVariant.workspace_id == workspace_id,
                    EvaluationExperimentVariant.experiment_id == experiment.id,
                )
            )
        )
        items = list(
            await session.scalars(
                select(EvaluationDatasetItem).where(
                    EvaluationDatasetItem.workspace_id == workspace_id,
                    EvaluationDatasetItem.dataset_version_id == experiment.dataset_version_id,
                    EvaluationDatasetItem.split == experiment.split,
                )
            )
        )
        expected = {
            (variant.id, item.id, repetition)
            for variant in variants
            for item in items
            for repetition in range(run.repetitions)
        }
        rows = await self._case_rows(session, workspace_id, run.id)
        if any(case.status not in _TERMINAL_CASES for case, _, _ in rows):
            raise AgentHubError(
                "EVALUATION_METRICS_RUN_NOT_TERMINAL",
                "All case results must be terminal before metrics materialization.",
                409,
            )
        actual = {
            (case.experiment_variant_id, case.dataset_item_id, case.repetition_index)
            for case, _, _ in rows
        }
        if actual != expected or len(rows) != len(actual):
            raise AgentHubError(
                "EVALUATION_CASE_SET_INTEGRITY_ERROR",
                "Persisted case results do not match the frozen execution plan.",
                409,
            )
        return rows

    @staticmethod
    async def _case_rows(
        session: AsyncSession, workspace_id: UUID, run_id: UUID
    ) -> list[tuple[Any, Any, Any]]:
        result = await session.execute(
            select(
                EvaluationExperimentCaseResult,
                EvaluationDatasetItem,
                EvaluationExperimentVariant,
            )
            .join(
                EvaluationDatasetItem,
                and_(
                    EvaluationDatasetItem.id == EvaluationExperimentCaseResult.dataset_item_id,
                    EvaluationDatasetItem.workspace_id
                    == EvaluationExperimentCaseResult.workspace_id,
                ),
            )
            .join(
                EvaluationExperimentVariant,
                and_(
                    EvaluationExperimentVariant.id
                    == EvaluationExperimentCaseResult.experiment_variant_id,
                    EvaluationExperimentVariant.workspace_id
                    == EvaluationExperimentCaseResult.workspace_id,
                ),
            )
            .where(
                EvaluationExperimentCaseResult.workspace_id == workspace_id,
                EvaluationExperimentCaseResult.experiment_run_id == run_id,
            )
            .order_by(
                EvaluationExperimentCaseResult.dataset_item_id,
                EvaluationExperimentCaseResult.experiment_variant_id,
                EvaluationExperimentCaseResult.repetition_index,
            )
        )
        return list(result)

    @staticmethod
    async def _load_run(
        session: AsyncSession, workspace_id: UUID, run_id: UUID
    ) -> EvaluationExperimentRun:
        run = await session.scalar(
            select(EvaluationExperimentRun).where(
                EvaluationExperimentRun.workspace_id == workspace_id,
                EvaluationExperimentRun.id == run_id,
            )
        )
        if run is None:
            raise AgentHubError(
                "EVALUATION_EXPERIMENT_RUN_NOT_FOUND", "The experiment run was not found.", 404
            )
        if run.status not in _TERMINAL_RUNS:
            raise AgentHubError(
                "EVALUATION_METRICS_RUN_NOT_TERMINAL",
                "Metrics require a terminal experiment run.",
                409,
            )
        return run

    @staticmethod
    async def _load_experiment(
        session: AsyncSession, workspace_id: UUID, experiment_id: UUID
    ) -> EvaluationExperiment:
        experiment = await session.scalar(
            select(EvaluationExperiment).where(
                EvaluationExperiment.workspace_id == workspace_id,
                EvaluationExperiment.id == experiment_id,
            )
        )
        if experiment is None or not experiment.spec_json:
            raise AgentHubError(
                "EXPERIMENT_REPRODUCIBILITY_VIOLATION",
                "The experiment definition is invalid.",
                409,
            )
        return experiment

    def _validate_manifest(self, experiment: EvaluationExperiment) -> None:
        try:
            self.registry.validate_manifest(experiment.evaluator_manifest)
        except ValueError as exc:
            raise AgentHubError(
                "EXPERIMENT_EVALUATOR_VERSION_MISMATCH",
                "The persisted evaluator manifest is not compatible.",
                409,
            ) from exc

    @staticmethod
    async def _load_comparison_variants(
        session: AsyncSession,
        workspace_id: UUID,
        run: EvaluationExperimentRun,
        baseline_id: UUID,
        candidate_id: UUID,
    ) -> tuple[EvaluationExperimentVariant, EvaluationExperimentVariant]:
        variants = list(
            await session.scalars(
                select(EvaluationExperimentVariant).where(
                    EvaluationExperimentVariant.workspace_id == workspace_id,
                    EvaluationExperimentVariant.experiment_id == run.experiment_id,
                    EvaluationExperimentVariant.id.in_([baseline_id, candidate_id]),
                )
            )
        )
        by_id = {variant.id: variant for variant in variants}
        if baseline_id == candidate_id or baseline_id not in by_id or candidate_id not in by_id:
            raise AgentHubError(
                "EVALUATION_COMPARISON_NOT_COMPARABLE",
                "Comparison variants must belong to the same experiment and differ.",
                422,
            )
        return by_id[baseline_id], by_id[candidate_id]

    def _metrics_output(self, run_id: UUID, rows: Sequence[tuple[Any, Any, Any]]) -> dict[str, Any]:
        by_variant: dict[UUID, list[tuple[Any, Any, Any]]] = defaultdict(list)
        for row in rows:
            by_variant[row[2].id].append(row)
        return {
            "run_id": str(run_id),
            "variants": {
                str(variant_id): self._variant_metrics(variant_rows)
                for variant_id, variant_rows in sorted(
                    by_variant.items(), key=lambda item: str(item[0])
                )
            },
            "overall": self._metrics_for_rows(rows, include_categories=True, scope_by_variant=True),
        }

    def _variant_metrics(self, rows: Sequence[tuple[Any, Any, Any]]) -> dict[str, Any]:
        return self._metrics_for_rows(rows, include_categories=True, scope_by_variant=False)

    def _metrics_for_rows(
        self,
        rows: Sequence[tuple[Any, Any, Any]],
        *,
        include_categories: bool,
        scope_by_variant: bool,
    ) -> dict[str, Any]:
        by_item_metric: dict[tuple[Any, str], list[MetricValue]] = defaultdict(list)
        by_category: dict[str, list[tuple[Any, Any, Any]]] = defaultdict(list)
        latencies: dict[Any, list[float]] = defaultdict(list)
        usage = {"input_tokens": [], "output_tokens": [], "total_tokens": [], "cached_tokens": []}
        costs: dict[str, list[Decimal]] = defaultdict(list)
        successful_costs: dict[str, list[Decimal]] = defaultdict(list)
        item_costs: dict[tuple[Any, str], list[Decimal]] = defaultdict(list)
        unexpected_failures = 0
        loop_guard_observations: list[bool] = []
        for case, item, variant in rows:
            evaluated = self.registry.evaluate(item.category, item.expected, case.observation)
            item_key = (variant.id, item.id) if scope_by_variant else item.id
            for name, metric in evaluated.items():
                by_item_metric[(item_key, name)].append(metric)
            if include_categories:
                by_category[str(item.category)].append((case, item, variant))
            if case.latency_ms is not None:
                latencies[item_key].append(float(case.latency_ms))
            for field, values in usage.items():
                value = getattr(case, field)
                if value is not None:
                    values.append(value)
            if case.cost_amount is not None and case.cost_currency:
                amount = Decimal(case.cost_amount)
                costs[case.cost_currency].append(amount)
                item_costs[(item_key, case.cost_currency)].append(amount)
                task = evaluated.get("task_success")
                if task is not None and task.status == MetricStatus.AVAILABLE and task.value == 1:
                    successful_costs[case.cost_currency].append(amount)
            if case.status == "FAILED":
                unexpected_failures += 1
            if "loop_guard_triggered" in case.observation:
                loop_guard_observations.append(bool(case.observation["loop_guard_triggered"]))

        by_metric: dict[str, list[MetricValue]] = defaultdict(list)
        for (_item_key, name), values in by_item_metric.items():
            by_metric[name].append(_aggregate_repetitions(name, values))
        output = {
            name: aggregate_metric_values(
                name, values, version=self.registry.version_for_metric(name)
            ).to_dict()
            for name, values in sorted(by_metric.items())
        }
        item_latency = [mean(values) for values in latencies.values() if values]
        output["latency_p50_ms"] = _scalar_metric(
            "latency_p50_ms",
            percentile(item_latency, 0.5),
            len(item_latency),
            self.registry.version_for_metric("latency_p50_ms"),
            details={"dataset_item_count": len(item_latency)},
        )
        output["latency_p95_ms"] = _scalar_metric(
            "latency_p95_ms",
            percentile(item_latency, 0.95),
            len(item_latency),
            self.registry.version_for_metric("latency_p95_ms"),
            details={"dataset_item_count": len(item_latency)},
        )
        for name, values in usage.items():
            known_name = f"known_{name}_count"
            self.registry.register_metric_definition(
                known_name,
                MetricDirection.HIGHER_IS_BETTER,
                MetricAggregationKind.SCALAR,
            )
            output[name] = _scalar_metric(
                name,
                sum(values) if values else None,
                len(latencies),
                self.registry.version_for_metric(name),
                details={"execution_count": len(values), "dataset_item_count": len(latencies)},
            )
            output[known_name] = _scalar_metric(
                known_name,
                len(values),
                len(latencies),
                "v1",
                details={"execution_count": len(values), "dataset_item_count": len(latencies)},
            )
        output["unknown_usage_count"] = _scalar_metric(
            "unknown_usage_count",
            len(rows) - len(usage["total_tokens"]),
            len(latencies),
            self.registry.version_for_metric("unknown_usage_count"),
            details={"execution_count": len(rows), "dataset_item_count": len(latencies)},
        )
        output["unexpected_failure_rate"] = _rate_scalar(
            "unexpected_failure_rate",
            unexpected_failures,
            len(rows),
            self.registry.version_for_metric("unexpected_failure_rate"),
        )
        output["loop_rate"] = _rate_scalar(
            "loop_rate",
            sum(loop_guard_observations),
            len(loop_guard_observations),
            self.registry.version_for_metric("loop_rate"),
        )
        output["repetition_latency_mean_ms"] = _scalar_metric(
            "repetition_latency_mean_ms",
            mean(item_latency) if item_latency else None,
            len(item_latency),
            self.registry.version_for_metric("repetition_latency_mean_ms"),
        )
        output["repetition_latency_stddev_ms"] = _scalar_metric(
            "repetition_latency_stddev_ms",
            pstdev(item_latency) if len(item_latency) > 1 else 0.0 if item_latency else None,
            len(item_latency),
            self.registry.version_for_metric("repetition_latency_stddev_ms"),
        )
        successful_dataset_item_costs: dict[str, list[Decimal]] = defaultdict(list)
        for (item_key, currency), amounts in item_costs.items():
            item_task_success = _aggregate_repetitions(
                "task_success", by_item_metric.get((item_key, "task_success"), [])
            )
            if (
                item_task_success.status == MetricStatus.AVAILABLE
                and item_task_success.value == 1
            ):
                successful_dataset_item_costs[currency].append(
                    sum(amounts, Decimal("0")) / len(amounts)
                )
        cost_groups: dict[str, dict[str, Any]] = {}
        for currency, amounts in sorted(costs.items()):
            for name in (
                f"total_cost_{currency}",
                f"cost_per_successful_case_{currency}",
                f"cost_per_successful_execution_{currency}",
                f"cost_per_successful_dataset_item_{currency}",
            ):
                self.registry.register_metric_definition(
                    name, MetricDirection.LOWER_IS_BETTER, MetricAggregationKind.COST
                )
            total = sum(amounts, Decimal("0"))
            success_amounts = successful_costs[currency]
            success_total = sum(success_amounts, Decimal("0"))
            successful_item_costs = successful_dataset_item_costs[currency]
            successful_item_total = sum(successful_item_costs, Decimal("0"))
            details = {
                "currency": currency,
                "execution_count": len(amounts),
                "unit": "successful case execution",
            }
            cost_groups[currency] = _scalar_metric(
                f"total_cost_{currency}", total, len(amounts), "v1", details=details
            )
            output[f"total_cost_{currency}"] = cost_groups[currency]
            output[f"cost_per_successful_case_{currency}"] = _scalar_metric(
                f"cost_per_successful_case_{currency}",
                success_total / len(success_amounts) if success_amounts else None,
                len(success_amounts),
                "v1",
                details={
                    **details,
                    "numerator": str(success_total),
                    "denominator": len(success_amounts),
                    "deprecated": True,
                    "alias_of": f"cost_per_successful_execution_{currency}",
                },
            )
            output[f"cost_per_successful_execution_{currency}"] = _scalar_metric(
                f"cost_per_successful_execution_{currency}",
                success_total / len(success_amounts) if success_amounts else None,
                len(success_amounts),
                "v1",
                details={
                    **details,
                    "numerator": str(success_total),
                    "denominator": len(success_amounts),
                },
            )
            output[f"cost_per_successful_dataset_item_{currency}"] = _scalar_metric(
                f"cost_per_successful_dataset_item_{currency}",
                successful_item_total / len(successful_item_costs)
                if successful_item_costs
                else None,
                len(successful_item_costs),
                "v1",
                details={
                    "currency": currency,
                    "unit": "successful dataset item",
                    "numerator": str(successful_item_total),
                    "denominator": len(successful_item_costs),
                    "repetition_aggregation": "mean_known_repetition_costs",
                },
            )
        if len(costs) == 1:
            currency = next(iter(costs))
            for generic_name in ("cost_per_successful_case", "cost_per_successful_execution"):
                currency_metric = output[f"{generic_name}_{currency}"]
                generic_details = {
                    **currency_metric.get("details", {}),
                    "currency": currency,
                }
                if generic_name == "cost_per_successful_case":
                    generic_details["alias_of"] = "cost_per_successful_execution"
                output[generic_name] = {
                    **currency_metric,
                    "name": generic_name,
                    "details": generic_details,
                }
            currency_metric = output[f"cost_per_successful_dataset_item_{currency}"]
            output["cost_per_successful_dataset_item"] = {
                **currency_metric,
                "name": "cost_per_successful_dataset_item",
                "details": {
                    **currency_metric.get("details", {}),
                    "currency": currency,
                },
            }
        elif len(costs) > 1:
            for generic_name in (
                "cost_per_successful_case",
                "cost_per_successful_execution",
                "cost_per_successful_dataset_item",
            ):
                output[generic_name] = {
                    "name": generic_name,
                    "status": MetricStatus.NOT_AVAILABLE.value,
                    "value": None,
                    "sample_count": 0,
                    "reason": "mixed_currency",
                    "evaluator_version": self.registry.version_for_metric(generic_name),
                    "details": {
                        "currencies": sorted(costs),
                        "unit": "successful case execution",
                    },
                }
        output["cost_by_currency"] = cost_groups
        if include_categories:
            output["categories"] = {
                category: self._metrics_for_rows(
                    category_rows, include_categories=False, scope_by_variant=scope_by_variant
                )
                for category, category_rows in sorted(by_category.items())
            }
        return output

    @staticmethod
    async def _pair_count(
        session: AsyncSession,
        workspace_id: UUID,
        run: EvaluationExperimentRun,
        baseline_variant_id: UUID,
        candidate_variant_id: UUID,
    ) -> tuple[int, int]:
        rows = list(
            await session.scalars(
                select(EvaluationExperimentCaseResult).where(
                    EvaluationExperimentCaseResult.workspace_id == workspace_id,
                    EvaluationExperimentCaseResult.experiment_run_id == run.id,
                    EvaluationExperimentCaseResult.experiment_variant_id.in_(
                        [baseline_variant_id, candidate_variant_id]
                    ),
                )
            )
        )
        by_item: dict[UUID, dict[UUID, set[int]]] = defaultdict(lambda: defaultdict(set))
        for row in rows:
            by_item[row.dataset_item_id][row.experiment_variant_id].add(row.repetition_index)
        expected = set(range(run.repetitions))
        complete = sum(
            baseline_variant_id in variants
            and candidate_variant_id in variants
            and variants[baseline_variant_id] == expected
            and variants[candidate_variant_id] == expected
            for variants in by_item.values()
        )
        return complete, max(len(by_item) - complete, 0)

    async def _paired_metric_values(
        self,
        session: AsyncSession,
        workspace_id: UUID,
        run_id: UUID,
        baseline_variant_id: UUID,
        candidate_variant_id: UUID,
        metric_name: str,
    ) -> list[tuple[float | None, float | None]]:
        rows = await self._case_rows(session, workspace_id, run_id)
        by_item: dict[UUID, dict[UUID, list[MetricValue]]] = defaultdict(lambda: defaultdict(list))
        for case, item, variant in rows:
            if variant.id not in {baseline_variant_id, candidate_variant_id}:
                continue
            metric = self.registry.evaluate(item.category, item.expected, case.observation).get(
                metric_name
            )
            if metric is not None:
                by_item[item.id][variant.id].append(metric)
        return [
            (
                _float_metric(
                    _aggregate_repetitions(metric_name, values.get(baseline_variant_id, []))
                ),
                _float_metric(
                    _aggregate_repetitions(metric_name, values.get(candidate_variant_id, []))
                ),
            )
            for values in by_item.values()
        ]

    def _metric_records(
        self, workspace_id: UUID, run_id: UUID, output: dict[str, Any]
    ) -> list[EvaluationMetricResult]:
        records: list[EvaluationMetricResult] = []
        for variant_id, metrics in output["variants"].items():
            records.extend(
                self._metric_records_for_dimension(
                    workspace_id, run_id, UUID(variant_id), "VARIANT", "ALL", metrics
                )
            )
            for category, category_metrics in metrics.get("categories", {}).items():
                records.extend(
                    self._metric_records_for_dimension(
                        workspace_id,
                        run_id,
                        UUID(variant_id),
                        "CATEGORY",
                        category,
                        category_metrics,
                    )
                )
        records.extend(
            self._metric_records_for_dimension(
                workspace_id, run_id, None, "OVERALL", "ALL", output["overall"]
            )
        )
        for category, category_metrics in output["overall"].get("categories", {}).items():
            records.extend(
                self._metric_records_for_dimension(
                    workspace_id,
                    run_id,
                    None,
                    "OVERALL_CATEGORY",
                    category,
                    category_metrics,
                )
            )
        return records

    def _metric_records_for_dimension(
        self,
        workspace_id: UUID,
        run_id: UUID,
        variant_id: UUID | None,
        dimension: str,
        category: str,
        metrics: dict[str, Any],
    ) -> list[EvaluationMetricResult]:
        records = []
        for name, metric in _metric_items(metrics):
            definition = self.registry.definition_for(name)
            records.append(
                EvaluationMetricResult(
                    workspace_id=workspace_id,
                    experiment_run_id=run_id,
                    experiment_variant_id=variant_id,
                    dimension=dimension,
                    category=category,
                    metric_name=name,
                    status=str(metric["status"]),
                    value=_decimal_or_none(metric.get("value")),
                    sample_count=int(metric.get("sample_count", 0)),
                    numerator=_decimal_or_none(metric.get("numerator")),
                    denominator=_decimal_or_none(metric.get("denominator")),
                    reason=metric.get("reason"),
                    evaluator_version=str(metric.get("evaluator_version", definition.version)),
                    direction=str(definition.direction),
                    details=metric,
                )
            )
        return records

    def _register_persisted_definition(self, row: EvaluationMetricResult) -> None:
        try:
            self.registry.definition_for(row.metric_name)
        except ValueError:
            self.registry.register_metric_definition(
                row.metric_name,
                MetricDirection(row.direction),
                MetricAggregationKind.COST
                if row.metric_name.startswith("cost") or row.metric_name.startswith("total_cost")
                else MetricAggregationKind.SCALAR,
                version=row.evaluator_version,
            )

    @staticmethod
    def _case_result_set_hash(rows: Sequence[tuple[Any, Any, Any]]) -> str:
        payload = []
        for case, _item, _variant in rows:
            payload.append(
                {
                    "dataset_item_id": str(case.dataset_item_id),
                    "variant_id": str(case.experiment_variant_id),
                    "repetition_index": case.repetition_index,
                    "status": str(case.status),
                    "observed_agent_status": case.observed_agent_status,
                    "observed_agent_failure_code": case.observed_agent_failure_code,
                    "safe_observation_hash": canonical_json_hash(case.observation),
                    "latency_ms": case.latency_ms,
                    "input_tokens": case.input_tokens,
                    "output_tokens": case.output_tokens,
                    "total_tokens": case.total_tokens,
                    "cached_tokens": case.cached_tokens,
                    "cost_amount": _decimal_text(case.cost_amount),
                    "cost_currency": case.cost_currency,
                }
            )
        return canonical_json_hash(
            sorted(
                payload,
                key=lambda item: (
                    item["dataset_item_id"],
                    item["variant_id"],
                    item["repetition_index"],
                ),
            )
        )

    @staticmethod
    def _metric_rows_hash(rows: Sequence[EvaluationMetricResult]) -> str:
        payload = []
        for row in rows:
            payload.append(
                {
                    "variant_id": str(row.experiment_variant_id)
                    if row.experiment_variant_id
                    else None,
                    "dimension": row.dimension,
                    "category": row.category,
                    "metric_name": row.metric_name,
                    "status": row.status,
                    "value": _decimal_text(row.value),
                    "sample_count": row.sample_count,
                    "numerator": _decimal_text(row.numerator),
                    "denominator": _decimal_text(row.denominator),
                    "evaluator_version": row.evaluator_version,
                    "direction": row.direction,
                    "details": row.details,
                }
            )
        return canonical_json_hash(
            sorted(
                payload,
                key=lambda item: (
                    item["dimension"],
                    item["category"],
                    item["variant_id"] or "",
                    item["metric_name"],
                ),
            )
        )

    @staticmethod
    def _comparison_hash_input(
        run_id: UUID,
        metric_snapshot_hash: str,
        baseline_variant_hash: str,
        candidate_variant_hash: str,
        evaluator_manifest_hash: str,
        evaluator_versions: dict[str, str],
        metrics: dict[str, Any],
        complete_pairs: int,
        incomplete_pairs: int,
        status: str,
    ) -> dict[str, Any]:
        return {
            "run_id": str(run_id),
            "metric_snapshot_hash": metric_snapshot_hash,
            "baseline_variant_hash": baseline_variant_hash,
            "candidate_variant_hash": candidate_variant_hash,
            "evaluator_manifest_hash": evaluator_manifest_hash,
            "evaluator_versions": evaluator_versions,
            "metrics": metrics,
            "complete_pairs": complete_pairs,
            "incomplete_pairs": incomplete_pairs,
            "status": status,
        }

    @staticmethod
    def _verify_comparison_hash(comparison: EvaluationExperimentComparison) -> None:
        if not comparison.comparison_hash or not comparison.metric_snapshot_hash:
            raise AgentHubError(
                "EVALUATION_COMPARISON_INTEGRITY_ERROR",
                "The comparison artifact is incomplete.",
                409,
            )
        expected = canonical_json_hash(
            EvaluationMetricsService._comparison_hash_input(
                comparison.experiment_run_id,
                comparison.metric_snapshot_hash,
                comparison.baseline_variant_hash or "",
                comparison.candidate_variant_hash or "",
                comparison.evaluator_manifest_hash or "",
                comparison.evaluator_versions,
                comparison.metrics,
                comparison.paired_pairs,
                comparison.missing_pairs,
                comparison.status,
            )
        )
        if expected != comparison.comparison_hash:
            raise AgentHubError(
                "EVALUATION_COMPARISON_INTEGRITY_ERROR",
                "The comparison artifact integrity check failed.",
                409,
            )

    @staticmethod
    def _verify_comparison_binding(
        comparison: EvaluationExperimentComparison, snapshot: EvaluationMetricSnapshot
    ) -> None:
        if (
            comparison.metric_snapshot_id != snapshot.id
            or comparison.metric_snapshot_hash != snapshot.metrics_hash
        ):
            raise AgentHubError(
                "EVALUATION_COMPARISON_INTEGRITY_ERROR",
                "The comparison is not bound to the run metric snapshot.",
                409,
            )

    @staticmethod
    def _verify_comparison_variants(
        comparison: EvaluationExperimentComparison,
        baseline: EvaluationExperimentVariant,
        candidate: EvaluationExperimentVariant,
        snapshot: EvaluationMetricSnapshot,
    ) -> None:
        if (
            comparison.baseline_variant_hash != baseline.variant_hash
            or comparison.candidate_variant_hash != candidate.variant_hash
            or comparison.evaluator_manifest_hash != snapshot.evaluator_manifest_hash
        ):
            raise AgentHubError(
                "EVALUATION_COMPARISON_INTEGRITY_ERROR",
                "The comparison variant or evaluator binding is invalid.",
                409,
            )

    @staticmethod
    def _payload(snapshot: EvaluationMetricSnapshot, output: dict[str, Any]) -> dict[str, Any]:
        return {
            **output,
            "snapshot_id": str(snapshot.id),
            "evaluator_manifest_hash": snapshot.evaluator_manifest_hash,
            "case_result_set_hash": snapshot.case_result_set_hash,
            "metrics_hash": snapshot.metrics_hash,
        }

    @staticmethod
    def _require_read(context: WorkspaceExecutionContext) -> None:
        if (
            EVALUATION_READ not in context.permissions
            and "workspace_read" not in context.permissions
        ):
            raise AgentHubError("FORBIDDEN", "You do not have permission.", 403)

    @staticmethod
    def _require_mutation(context: WorkspaceExecutionContext) -> None:
        if EVALUATION_RUN not in context.permissions:
            raise AgentHubError("FORBIDDEN", "You do not have permission.", 403)

    @staticmethod
    def _workspace_id(context: WorkspaceExecutionContext) -> UUID:
        try:
            return UUID(context.workspace_id)
        except ValueError:
            raise AgentHubError("INVALID_WORKSPACE", "Workspace context is invalid.", 500) from None

    @staticmethod
    def _user_id(context: WorkspaceExecutionContext) -> UUID:
        try:
            if context.user_id is None:
                raise ValueError
            return UUID(context.user_id)
        except ValueError:
            raise AgentHubError(
                "AUTHENTICATION_REQUIRED", "Authentication is required.", 401
            ) from None

    @staticmethod
    def _not_found(code: str) -> None:
        raise AgentHubError(code, "The evaluation resource was not found.", 404)

    @staticmethod
    def _integrity_error(code: str = "EXPERIMENT_REPRODUCIBILITY_VIOLATION") -> None:
        raise AgentHubError(code, "The evaluation artifact integrity check failed.", 409)


def _aggregate_repetitions(name: str, values: Sequence[MetricValue]) -> MetricValue:
    if not values:
        return MetricValue(name, MetricStatus.NOT_AVAILABLE, None, 1, reason="no_observation")
    available = [value for value in values if value.status == MetricStatus.AVAILABLE]
    not_available = sum(value.status == MetricStatus.NOT_AVAILABLE for value in values)
    not_applicable = sum(value.status == MetricStatus.NOT_APPLICABLE for value in values)
    details = {
        "repetition_count": len(values),
        "known_repetition_count": len(available),
        "not_available_repetition_count": not_available,
        "not_applicable_repetition_count": not_applicable,
    }
    if not available:
        status = MetricStatus.NOT_APPLICABLE if not_available == 0 else MetricStatus.NOT_AVAILABLE
        return MetricValue(
            name, status, None, 1, reason="no_available_repetitions", details=details
        )
    numeric = [float(value.value) for value in available if value.value is not None]
    value = mean(numeric) if numeric else None
    if value is None:
        return MetricValue(
            name,
            MetricStatus.NOT_AVAILABLE,
            None,
            1,
            reason="metric_value_missing",
            details=details,
        )
    return MetricValue(name, MetricStatus.AVAILABLE, value, 1, value, 1, details=details)


def _metric_from_row(row: EvaluationMetricResult) -> MetricValue:
    return MetricValue(
        row.metric_name,
        MetricStatus(row.status),
        float(row.value) if row.value is not None else None,
        row.sample_count,
        float(row.numerator) if row.numerator is not None else None,
        float(row.denominator) if row.denominator is not None else None,
        row.reason,
        row.evaluator_version,
        row.details,
    )


def _not_comparable_metric(
    baseline: MetricValue, candidate: MetricValue, reason: str
) -> dict[str, Any]:
    return {
        "baseline": baseline.to_dict(),
        "candidate": candidate.to_dict(),
        "absolute_delta": None,
        "relative_delta": None,
        "paired_win": 0,
        "paired_tie": 0,
        "paired_loss": 0,
        "applicable_pairs": 0,
        "missing_pairs": 0,
        "direction": MetricDirection.UNKNOWN.value,
        "status": "NOT_COMPARABLE",
        "reason": reason,
    }


def _output_from_metric_rows(
    run_id: UUID, rows: Sequence[EvaluationMetricResult]
) -> dict[str, Any]:
    output: dict[str, Any] = {"run_id": str(run_id), "variants": {}, "overall": {}}
    for row in rows:
        metric = _metric_from_row(row).to_dict()
        if row.dimension == "OVERALL":
            output["overall"][row.metric_name] = metric
        elif row.dimension == "OVERALL_CATEGORY":
            output["overall"].setdefault("categories", {}).setdefault(row.category, {})[
                row.metric_name
            ] = metric
        elif row.dimension == "VARIANT" and row.experiment_variant_id is not None:
            output["variants"].setdefault(str(row.experiment_variant_id), {})[row.metric_name] = (
                metric
            )
        elif row.dimension == "CATEGORY" and row.experiment_variant_id is not None:
            variant = output["variants"].setdefault(str(row.experiment_variant_id), {})
            variant.setdefault("categories", {}).setdefault(row.category, {})[row.metric_name] = (
                metric
            )
    return output


def _metric_items(metrics: dict[str, Any]):
    return (
        (name, value)
        for name, value in metrics.items()
        if isinstance(value, dict) and "status" in value
    )


def _scalar_metric(
    name: str,
    value: Any,
    sample_count: int,
    version: str,
    *,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if value is None:
        return {
            "name": name,
            "status": MetricStatus.NOT_AVAILABLE.value,
            "value": None,
            "sample_count": 0,
            "reason": "no_available_samples",
            "evaluator_version": version,
            "details": details or {},
        }
    return {
        "name": name,
        "status": MetricStatus.AVAILABLE.value,
        "value": str(value) if isinstance(value, Decimal) else value,
        "sample_count": sample_count,
        "evaluator_version": version,
        "details": details or {},
    }


def _rate_scalar(name: str, numerator: int, denominator: int, version: str) -> dict[str, Any]:
    if denominator == 0:
        return {
            "name": name,
            "status": MetricStatus.NOT_APPLICABLE.value,
            "value": None,
            "sample_count": 0,
            "reason": "zero_denominator",
            "evaluator_version": version,
        }
    return {
        "name": name,
        "status": MetricStatus.AVAILABLE.value,
        "value": numerator / denominator,
        "sample_count": denominator,
        "numerator": numerator,
        "denominator": denominator,
        "evaluator_version": version,
    }


def _float_metric(metric: MetricValue) -> float | None:
    return (
        float(metric.value)
        if metric.status == MetricStatus.AVAILABLE and metric.value is not None
        else None
    )


def _currency(row: EvaluationMetricResult) -> str | None:
    details = row.details if isinstance(row.details, dict) else {}
    value = details.get("currency")
    if value is None and isinstance(details.get("details"), dict):
        value = details["details"].get("currency")
    return str(value) if value is not None else None


def _decimal_text(value: Any) -> str | None:
    if value is None:
        return None
    decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    return format(decimal_value.normalize(), "f")


def _decimal_or_none(value: Any) -> Decimal | None:
    return Decimal(str(value)) if value is not None else None


__all__ = ["EvaluationMetricsService"]
