from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.dependencies import get_db_session
from apps.api.knowledge_dependencies import get_workspace_context
from apps.api.schemas.evaluation import (
    EvaluationDatasetCreateRequest,
    EvaluationDatasetItemResponse,
    EvaluationDatasetResponse,
    EvaluationDatasetVersionCreateRequest,
    EvaluationDatasetVersionDetailResponse,
    EvaluationDatasetVersionResponse,
    EvaluationExperimentCreateRequest,
    EvaluationExperimentDetailResponse,
    EvaluationExperimentResponse,
    EvaluationExperimentRunResponse,
    EvaluationExperimentVariantCreateRequest,
    EvaluationExperimentVariantResponse,
    PricingSnapshotCreateRequest,
    PricingSnapshotResponse,
)
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.evaluation.experiments import ExperimentService
from packages.evaluation.service import EvaluationDatasetService

router = APIRouter(prefix="/api/v1/workspaces/{workspace_id}/evaluation", tags=["evaluation"])
context_dependency = Depends(get_workspace_context)
db_session_dependency = Depends(get_db_session)


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
    snapshots = await EvaluationDatasetService().list_pricing_snapshots(
        session, context=context
    )
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
    experiment = await service.get_experiment(
        session, context=context, experiment_id=experiment_id
    )
    variants = await service.list_variants(
        session, context=context, experiment_id=experiment_id
    )
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
    variants = await service.list_variants(
        session, context=context, experiment_id=experiment_id
    )
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
    workspace_id: UUID,
    experiment_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> EvaluationExperimentRunResponse:
    del workspace_id
    run, exposure_index = await ExperimentService().create_run(
        session, context=context, experiment_id=experiment_id
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
    run, exposure_index = await ExperimentService().get_run(
        session, context=context, run_id=run_id
    )
    response = EvaluationExperimentRunResponse.model_validate(run, from_attributes=True)
    return response.model_copy(update={"holdout_exposure_index": exposure_index})


__all__ = ["router"]
