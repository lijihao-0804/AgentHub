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
    PricingSnapshotCreateRequest,
    PricingSnapshotResponse,
)
from packages.core.execution_context.models import WorkspaceExecutionContext
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


__all__ = ["router"]
