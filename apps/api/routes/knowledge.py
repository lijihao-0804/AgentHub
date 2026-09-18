from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, File, Request, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.dependencies import get_db_session
from apps.api.knowledge_dependencies import get_workspace_context
from apps.api.schemas.knowledge import (
    DocumentResponse,
    DocumentRevisionResponse,
    DocumentRevisionStatusResponse,
    DocumentUploadResponse,
    IngestionJobResponse,
    KnowledgeBaseCreateRequest,
    KnowledgeBaseResponse,
)
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.knowledge.blob_store import LocalBlobStore
from packages.knowledge.services import KnowledgeService
from packages.knowledge.upload_security import (
    UploadSecurityError,
    UploadSecurityPolicy,
    enforce_stream_size,
    validate_content_signature,
    validate_declared_media_type,
    validate_original_filename,
)

router = APIRouter(tags=["knowledge"])
db_session_dependency = Depends(get_db_session)
context_dependency = Depends(get_workspace_context)
upload_file_dependency = File(...)


@router.post(
    "/api/v1/workspaces/{workspace_id}/knowledge-bases",
    response_model=KnowledgeBaseResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_knowledge_base(
    workspace_id: UUID,
    payload: KnowledgeBaseCreateRequest,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> KnowledgeBaseResponse:
    del workspace_id
    knowledge_base = await KnowledgeService().create_knowledge_base(
        session, context=context, name=payload.name
    )
    return KnowledgeBaseResponse.model_validate(knowledge_base, from_attributes=True)


@router.get(
    "/api/v1/workspaces/{workspace_id}/knowledge-bases",
    response_model=list[KnowledgeBaseResponse],
)
async def list_knowledge_bases(
    workspace_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> list[KnowledgeBaseResponse]:
    del workspace_id
    knowledge_bases = await KnowledgeService().list_knowledge_bases(session, context=context)
    return [
        KnowledgeBaseResponse.model_validate(item, from_attributes=True)
        for item in knowledge_bases
    ]


async def _guarded_chunks(
    file: UploadFile,
    first_chunk: bytes,
    policy: UploadSecurityPolicy,
) -> AsyncIterator[bytes]:
    total = 0
    chunk = first_chunk
    while chunk:
        total += len(chunk)
        enforce_stream_size(total, policy)
        yield chunk
        chunk = await file.read(1024 * 1024)


def _upload_security_error(exc: UploadSecurityError) -> AgentHubError:
    return AgentHubError(exc.code, exc.message, 422)


@router.post(
    "/api/v1/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/documents",
    response_model=DocumentUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_document(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    request: Request,
    file: UploadFile = upload_file_dependency,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> DocumentUploadResponse:
    del workspace_id
    settings = request.app.state.settings
    policy = UploadSecurityPolicy(max_file_size_bytes=settings.knowledge_max_upload_bytes)
    try:
        filename = validate_original_filename(file.filename)
        media_type = validate_declared_media_type(filename, file.content_type)
        first_chunk = await file.read(64 * 1024)
        validate_content_signature(filename, first_chunk)
    except UploadSecurityError as exc:
        raise _upload_security_error(exc) from None

    document, revision, job = await KnowledgeService().upload_document(
        session,
        context=context,
        knowledge_base_id=knowledge_base_id,
        filename=filename,
        media_type=media_type,
        chunks=_guarded_chunks(file, first_chunk, policy),
        blob_store=LocalBlobStore(settings.blob_root),
    )
    return _upload_response(document, revision, job)


@router.post(
    "/api/v1/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/documents/{document_id}/revisions",
    response_model=DocumentUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_revision(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    document_id: UUID,
    request: Request,
    file: UploadFile = upload_file_dependency,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> DocumentUploadResponse:
    del workspace_id
    settings = request.app.state.settings
    policy = UploadSecurityPolicy(max_file_size_bytes=settings.knowledge_max_upload_bytes)
    try:
        filename = validate_original_filename(file.filename)
        media_type = validate_declared_media_type(filename, file.content_type)
        first_chunk = await file.read(64 * 1024)
        validate_content_signature(filename, first_chunk)
    except UploadSecurityError as exc:
        raise _upload_security_error(exc) from None

    document, revision, job = await KnowledgeService().upload_document(
        session,
        context=context,
        knowledge_base_id=knowledge_base_id,
        filename=filename,
        media_type=media_type,
        chunks=_guarded_chunks(file, first_chunk, policy),
        document_id=document_id,
        blob_store=LocalBlobStore(settings.blob_root),
    )
    return _upload_response(document, revision, job)


@router.get(
    "/api/v1/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/documents/{document_id}/revisions",
    response_model=list[DocumentRevisionStatusResponse],
)
async def list_revisions(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    document_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> list[DocumentRevisionStatusResponse]:
    del workspace_id
    records = await KnowledgeService().list_revisions(
        session,
        context=context,
        knowledge_base_id=knowledge_base_id,
        document_id=document_id,
    )
    return [
        DocumentRevisionStatusResponse(
            revision=DocumentRevisionResponse.model_validate(revision, from_attributes=True),
            ingestion_job=IngestionJobResponse.model_validate(job, from_attributes=True),
        )
        for revision, job in records
    ]


def _upload_response(document, revision, job) -> DocumentUploadResponse:
    document_response = (
        DocumentResponse.model_validate(document, from_attributes=True)
        if document is not None
        else None
    )
    if document_response is None:
        raise AgentHubError("INTERNAL_ERROR", "The document response could not be built.", 500)
    return DocumentUploadResponse(
        document=document_response,
        revision=DocumentRevisionResponse.model_validate(revision, from_attributes=True),
        ingestion_job=IngestionJobResponse.model_validate(job, from_attributes=True),
    )
