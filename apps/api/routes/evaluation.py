from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.dependencies import get_db_session
from apps.api.knowledge_dependencies import get_experiment_run_queue, get_workspace_context
from apps.api.schemas.evaluation import (
    EvaluationAblationResponse,
    EvaluationComparisonCreateRequest,
    EvaluationComparisonResponse,
    EvaluationDatasetCreateRequest,
    EvaluationDatasetItemResponse,
    EvaluationDatasetResponse,
    EvaluationDatasetVersionCreateRequest,
    EvaluationDatasetVersionDetailResponse,
    EvaluationDatasetVersionResponse,
    EvaluationExperimentCreateRequest,
    EvaluationExperimentDetailResponse,
    EvaluationExperimentResponse,
    EvaluationExperimentRunProgressResponse,
    EvaluationExperimentRunResponse,
    EvaluationExperimentVariantCreateRequest,
    EvaluationExperimentVariantResponse,
    EvaluationReleaseGateCreateRequest,
    EvaluationReleaseGateDecisionResponse,
    EvaluationReleaseGatePolicyCreateRequest,
    EvaluationReleaseGatePolicyResponse,
    PricingSnapshotCreateRequest,
    PricingSnapshotResponse,
)
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.evaluation.ablation import EvaluationAblationService
from packages.evaluation.experiments import ExperimentService
from packages.evaluation.metrics_service import EvaluationMetricsService
from packages.evaluation.queue import ExperimentRunQueue
from packages.evaluation.release_gate import EvaluationReleaseGateService
from packages.evaluation.service import EvaluationDatasetService

router = APIRouter(prefix="/api/v1/workspaces/{workspace_id}/evaluation", tags=["evaluation"])
context_dependency = Depends(get_workspace_context)
db_session_dependency = Depends(get_db_session)
run_queue_dependency = Depends(get_experiment_run_queue)


