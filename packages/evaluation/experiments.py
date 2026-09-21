"""M7-B experiment definition, reproducibility, and queued-run services."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, NoReturn
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from packages.agent_runtime.frozen import parse_frozen_agent_spec
from packages.agent_runtime.models import AgentVersion
from packages.control_plane.rbac import EVALUATION_MANAGE, EVALUATION_READ, EVALUATION_RUN
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.evaluation.build_identity import (
    BuildIdentityProvider,
    EnvironmentBuildIdentityProvider,
)
from packages.evaluation.judge import FrozenJudgeProfile
from packages.evaluation.models import (
    EvaluationDatasetItem,
    EvaluationDatasetVersion,
    EvaluationDatasetVersionStatus,
    EvaluationExperiment,
    EvaluationExperimentCaseResult,
    EvaluationExperimentHoldoutExposure,
    EvaluationExperimentPurpose,
    EvaluationExperimentRun,
    EvaluationExperimentRunStatus,
    EvaluationExperimentStatus,
    EvaluationExperimentVariant,
    PricingSnapshot,
)
from packages.evaluation.reproducibility import (
    EVALUATION_SCHEMA_VERSION,
    default_evaluator_manifest,
    experiment_spec_hash,
    normalize_knowledge_snapshots,
    pricing_snapshot_content_hash,
    variant_hash,
)
from packages.evaluation.validation import validate_variant_metadata
from packages.knowledge.models import KnowledgeSnapshot
from packages.knowledge.snapshots import KnowledgeSnapshotService


class ExperimentService:
    def __init__(self, build_identity_provider: BuildIdentityProvider | None = None) -> None:
        self.build_identity_provider = build_identity_provider or EnvironmentBuildIdentityProvider()

    async def create_experiment(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
        name: str,
        description: str | None,
        dataset_version_id: UUID,
        split: str,
        purpose: str,
        repetitions: int = 1,
        judge_profile: FrozenJudgeProfile | None = None,
    ) -> EvaluationExperiment:
        self._require_permission(context, EVALUATION_MANAGE)
        normalized_name = self._required_text(name, "name", code="EVALUATION_EXPERIMENT_INVALID")
        self._validate_definition(split, purpose, repetitions)
        workspace_id = self._workspace_id(context)
        dataset_version = await session.scalar(
            select(EvaluationDatasetVersion).where(
                EvaluationDatasetVersion.workspace_id == workspace_id,
                EvaluationDatasetVersion.id == dataset_version_id,
            )
        )
        if dataset_version is None:
            raise AgentHubError(
                "EVALUATION_DATASET_VERSION_NOT_FOUND",
                "The dataset version was not found.",
                404,
            )
        if dataset_version.status != EvaluationDatasetVersionStatus.PUBLISHED:
            raise AgentHubError(
                "EVALUATION_DATASET_NOT_PUBLISHED",
                "Experiments require a published dataset version.",
                422,
            )
        split_count = await session.scalar(
            select(func.count(EvaluationDatasetItem.id)).where(
                EvaluationDatasetItem.workspace_id == workspace_id,
                EvaluationDatasetItem.dataset_version_id == dataset_version.id,
                EvaluationDatasetItem.split == split,
            )
        )
        if not split_count:
            raise AgentHubError(
                "EVALUATION_DATASET_SPLIT_EMPTY",
                "The selected dataset split has no cases.",
                422,
            )
        build_sha = self.build_identity_provider.get_build_sha()
        if not build_sha:
            raise AgentHubError(
                "EXPERIMENT_BUILD_ID_UNKNOWN",
                "A formal experiment requires a validated build identity.",
                422,
            )
        experiment = EvaluationExperiment(
            workspace_id=workspace_id,
            name=normalized_name,
            description=description.strip() if description else None,
            dataset_version_id=dataset_version.id,
            dataset_content_hash=dataset_version.content_hash,
            dataset_schema_version=dataset_version.schema_version,
            split=split,
            purpose=purpose,
            repetitions=repetitions,
            status=EvaluationExperimentStatus.DRAFT,
            build_sha=build_sha,
            # The judge, when one is used, is frozen here with the deterministic
            # evaluators so it cannot be swapped underneath historical scores.
            evaluator_manifest=default_evaluator_manifest(judge_profile),
            created_by=self._user_id(context),
        )
        session.add(experiment)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise AgentHubError(
                "EVALUATION_EXPERIMENT_CREATE_FAILED",
                "The experiment could not be recorded.",
                409,
            ) from exc
        return experiment

    async def list_experiments(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
    ) -> list[EvaluationExperiment]:
        self._require_read(context)
        result = await session.scalars(
            select(EvaluationExperiment)
            .where(EvaluationExperiment.workspace_id == self._workspace_id(context))
            .order_by(EvaluationExperiment.created_at.desc(), EvaluationExperiment.id)
        )
        return list(result)

    async def get_experiment(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
        experiment_id: UUID,
        for_update: bool = False,
    ) -> EvaluationExperiment:
        self._require_read(context) if not for_update else self._require_permission(
            context, EVALUATION_MANAGE
        )
        statement = select(EvaluationExperiment).where(
            EvaluationExperiment.workspace_id == self._workspace_id(context),
            EvaluationExperiment.id == experiment_id,
        )
        if for_update:
            statement = statement.with_for_update()
        experiment = await session.scalar(statement)
        if experiment is None:
            self._not_found("EVALUATION_EXPERIMENT_NOT_FOUND", "The experiment was not found.")
        return experiment

    async def list_variants(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
        experiment_id: UUID,
    ) -> list[EvaluationExperimentVariant]:
        await self.get_experiment(session, context=context, experiment_id=experiment_id)
        result = await session.scalars(
            select(EvaluationExperimentVariant)
            .where(
                EvaluationExperimentVariant.workspace_id == self._workspace_id(context),
                EvaluationExperimentVariant.experiment_id == experiment_id,
            )
            .order_by(EvaluationExperimentVariant.ordinal, EvaluationExperimentVariant.id)
        )
        return list(result)

    async def add_variant(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
        experiment_id: UUID,
        label: str,
        agent_version_id: UUID,
        pricing_snapshot_id: UUID,
        ordinal: int,
        variant_metadata: Mapping[str, Any] | None = None,
    ) -> EvaluationExperimentVariant:
        self._require_permission(context, EVALUATION_MANAGE)
        experiment = await self.get_experiment(
            session, context=context, experiment_id=experiment_id, for_update=True
        )
        self._require_draft(experiment)
        normalized_label = self._required_text(label, "label", code="EVALUATION_VARIANT_INVALID")
        if ordinal < 0 or ordinal >= 5:
            raise AgentHubError(
                "EVALUATION_VARIANT_INVALID", "ordinal must be between 0 and 4.", 422
            )
        current_count = await session.scalar(
            select(func.count(EvaluationExperimentVariant.id)).where(
                EvaluationExperimentVariant.workspace_id == experiment.workspace_id,
                EvaluationExperimentVariant.experiment_id == experiment.id,
            )
        )
        if current_count >= 5:
            raise AgentHubError(
                "EVALUATION_VARIANT_LIMIT",
                "An experiment may contain at most five variants.",
                422,
            )
        metadata = validate_variant_metadata(
            variant_metadata if variant_metadata is not None else {}
        )
        agent_version = await self._load_agent_version(
            session, experiment.workspace_id, agent_version_id
        )
        pricing_snapshot = await session.scalar(
            select(PricingSnapshot).where(
                PricingSnapshot.workspace_id == experiment.workspace_id,
                PricingSnapshot.id == pricing_snapshot_id,
            )
        )
        if pricing_snapshot is None:
            raise AgentHubError(
                "EVALUATION_PRICING_NOT_FOUND",
                "The pricing snapshot was not found.",
                404,
            )
        pricing_hash = pricing_snapshot_content_hash(pricing_snapshot)
        if pricing_hash != pricing_snapshot.content_hash:
            raise AgentHubError(
                "EVALUATION_PRICING_INTEGRITY_ERROR",
                "The pricing snapshot content hash is invalid.",
                422,
            )
        self._validate_pricing_model(agent_version.resolved_spec, pricing_snapshot)
        snapshots = await self._freeze_knowledge(
            session, context, experiment.workspace_id, agent_version.resolved_spec
        )
        frozen_variant_hash = variant_hash(
            agent_version_id=str(agent_version.id),
            resolved_spec_hash=agent_version.resolved_spec_hash,
            effective_knowledge_snapshots=snapshots,
            pricing_snapshot_id=str(pricing_snapshot.id),
            pricing_snapshot_hash=pricing_hash,
            variant_metadata=metadata,
        )
        variant = EvaluationExperimentVariant(
            workspace_id=experiment.workspace_id,
            experiment_id=experiment.id,
            label=normalized_label,
            agent_version_id=agent_version.id,
            resolved_spec_hash=agent_version.resolved_spec_hash,
            pricing_snapshot_id=pricing_snapshot.id,
            pricing_snapshot_hash=pricing_hash,
            effective_knowledge_snapshots=snapshots,
            variant_metadata=metadata,
            variant_hash=frozen_variant_hash,
            ordinal=ordinal,
        )
        session.add(variant)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise AgentHubError(
                "EVALUATION_VARIANT_CONFLICT",
                "The variant label or ordinal is already used by this experiment.",
                409,
            ) from exc
        return variant

    async def finalize_experiment(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
        experiment_id: UUID,
    ) -> EvaluationExperiment:
        self._require_permission(context, EVALUATION_MANAGE)
        experiment = await self.get_experiment(
            session, context=context, experiment_id=experiment_id, for_update=True
        )
        self._require_draft(experiment)
        dataset_version = await session.scalar(
            select(EvaluationDatasetVersion).where(
                EvaluationDatasetVersion.workspace_id == experiment.workspace_id,
                EvaluationDatasetVersion.id == experiment.dataset_version_id,
            )
        )
        if (
            dataset_version is None
            or dataset_version.status != EvaluationDatasetVersionStatus.PUBLISHED
            or dataset_version.content_hash != experiment.dataset_content_hash
            or dataset_version.schema_version != experiment.dataset_schema_version
        ):
            raise AgentHubError(
                "EVALUATION_DATASET_INTEGRITY_ERROR",
                "The experiment dataset binding is no longer valid.",
                409,
            )
        variants = list(
            await session.scalars(
                select(EvaluationExperimentVariant)
                .where(
                    EvaluationExperimentVariant.workspace_id == experiment.workspace_id,
                    EvaluationExperimentVariant.experiment_id == experiment.id,
                )
                .order_by(EvaluationExperimentVariant.ordinal, EvaluationExperimentVariant.id)
            )
        )
        if not variants:
            raise AgentHubError(
                "EVALUATION_EXPERIMENT_NO_VARIANTS",
                "An experiment requires at least one variant.",
                422,
            )
        for variant in variants:
            await self._validate_stored_variant(session, variant, experiment.workspace_id)
        spec = {
            "schema_version": EVALUATION_SCHEMA_VERSION,
            "dataset": {
                "version_id": str(experiment.dataset_version_id),
                "content_hash": experiment.dataset_content_hash,
                "schema_version": experiment.dataset_schema_version,
            },
            "split": experiment.split,
            "purpose": experiment.purpose,
            "repetitions": experiment.repetitions,
            "build_sha": experiment.build_sha,
            "variants": [self._variant_spec(variant) for variant in variants],
            "evaluator_manifest": experiment.evaluator_manifest,
        }
        experiment.spec_json = spec
        experiment.spec_hash = experiment_spec_hash(spec)
        experiment.status = EvaluationExperimentStatus.READY
        await session.commit()
        return experiment

    async def create_run(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
        experiment_id: UUID,
    ) -> tuple[EvaluationExperimentRun, int | None]:
        self._require_permission(context, EVALUATION_RUN)
        experiment = await session.scalar(
            select(EvaluationExperiment)
            .where(
                EvaluationExperiment.workspace_id == self._workspace_id(context),
                EvaluationExperiment.id == experiment_id,
            )
            .with_for_update()
        )
        if experiment is None:
            self._not_found("EVALUATION_EXPERIMENT_NOT_FOUND", "The experiment was not found.")
        if experiment.status != EvaluationExperimentStatus.READY:
            raise AgentHubError(
                "EVALUATION_EXPERIMENT_NOT_READY",
                "Only a READY experiment can create a run.",
                409,
            )
        if not experiment.spec_json or not experiment.spec_hash:
            raise AgentHubError(
                "EVALUATION_EXPERIMENT_INTEGRITY_ERROR",
                "The experiment specification is missing.",
                409,
            )
        if experiment_spec_hash(experiment.spec_json) != experiment.spec_hash:
            raise AgentHubError(
                "EVALUATION_EXPERIMENT_INTEGRITY_ERROR",
                "The experiment specification hash is invalid.",
                409,
            )
        run = EvaluationExperimentRun(
            workspace_id=experiment.workspace_id,
            experiment_id=experiment.id,
            status=EvaluationExperimentRunStatus.QUEUED,
            git_commit=experiment.build_sha,
            dataset_version_id=experiment.dataset_version_id,
            dataset_hash=experiment.dataset_content_hash,
            experiment_spec_hash=experiment.spec_hash,
            split=experiment.split,
            purpose=experiment.purpose,
            repetitions=experiment.repetitions,
            created_by=self._user_id(context),
        )
        session.add(run)
        exposure_index: int | None = None
        if experiment.split == "HOLDOUT":
            dataset_version = await session.scalar(
                select(EvaluationDatasetVersion)
                .where(
                    EvaluationDatasetVersion.workspace_id == experiment.workspace_id,
                    EvaluationDatasetVersion.id == experiment.dataset_version_id,
                )
                .with_for_update()
            )
            if (
                dataset_version is None
                or dataset_version.status != EvaluationDatasetVersionStatus.PUBLISHED
            ):
                raise AgentHubError(
                    "EVALUATION_DATASET_NOT_PUBLISHED",
                    "The holdout dataset version is no longer published.",
                    409,
                )
            exposure_index = (
                await session.scalar(
                    select(
                        func.coalesce(
                            func.max(EvaluationExperimentHoldoutExposure.exposure_index), 0
                        )
                    ).where(
                        EvaluationExperimentHoldoutExposure.workspace_id == experiment.workspace_id,
                        EvaluationExperimentHoldoutExposure.dataset_version_id
                        == experiment.dataset_version_id,
                    )
                )
            ) + 1
            await session.flush()
            session.add(
                EvaluationExperimentHoldoutExposure(
                    workspace_id=experiment.workspace_id,
                    dataset_version_id=experiment.dataset_version_id,
                    experiment_id=experiment.id,
                    experiment_run_id=run.id,
                    exposure_index=exposure_index,
                    purpose=experiment.purpose,
                    created_by=self._user_id(context),
                )
            )
            experiment.holdout_exposure_index = exposure_index
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise AgentHubError(
                "EVALUATION_EXPERIMENT_RUN_CREATE_FAILED",
                "The experiment run could not be queued.",
                409,
            ) from exc
        return run, exposure_index

    async def get_run(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
        run_id: UUID,
    ) -> tuple[EvaluationExperimentRun, int | None]:
        self._require_read(context)
        workspace_id = self._workspace_id(context)
        run = await session.scalar(
            select(EvaluationExperimentRun).where(
                EvaluationExperimentRun.workspace_id == workspace_id,
                EvaluationExperimentRun.id == run_id,
            )
        )
        if run is None:
            self._not_found(
                "EVALUATION_EXPERIMENT_RUN_NOT_FOUND", "The experiment run was not found."
            )
        exposure_index = await session.scalar(
            select(EvaluationExperimentHoldoutExposure.exposure_index).where(
                EvaluationExperimentHoldoutExposure.workspace_id == workspace_id,
                EvaluationExperimentHoldoutExposure.experiment_run_id == run.id,
            )
        )
        return run, exposure_index

    async def request_run_cancel(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
        run_id: UUID,
    ) -> EvaluationExperimentRun:
        self._require_permission(context, EVALUATION_RUN)
        run = await session.scalar(
            select(EvaluationExperimentRun)
            .where(
                EvaluationExperimentRun.workspace_id == self._workspace_id(context),
                EvaluationExperimentRun.id == run_id,
            )
            .with_for_update()
        )
        if run is None:
            self._not_found(
                "EVALUATION_EXPERIMENT_RUN_NOT_FOUND", "The experiment run was not found."
            )
        if run.status == EvaluationExperimentRunStatus.QUEUED:
            run.status = EvaluationExperimentRunStatus.CANCELLED
            run.completed_at = datetime.now(UTC)
            await session.execute(
                update(EvaluationExperimentCaseResult)
                .where(
                    EvaluationExperimentCaseResult.workspace_id == run.workspace_id,
                    EvaluationExperimentCaseResult.experiment_run_id == run.id,
                    EvaluationExperimentCaseResult.status == "PENDING",
                )
                .values(status="CANCELLED", completed_at=datetime.now(UTC))
            )
        elif run.status == EvaluationExperimentRunStatus.RUNNING:
            run.status = EvaluationExperimentRunStatus.CANCEL_REQUESTED
        await session.commit()
        return run

    async def run_progress(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
        run_id: UUID,
    ) -> dict[str, int | float]:
        run, _ = await self.get_run(session, context=context, run_id=run_id)
        del run
        rows = await session.execute(
            select(EvaluationExperimentCaseResult.status, func.count())
            .where(
                EvaluationExperimentCaseResult.workspace_id == self._workspace_id(context),
                EvaluationExperimentCaseResult.experiment_run_id == run_id,
            )
            .group_by(EvaluationExperimentCaseResult.status)
        )
        counts = {str(status): int(count) for status, count in rows}
        total = sum(counts.values())
        completed = counts.get("SUCCEEDED", 0)
        failed = counts.get("FAILED", 0)
        cancelled = counts.get("CANCELLED", 0)
        return {
            "total": total,
            "pending": counts.get("PENDING", 0),
            "running": counts.get("RUNNING", 0),
            "completed": completed,
            "failed": failed,
            "cancelled": cancelled,
            "progress": 1.0 if total == 0 else (completed + failed + cancelled) / total,
        }

    async def holdout_exposure_count(
        self,
        session: AsyncSession,
        *,
        context: WorkspaceExecutionContext,
        experiment_id: UUID,
    ) -> int:
        await self.get_experiment(session, context=context, experiment_id=experiment_id)
        count = await session.scalar(
            select(func.count(EvaluationExperimentHoldoutExposure.id)).where(
                EvaluationExperimentHoldoutExposure.workspace_id == self._workspace_id(context),
                EvaluationExperimentHoldoutExposure.experiment_id == experiment_id,
            )
        )
        return int(count or 0)

    async def _freeze_knowledge(
        self,
        session: AsyncSession,
        context: WorkspaceExecutionContext,
        workspace_id: UUID,
        resolved_spec: Mapping[str, Any],
    ) -> list[dict[str, str]]:
        retrieval = resolved_spec.get("retrieval")
        if not isinstance(retrieval, Mapping):
            raise self._agent_integrity_error()
        bindings = retrieval.get("knowledge_bindings")
        if bindings is None:
            if retrieval.get("knowledge_snapshots"):
                raise self._agent_integrity_error()
            return []
        if not isinstance(bindings, list):
            raise self._agent_integrity_error()
        snapshot_service = KnowledgeSnapshotService()
        frozen: list[dict[str, str]] = []
        knowledge_context = context.model_copy(
            update={"permissions": context.permissions | frozenset({"knowledge_run"})}
        )
        for binding in bindings:
            if not isinstance(binding, Mapping):
                raise self._agent_integrity_error()
            try:
                knowledge_base_id = UUID(str(binding["knowledge_base_id"]))
            except (KeyError, ValueError, TypeError):
                raise self._agent_integrity_error() from None
            binding_mode = binding.get("binding_mode")
            if binding_mode == "LATEST":
                snapshot = await snapshot_service.resolve_snapshot(
                    session,
                    knowledge_context,
                    knowledge_base_id,
                    "LATEST",
                )
                snapshot_id = snapshot.snapshot_id
                snapshot_hash = snapshot.content_hash
            elif binding_mode == "PINNED":
                try:
                    snapshot_id = UUID(str(binding["snapshot_id"]))
                    snapshot_hash = str(binding["snapshot_hash"])
                except (KeyError, ValueError, TypeError):
                    raise self._agent_integrity_error() from None
                snapshot = await session.scalar(
                    select(KnowledgeSnapshot).where(
                        KnowledgeSnapshot.workspace_id == workspace_id,
                        KnowledgeSnapshot.knowledge_base_id == knowledge_base_id,
                        KnowledgeSnapshot.id == snapshot_id,
                    )
                )
                if snapshot is None or snapshot.content_hash != snapshot_hash:
                    raise AgentHubError(
                        "EVALUATION_KNOWLEDGE_SNAPSHOT_INVALID",
                        "The pinned knowledge snapshot is invalid.",
                        422,
                    )
            else:
                raise self._agent_integrity_error()
            frozen.append(
                {
                    "knowledge_base_id": str(knowledge_base_id),
                    "binding_mode": str(binding_mode),
                    "snapshot_id": str(snapshot_id),
                    "snapshot_content_hash": snapshot_hash,
                }
            )
        return normalize_knowledge_snapshots(frozen)

    async def _validate_stored_variant(
        self,
        session: AsyncSession,
        variant: EvaluationExperimentVariant,
        workspace_id: UUID,
    ) -> None:
        agent_version = await self._load_agent_version(
            session, workspace_id, variant.agent_version_id
        )
        if agent_version.resolved_spec_hash != variant.resolved_spec_hash:
            raise self._agent_integrity_error()
        pricing_snapshot = await session.scalar(
            select(PricingSnapshot).where(
                PricingSnapshot.workspace_id == workspace_id,
                PricingSnapshot.id == variant.pricing_snapshot_id,
            )
        )
        if pricing_snapshot is None:
            raise AgentHubError(
                "EVALUATION_PRICING_NOT_FOUND",
                "The pricing snapshot was not found.",
                404,
            )
        if pricing_snapshot_content_hash(pricing_snapshot) != variant.pricing_snapshot_hash:
            raise AgentHubError(
                "EVALUATION_PRICING_INTEGRITY_ERROR",
                "The pricing snapshot changed after variant creation.",
                409,
            )
        self._validate_pricing_model(agent_version.resolved_spec, pricing_snapshot)
        snapshots = normalize_knowledge_snapshots(variant.effective_knowledge_snapshots)
        for item in snapshots:
            snapshot = await session.scalar(
                select(KnowledgeSnapshot).where(
                    KnowledgeSnapshot.workspace_id == workspace_id,
                    KnowledgeSnapshot.knowledge_base_id == UUID(item["knowledge_base_id"]),
                    KnowledgeSnapshot.id == UUID(item["snapshot_id"]),
                )
            )
            if snapshot is None or snapshot.content_hash != item["snapshot_content_hash"]:
                raise AgentHubError(
                    "EVALUATION_KNOWLEDGE_SNAPSHOT_INVALID",
                    "The frozen knowledge snapshot is invalid.",
                    409,
                )
        expected_hash = variant_hash(
            agent_version_id=str(variant.agent_version_id),
            resolved_spec_hash=variant.resolved_spec_hash,
            effective_knowledge_snapshots=snapshots,
            pricing_snapshot_id=str(variant.pricing_snapshot_id),
            pricing_snapshot_hash=variant.pricing_snapshot_hash,
            variant_metadata=variant.variant_metadata,
        )
        if expected_hash != variant.variant_hash:
            raise AgentHubError(
                "EVALUATION_VARIANT_INTEGRITY_ERROR",
                "The variant content hash is invalid.",
                409,
            )

    async def _load_agent_version(
        self, session: AsyncSession, workspace_id: UUID, agent_version_id: UUID
    ) -> AgentVersion:
        agent_version = await session.scalar(
            select(AgentVersion).where(
                AgentVersion.workspace_id == workspace_id,
                AgentVersion.id == agent_version_id,
            )
        )
        if agent_version is None:
            self._not_found(
                "EVALUATION_AGENT_VERSION_NOT_FOUND", "The agent version was not found."
            )
        if canonical_json_hash(agent_version.resolved_spec) != agent_version.resolved_spec_hash:
            raise self._agent_integrity_error()
        try:
            parse_frozen_agent_spec(agent_version.resolved_spec, workspace_id=workspace_id)
        except AgentHubError as exc:
            raise self._agent_integrity_error() from exc
        return agent_version

    @staticmethod
    def _variant_spec(variant: EvaluationExperimentVariant) -> dict[str, Any]:
        return {
            "label": variant.label,
            "ordinal": variant.ordinal,
            "agent_version_id": str(variant.agent_version_id),
            "resolved_spec_hash": variant.resolved_spec_hash,
            "knowledge_snapshots": normalize_knowledge_snapshots(
                variant.effective_knowledge_snapshots
            ),
            "pricing_snapshot_id": str(variant.pricing_snapshot_id),
            "pricing_snapshot_hash": variant.pricing_snapshot_hash,
            "variant_metadata": dict(variant.variant_metadata),
            "variant_hash": variant.variant_hash,
        }

    @staticmethod
    def _validate_pricing_model(resolved_spec: Mapping[str, Any], pricing: PricingSnapshot) -> None:
        model = resolved_spec.get("model")
        if not isinstance(model, Mapping):
            raise ExperimentService._agent_integrity_error()
        if model.get("provider") != pricing.provider or model.get("model") != pricing.model:
            raise AgentHubError(
                "EVALUATION_PRICING_MODEL_MISMATCH",
                "The pricing provider/model does not match the AgentVersion.",
                422,
            )

    @staticmethod
    def _validate_definition(split: str, purpose: str, repetitions: int) -> None:
        if split not in {"DEV", "HOLDOUT"} or purpose not in {
            EvaluationExperimentPurpose.DEVELOPMENT,
            EvaluationExperimentPurpose.HOLDOUT_VALIDATION,
            EvaluationExperimentPurpose.RELEASE_GATE,
        }:
            raise AgentHubError(
                "EVALUATION_EXPERIMENT_INVALID", "The experiment definition is invalid.", 422
            )
        if (purpose == EvaluationExperimentPurpose.DEVELOPMENT and split != "DEV") or (
            purpose
            in {
                EvaluationExperimentPurpose.HOLDOUT_VALIDATION,
                EvaluationExperimentPurpose.RELEASE_GATE,
            }
            and split != "HOLDOUT"
        ):
            raise AgentHubError(
                "EVALUATION_EXPERIMENT_INVALID",
                "The experiment purpose and split do not match.",
                422,
            )
        if isinstance(repetitions, bool) or not 1 <= repetitions <= 5:
            raise AgentHubError(
                "EVALUATION_EXPERIMENT_INVALID",
                "repetitions must be between 1 and 5.",
                422,
            )

    @staticmethod
    def _require_draft(experiment: EvaluationExperiment) -> None:
        if experiment.status != EvaluationExperimentStatus.DRAFT:
            raise AgentHubError(
                "EVALUATION_EXPERIMENT_IMMUTABLE",
                "READY experiments are immutable.",
                409,
            )

    @staticmethod
    def _agent_integrity_error() -> AgentHubError:
        return AgentHubError(
            "EVALUATION_AGENT_VERSION_INTEGRITY_ERROR",
            "The AgentVersion reproducibility record is invalid.",
            409,
        )

    @staticmethod
    def _required_text(value: str, field: str, *, code: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise AgentHubError(code, f"{field} is required.", 422)
        return normalized

    def _require_read(self, context: WorkspaceExecutionContext) -> None:
        if (
            EVALUATION_READ not in context.permissions
            and "workspace_read" not in context.permissions
        ):
            raise AgentHubError("FORBIDDEN", "You do not have permission.", 403)

    @staticmethod
    def _require_permission(context: WorkspaceExecutionContext, permission: str) -> None:
        if permission not in context.permissions:
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
    def _not_found(code: str, message: str) -> NoReturn:
        raise AgentHubError(code, message, 404)


__all__ = ["ExperimentService"]
