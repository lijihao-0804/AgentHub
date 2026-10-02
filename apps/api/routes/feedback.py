from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.dependencies import get_db_session
from apps.api.knowledge_dependencies import get_workspace_context
from apps.api.schemas.evaluation import EvaluationDatasetVersionResponse
from apps.api.schemas.feedback import (
    FeedbackCreate,
    FeedbackImport,
    FeedbackResponse,
    FeedbackReview,
)
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.feedback.service import FeedbackService

router = APIRouter(prefix="/api/v1/workspaces/{workspace_id}", tags=["feedback"])
context_dependency = Depends(get_workspace_context)
session_dependency = Depends(get_db_session)


@router.post("/runs/{run_id}/feedback", response_model=FeedbackResponse, status_code=201)
async def submit_feedback(
    workspace_id: UUID,
    run_id: UUID,
    payload: FeedbackCreate,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = session_dependency,
):
    return await FeedbackService().submit(session, context, run_id=run_id, **payload.model_dump())


@router.get("/runs/{run_id}/feedback", response_model=list[FeedbackResponse])
async def list_feedback(
    workspace_id: UUID,
    run_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = session_dependency,
):
    return await FeedbackService().list_for_run(session, context, run_id)


@router.post("/feedback/{feedback_id}/review", response_model=FeedbackResponse)
async def review_feedback(
    workspace_id: UUID,
    feedback_id: UUID,
    payload: FeedbackReview,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = session_dependency,
):
    return await FeedbackService().review(session, context, feedback_id, **payload.model_dump())


@router.post("/feedback/{feedback_id}/import-dev", response_model=EvaluationDatasetVersionResponse)
async def import_feedback(
    workspace_id: UUID,
    feedback_id: UUID,
    payload: FeedbackImport,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = session_dependency,
):
    return await FeedbackService().import_dev(session, context, feedback_id, **payload.model_dump())
