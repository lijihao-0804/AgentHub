"""Immutable, provider-neutral experiment ablation analysis."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.agent_runtime.models import AgentVersion
from packages.control_plane.rbac import EVALUATION_READ, EVALUATION_RUN
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.evaluation.metrics_service import EvaluationMetricsService
from packages.evaluation.models import (
    EvaluationAblationFactor,
    EvaluationAblationResult,
    EvaluationExperimentComparison,
    EvaluationExperimentRun,
    EvaluationExperimentVariant,
)


class EvaluationAblationService:
    async def create_ablation(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
        run_id: UUID,
        comparison_id: UUID,
    ) -> EvaluationAblationResult:
        self._require_mutation(context)
        workspace_id = self._workspace_id(context)
        comparison, baseline, candidate = await self._load_inputs(
            session, workspace_id, run_id, comparison_id
        )
        existing = await session.scalar(
            select(EvaluationAblationResult).where(
                EvaluationAblationResult.workspace_id == workspace_id,
                EvaluationAblationResult.comparison_id == comparison_id,
            )
        )
        if existing is not None:
            self._verify(existing, comparison)
            return existing

        baseline_spec, candidate_spec = await self._load_specs(
            session, workspace_id, baseline, candidate
        )
        analysis = _analyze_specs(baseline_spec, candidate_spec)
        result = EvaluationAblationResult(
            workspace_id=workspace_id,
            comparison_id=comparison_id,
            experiment_run_id=run_id,
            baseline_variant_id=baseline.id,
            candidate_variant_id=candidate.id,
            factor=analysis["factor"],
            changed_paths=analysis["changed_paths"],
            baseline_factor_hash=analysis["baseline_factor_hash"],
            candidate_factor_hash=analysis["candidate_factor_hash"],
            analysis_hash=_analysis_hash(comparison.comparison_hash or "", analysis),
            created_by=self._user_id(context),
        )
        session.add(result)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            existing = await session.scalar(
                select(EvaluationAblationResult).where(
                    EvaluationAblationResult.workspace_id == workspace_id,
                    EvaluationAblationResult.comparison_id == comparison_id,
                )
            )
            if existing is None:
                raise
            self._verify(existing, comparison)
            return existing
        return result

    async def get_ablation(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
        run_id: UUID,
        comparison_id: UUID,
    ) -> EvaluationAblationResult:
        self._require_read(context)
        workspace_id = self._workspace_id(context)
        comparison, _baseline, _candidate = await self._load_inputs(
            session, workspace_id, run_id, comparison_id
        )
        result = await session.scalar(
            select(EvaluationAblationResult).where(
                EvaluationAblationResult.workspace_id == workspace_id,
                EvaluationAblationResult.comparison_id == comparison_id,
            )
        )
        if result is None:
            raise AgentHubError("EVALUATION_ABLATION_NOT_FOUND", "The ablation was not found.", 404)
        self._verify(result, comparison)
        return result

    async def _load_inputs(
        self, session: AsyncSession, workspace_id: UUID, run_id: UUID, comparison_id: UUID
    ) -> tuple[
        EvaluationExperimentComparison,
        EvaluationExperimentVariant,
        EvaluationExperimentVariant,
    ]:
        run = await session.scalar(
            select(EvaluationExperimentRun).where(
                EvaluationExperimentRun.workspace_id == workspace_id,
                EvaluationExperimentRun.id == run_id,
            )
        )
        comparison = await session.scalar(
            select(EvaluationExperimentComparison).where(
                EvaluationExperimentComparison.workspace_id == workspace_id,
                EvaluationExperimentComparison.experiment_run_id == run_id,
                EvaluationExperimentComparison.id == comparison_id,
            )
        )
        if run is None or comparison is None:
            raise AgentHubError(
                "EVALUATION_COMPARISON_NOT_FOUND", "The comparison was not found.", 404
            )
        EvaluationMetricsService._verify_comparison_hash(comparison)
        variants = list(
            await session.scalars(
                select(EvaluationExperimentVariant).where(
                    EvaluationExperimentVariant.workspace_id == workspace_id,
                    EvaluationExperimentVariant.id.in_(
                        [comparison.baseline_variant_id, comparison.candidate_variant_id]
                    ),
                )
            )
        )
        by_id = {variant.id: variant for variant in variants}
        baseline = by_id.get(comparison.baseline_variant_id)
        candidate = by_id.get(comparison.candidate_variant_id)
        if baseline is None or candidate is None:
            raise AgentHubError(
                "EVALUATION_COMPARISON_INTEGRITY_ERROR",
                "The comparison variants are unavailable.",
                409,
            )
        if (
            baseline.experiment_id != candidate.experiment_id
            or baseline.experiment_id != run.experiment_id
        ):
            raise AgentHubError(
                "EVALUATION_COMPARISON_INTEGRITY_ERROR",
                "The comparison variants are not bound to the experiment run.",
                409,
            )
        return comparison, baseline, candidate

    @staticmethod
    async def _load_specs(
        session: AsyncSession,
        workspace_id: UUID,
        baseline: EvaluationExperimentVariant,
        candidate: EvaluationExperimentVariant,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        versions = list(
            await session.scalars(
                select(AgentVersion).where(
                    AgentVersion.workspace_id == workspace_id,
                    AgentVersion.id.in_([baseline.agent_version_id, candidate.agent_version_id]),
                )
            )
        )
        by_id = {version.id: version for version in versions}
        before = by_id.get(baseline.agent_version_id)
        after = by_id.get(candidate.agent_version_id)
        if before is None or after is None:
            raise AgentHubError(
                "EVALUATION_ABLATION_INTEGRITY_ERROR",
                "The ablation AgentVersion binding is unavailable.",
                409,
            )
        if (
            canonical_json_hash(before.resolved_spec) != baseline.resolved_spec_hash
            or canonical_json_hash(after.resolved_spec) != candidate.resolved_spec_hash
        ):
            raise AgentHubError(
                "EVALUATION_ABLATION_INTEGRITY_ERROR",
                "The ablation AgentVersion binding is invalid.",
                409,
            )
        return before.resolved_spec, after.resolved_spec

    @staticmethod
    def _verify(
        result: EvaluationAblationResult, comparison: EvaluationExperimentComparison
    ) -> None:
        analysis = {
            "factor": result.factor,
            "changed_paths": result.changed_paths,
            "baseline_factor_hash": result.baseline_factor_hash,
            "candidate_factor_hash": result.candidate_factor_hash,
        }
        if result.analysis_hash != _analysis_hash(comparison.comparison_hash or "", analysis):
            raise AgentHubError(
                "EVALUATION_ABLATION_INTEGRITY_ERROR",
                "The ablation artifact integrity check failed.",
                409,
            )

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
            raise AgentHubError(
                "INVALID_WORKSPACE", "Workspace context is invalid.", 500
            ) from None

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


def _diff_paths(before: Any, after: Any, prefix: str = "") -> list[str]:
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        paths: list[str] = []
        for key in sorted(set(before) | set(after), key=str):
            child = f"{prefix}.{key}" if prefix else str(key)
            if key not in before or key not in after:
                paths.append(child)
            else:
                paths.extend(_diff_paths(before[key], after[key], child))
        return paths
    if before != after:
        return [prefix or "$"]
    return []


def _section_hash(spec: Mapping[str, Any], section: str) -> str:
    return canonical_json_hash(spec.get(section, {}))


def _analyze_specs(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    changed_paths = _diff_paths(before, after)
    if not changed_paths:
        factor = EvaluationAblationFactor.NO_CHANGE.value
    else:
        roots = {path.split(".", 1)[0] for path in changed_paths}
        if roots == {"prompt"}:
            factor = EvaluationAblationFactor.PROMPT.value
        elif roots == {"model"}:
            factor = EvaluationAblationFactor.MODEL.value
        elif roots == {"retrieval"} and _knowledge_snapshots(before) == _knowledge_snapshots(after):
            factor = EvaluationAblationFactor.RETRIEVAL.value
        else:
            factor = EvaluationAblationFactor.MULTI_FACTOR_CHANGE.value
    section = {
        EvaluationAblationFactor.PROMPT.value: "prompt",
        EvaluationAblationFactor.MODEL.value: "model",
        EvaluationAblationFactor.RETRIEVAL.value: "retrieval",
    }.get(factor)
    if section is None:
        baseline_factor_hash = canonical_json_hash(before)
        candidate_factor_hash = canonical_json_hash(after)
    else:
        baseline_factor_hash = _section_hash(before, section)
        candidate_factor_hash = _section_hash(after, section)
    return {
        "factor": factor,
        "changed_paths": changed_paths,
        "baseline_factor_hash": baseline_factor_hash,
        "candidate_factor_hash": candidate_factor_hash,
    }


def _knowledge_snapshots(spec: Mapping[str, Any]) -> Any:
    retrieval = spec.get("retrieval", {})
    if not isinstance(retrieval, Mapping):
        return None
    return retrieval.get("knowledge_snapshots", retrieval.get("knowledge_snapshot_ids", []))


def _analysis_hash(comparison_hash: str, analysis: Mapping[str, Any]) -> str:
    return canonical_json_hash({"comparison_hash": comparison_hash, **dict(analysis)})


__all__ = ["EvaluationAblationService"]