@router.post(
    "/datasets",
    response_model=EvaluationDatasetResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_dataset(
    workspace_id: UUID,
    payload: EvaluationDatasetCreateRequest,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> EvaluationDatasetResponse:
    del workspace_id
    dataset = await EvaluationDatasetService().create_dataset(
        session,
        context=context,
        name=payload.name,
        description=payload.description,
    )
    return EvaluationDatasetResponse.model_validate(dataset, from_attributes=True)


@router.get("/datasets", response_model=list[EvaluationDatasetResponse])
async def list_datasets(
    workspace_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> list[EvaluationDatasetResponse]:
    del workspace_id
    datasets = await EvaluationDatasetService().list_datasets(session, context=context)
    return [
        EvaluationDatasetResponse.model_validate(item, from_attributes=True) for item in datasets
    ]


@router.post(
    "/datasets/{dataset_id}/versions",
    response_model=EvaluationDatasetVersionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_dataset_version(
    workspace_id: UUID,
    dataset_id: UUID,
    payload: EvaluationDatasetVersionCreateRequest,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> EvaluationDatasetVersionResponse:
    del workspace_id
    version = await EvaluationDatasetService().create_version(
        session,
        context=context,
        dataset_id=dataset_id,
        items=[item.model_dump() for item in payload.items],
        schema_version=payload.schema_version,
    )
    response = EvaluationDatasetVersionResponse.model_validate(version, from_attributes=True)
    return response.model_copy(update={"item_count": len(payload.items)})


@router.get(
    "/datasets/{dataset_id}/versions",
    response_model=list[EvaluationDatasetVersionResponse],
)
async def list_dataset_versions(
    workspace_id: UUID,
    dataset_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> list[EvaluationDatasetVersionResponse]:
    del workspace_id
    versions = await EvaluationDatasetService().list_versions(
        session, context=context, dataset_id=dataset_id
    )
    return [
        EvaluationDatasetVersionResponse.model_validate(version, from_attributes=True)
        for version in versions
    ]


@router.get(
    "/datasets/{dataset_id}/versions/{version_id}",
    response_model=EvaluationDatasetVersionDetailResponse,
)
async def get_dataset_version(
    workspace_id: UUID,
    dataset_id: UUID,
    version_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> EvaluationDatasetVersionDetailResponse:
    del workspace_id
    service = EvaluationDatasetService()
    version = await service.get_version(
        session, context=context, dataset_id=dataset_id, version_id=version_id
    )
    items = await service.list_version_items(
        session, context=context, dataset_id=dataset_id, version_id=version_id
    )
    response = EvaluationDatasetVersionResponse.model_validate(version, from_attributes=True)
    return EvaluationDatasetVersionDetailResponse(
        **response.model_dump(),
        item_count=len(items),
        items=[
            EvaluationDatasetItemResponse.model_validate(item, from_attributes=True)
            for item in items
        ],
    )


@router.post(
    "/datasets/{dataset_id}/versions/{version_id}/publish",
    response_model=EvaluationDatasetVersionResponse,
)
async def publish_dataset_version(
    workspace_id: UUID,
    dataset_id: UUID,
    version_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> EvaluationDatasetVersionResponse:
    del workspace_id
    service = EvaluationDatasetService()
    version = await service.publish_version(
        session,
        context=context,
        dataset_id=dataset_id,
        version_id=version_id,
    )
    items = await service.list_version_items(
        session, context=context, dataset_id=dataset_id, version_id=version_id
    )
    response = EvaluationDatasetVersionResponse.model_validate(version, from_attributes=True)
    return response.model_copy(update={"item_count": len(items)})


@router.post(
    "/pricing-snapshots",
    response_model=PricingSnapshotResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_pricing_snapshot(
    workspace_id: UUID,
    payload: PricingSnapshotCreateRequest,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> PricingSnapshotResponse:
    del workspace_id
    snapshot = await EvaluationDatasetService().create_pricing_snapshot(
        session,
        context=context,
        name=payload.name,
        provider=payload.provider,
        model=payload.model,
        currency=payload.currency,
        input_price_per_1m=payload.input_price_per_1m,
        output_price_per_1m=payload.output_price_per_1m,
        cached_input_price_per_1m=payload.cached_input_price_per_1m,
        effective_at=payload.effective_at,
        source_note=payload.source_note,
    )
    return PricingSnapshotResponse.model_validate(snapshot, from_attributes=True)


@router.get("/pricing-snapshots", response_model=list[PricingSnapshotResponse])
async def list_pricing_snapshots(
    workspace_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> list[PricingSnapshotResponse]:
    del workspace_id
    snapshots = await EvaluationDatasetService().list_pricing_snapshots(session, context=context)
    return [
        PricingSnapshotResponse.model_validate(snapshot, from_attributes=True)
        for snapshot in snapshots
    ]


@router.post(
    "/experiments",
    response_model=EvaluationExperimentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_experiment(
    workspace_id: UUID,
    payload: EvaluationExperimentCreateRequest,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> EvaluationExperimentResponse:
    del workspace_id
    experiment = await ExperimentService().create_experiment(
        session,
        context=context,
        name=payload.name,
        description=payload.description,
        dataset_version_id=payload.dataset_version_id,
        split=payload.split,
        purpose=payload.purpose,
        repetitions=payload.repetitions,
    )
    return EvaluationExperimentResponse.model_validate(experiment, from_attributes=True)


@router.get("/experiments", response_model=list[EvaluationExperimentResponse])
async def list_experiments(
    workspace_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> list[EvaluationExperimentResponse]:
    del workspace_id
    experiments = await ExperimentService().list_experiments(session, context=context)
    return [
        EvaluationExperimentResponse.model_validate(item, from_attributes=True)
        for item in experiments
    ]


@router.get(
    "/experiments/{experiment_id}",
    response_model=EvaluationExperimentDetailResponse,
)
async def get_experiment(
    workspace_id: UUID,
    experiment_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> EvaluationExperimentDetailResponse:
    del workspace_id
    service = ExperimentService()
    experiment = await service.get_experiment(session, context=context, experiment_id=experiment_id)
    variants = await service.list_variants(session, context=context, experiment_id=experiment_id)
    count = await service.holdout_exposure_count(
        session, context=context, experiment_id=experiment_id
    )
    response = EvaluationExperimentResponse.model_validate(experiment, from_attributes=True)
    return EvaluationExperimentDetailResponse(
        **response.model_dump(exclude={"holdout_exposure_count"}),
        holdout_exposure_count=count,
        variants=[
            EvaluationExperimentVariantResponse.model_validate(item, from_attributes=True)
            for item in variants
        ],
    )


@router.post(
    "/experiments/{experiment_id}/variants",
    response_model=EvaluationExperimentVariantResponse,
    status_code=status.HTTP_201_CREATED,
)
async def add_experiment_variant(
    workspace_id: UUID,
    experiment_id: UUID,
    payload: EvaluationExperimentVariantCreateRequest,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> EvaluationExperimentVariantResponse:
    del workspace_id
    variant = await ExperimentService().add_variant(
        session,
        context=context,
        experiment_id=experiment_id,
        label=payload.label,
        agent_version_id=payload.agent_version_id,
        pricing_snapshot_id=payload.pricing_snapshot_id,
        ordinal=payload.ordinal,
        variant_metadata=payload.variant_metadata,
    )
    return EvaluationExperimentVariantResponse.model_validate(variant, from_attributes=True)


@router.get(
    "/experiments/{experiment_id}/variants",
    response_model=list[EvaluationExperimentVariantResponse],
)
async def list_experiment_variants(
    workspace_id: UUID,
    experiment_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> list[EvaluationExperimentVariantResponse]:
    del workspace_id
    variants = await ExperimentService().list_variants(
        session, context=context, experiment_id=experiment_id
    )
    return [
        EvaluationExperimentVariantResponse.model_validate(item, from_attributes=True)
        for item in variants
    ]


@router.post(
    "/experiments/{experiment_id}/finalize",
    response_model=EvaluationExperimentDetailResponse,
)
async def finalize_experiment(
    workspace_id: UUID,
    experiment_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> EvaluationExperimentDetailResponse:
    del workspace_id
    service = ExperimentService()
    experiment = await service.finalize_experiment(
        session, context=context, experiment_id=experiment_id
    )
    variants = await service.list_variants(session, context=context, experiment_id=experiment_id)
    count = await service.holdout_exposure_count(
        session, context=context, experiment_id=experiment_id
    )
    response = EvaluationExperimentResponse.model_validate(experiment, from_attributes=True)
    return EvaluationExperimentDetailResponse(
        **response.model_dump(exclude={"holdout_exposure_count"}),
        holdout_exposure_count=count,
        variants=[
            EvaluationExperimentVariantResponse.model_validate(item, from_attributes=True)
            for item in variants
        ],
    )


@router.post(
    "/experiments/{experiment_id}/runs",
    response_model=EvaluationExperimentRunResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_experiment_run(
    request: Request,
    workspace_id: UUID,
    experiment_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
    queue: ExperimentRunQueue = run_queue_dependency,
) -> EvaluationExperimentRunResponse:
    del workspace_id
    run, exposure_index = await ExperimentService().create_run(
        session, context=context, experiment_id=experiment_id
    )
    try:
        await queue.enqueue(run.id)
    except Exception:
        request.app.state.evaluation_enqueue_failures = (
            getattr(request.app.state, "evaluation_enqueue_failures", 0) + 1
        )
    response = EvaluationExperimentRunResponse.model_validate(run, from_attributes=True)
    return response.model_copy(update={"holdout_exposure_index": exposure_index})


@router.get(
    "/experiment-runs/{run_id}",
    response_model=EvaluationExperimentRunResponse,
)
async def get_experiment_run(
    workspace_id: UUID,
    run_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> EvaluationExperimentRunResponse:
    del workspace_id
    run, exposure_index = await ExperimentService().get_run(session, context=context, run_id=run_id)
    response = EvaluationExperimentRunResponse.model_validate(run, from_attributes=True)
    return response.model_copy(update={"holdout_exposure_index": exposure_index})


@router.post(
    "/experiment-runs/{run_id}/cancel",
    response_model=EvaluationExperimentRunResponse,
)
async def cancel_experiment_run(
    workspace_id: UUID,
    run_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> EvaluationExperimentRunResponse:
    del workspace_id
    run = await ExperimentService().request_run_cancel(session, context=context, run_id=run_id)
    return EvaluationExperimentRunResponse.model_validate(run, from_attributes=True)


@router.get(
    "/experiment-runs/{run_id}/progress",
    response_model=EvaluationExperimentRunProgressResponse,
)
async def get_experiment_run_progress(
    workspace_id: UUID,
    run_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> EvaluationExperimentRunProgressResponse:
    del workspace_id
    progress = await ExperimentService().run_progress(session, context=context, run_id=run_id)
    return EvaluationExperimentRunProgressResponse.model_validate(progress)


@router.post("/experiment-runs/{run_id}/metrics", response_model=dict[str, Any])
async def materialize_experiment_run_metrics(
    workspace_id: UUID,
    run_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> dict[str, Any]:
    del workspace_id
    return await EvaluationMetricsService().materialize_metrics(
        session, context=context, run_id=run_id
    )


@router.get("/experiment-runs/{run_id}/metrics", response_model=dict[str, Any])
async def get_experiment_run_metrics(
    workspace_id: UUID,
    run_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> dict[str, Any]:
    del workspace_id
    return await EvaluationMetricsService().get_persisted_metrics(
        session, context=context, run_id=run_id
    )


@router.post(
    "/experiment-runs/{run_id}/comparisons",
    response_model=EvaluationComparisonResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_experiment_comparison(
    workspace_id: UUID,
    run_id: UUID,
    payload: EvaluationComparisonCreateRequest,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> EvaluationComparisonResponse:
    del workspace_id
    comparison = await EvaluationMetricsService().create_comparison(
        session,
        context=context,
        run_id=run_id,
        baseline_variant_id=payload.baseline_variant_id,
        candidate_variant_id=payload.candidate_variant_id,
    )
    return EvaluationComparisonResponse.model_validate(comparison, from_attributes=True)


@router.get(
    "/experiment-runs/{run_id}/comparisons",
    response_model=list[EvaluationComparisonResponse],
)
async def list_experiment_comparisons(
    workspace_id: UUID,
    run_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> list[EvaluationComparisonResponse]:
    del workspace_id
    comparisons = await EvaluationMetricsService().get_comparisons(
        session, context=context, run_id=run_id
    )
    return [
        EvaluationComparisonResponse.model_validate(item, from_attributes=True)
        for item in comparisons
    ]


@router.get(
    "/experiment-runs/{run_id}/comparisons/{comparison_id}",
    response_model=EvaluationComparisonResponse,
)
async def get_experiment_comparison(
    workspace_id: UUID,
    run_id: UUID,
    comparison_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> EvaluationComparisonResponse:
    del workspace_id
    comparison = await EvaluationMetricsService().get_comparison(
        session,
        context=context,
        run_id=run_id,
        comparison_id=comparison_id,
    )
    return EvaluationComparisonResponse.model_validate(comparison, from_attributes=True)


@router.post(
    "/experiment-runs/{run_id}/comparisons/{comparison_id}/ablation",
    response_model=EvaluationAblationResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_experiment_ablation(
    workspace_id: UUID,
    run_id: UUID,
    comparison_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> EvaluationAblationResponse:
    del workspace_id
    result = await EvaluationAblationService().create_ablation(
        session, context=context, run_id=run_id, comparison_id=comparison_id
    )
    return EvaluationAblationResponse.model_validate(result, from_attributes=True)


@router.get(
    "/experiment-runs/{run_id}/comparisons/{comparison_id}/ablation",
    response_model=EvaluationAblationResponse,
)
async def get_experiment_ablation(
    workspace_id: UUID,
    run_id: UUID,
    comparison_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> EvaluationAblationResponse:
    del workspace_id
    result = await EvaluationAblationService().get_ablation(
        session, context=context, run_id=run_id, comparison_id=comparison_id
    )
    return EvaluationAblationResponse.model_validate(result, from_attributes=True)


@router.post(
    "/release-gate-policies",
    response_model=EvaluationReleaseGatePolicyResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_release_gate_policy(
    workspace_id: UUID,
    payload: EvaluationReleaseGatePolicyCreateRequest,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> EvaluationReleaseGatePolicyResponse:
    del workspace_id
    policy = await EvaluationReleaseGateService().create_policy(
        session,
        context=context,
        name=payload.name,
        description=payload.description,
        policy_json=payload.policy_json,
    )
    return EvaluationReleaseGatePolicyResponse.model_validate(policy, from_attributes=True)


@router.get(
    "/release-gate-policies",
    response_model=list[EvaluationReleaseGatePolicyResponse],
)
async def list_release_gate_policies(
    workspace_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> list[EvaluationReleaseGatePolicyResponse]:
    del workspace_id
    policies = await EvaluationReleaseGateService().list_policies(session, context=context)
    return [
        EvaluationReleaseGatePolicyResponse.model_validate(policy, from_attributes=True)
        for policy in policies
    ]


@router.get(
    "/release-gate-policies/{policy_id}",
    response_model=EvaluationReleaseGatePolicyResponse,
)
async def get_release_gate_policy(
    workspace_id: UUID,
    policy_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> EvaluationReleaseGatePolicyResponse:
    del workspace_id
    policy = await EvaluationReleaseGateService().get_policy(
        session, context=context, policy_id=policy_id
    )
    return EvaluationReleaseGatePolicyResponse.model_validate(policy, from_attributes=True)


@router.post(
    "/experiment-runs/{run_id}/comparisons/{comparison_id}/release-gates",
    response_model=EvaluationReleaseGateDecisionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_release_gate_decision(
    workspace_id: UUID,
    run_id: UUID,
    comparison_id: UUID,
    payload: EvaluationReleaseGateCreateRequest,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> EvaluationReleaseGateDecisionResponse:
    del workspace_id
    decision = await EvaluationReleaseGateService().create_decision(
        session,
        context=context,
        run_id=run_id,
        comparison_id=comparison_id,
        policy_id=payload.policy_id,
    )
    return EvaluationReleaseGateDecisionResponse.model_validate(decision, from_attributes=True)


@router.get(
    "/experiment-runs/{run_id}/comparisons/{comparison_id}/release-gates",
    response_model=list[EvaluationReleaseGateDecisionResponse],
)
async def list_release_gate_decisions(
    workspace_id: UUID,
    run_id: UUID,
    comparison_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> list[EvaluationReleaseGateDecisionResponse]:
    del workspace_id
    decisions = await EvaluationReleaseGateService().list_decisions(
        session, context=context, run_id=run_id, comparison_id=comparison_id
    )
    return [
        EvaluationReleaseGateDecisionResponse.model_validate(decision, from_attributes=True)
        for decision in decisions
    ]


@router.get(
    "/experiment-runs/{run_id}/comparisons/{comparison_id}/release-gates/{policy_id}",
    response_model=EvaluationReleaseGateDecisionResponse,
)
async def get_release_gate_decision(
    workspace_id: UUID,
    run_id: UUID,
    comparison_id: UUID,
    policy_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> EvaluationReleaseGateDecisionResponse:
    del workspace_id
    decision = await EvaluationReleaseGateService().get_decision(
        session,
        context=context,
        run_id=run_id,
        comparison_id=comparison_id,
        policy_id=policy_id,
    )
    return EvaluationReleaseGateDecisionResponse.model_validate(decision, from_attributes=True)


__all__ = ["router"]
