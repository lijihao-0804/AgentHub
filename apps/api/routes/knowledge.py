from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, File, Request, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.dependencies import get_db_session
from apps.api.knowledge_dependencies import (
    get_ingestion_queue,
    get_retrieval_components,
    get_workspace_context,
)
from apps.api.retrieval_projection import RetrievalTraceMetadata, project_retrieval_trace
from apps.api.schemas.knowledge import (
    DocumentLifecycleUpdateRequest,
    DocumentResponse,
    DocumentRevisionResponse,
    DocumentRevisionStatusResponse,
    DocumentUploadResponse,
    IngestionJobResponse,
    KnowledgeBaseCreateRequest,
    KnowledgeBaseResponse,
    KnowledgeSnapshotResponse,
    RetrievalPlaygroundEvidence,
    RetrievalPlaygroundRequest,
    RetrievalPlaygroundResponse,
)
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.knowledge.blob_store import LocalBlobStore
from packages.knowledge.composition import RetrievalComponents
from packages.knowledge.contracts import RetrievalQuery, RetrievalResult
from packages.knowledge.models import DocumentChunk, DocumentRevision, KnowledgeSnapshotItem
from packages.knowledge.queue import IngestionQueue
from packages.knowledge.retrieval import HybridKnowledgeRetriever
from packages.knowledge.services import KnowledgeService
from packages.knowledge.snapshots import KnowledgeSnapshotService
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
queue_dependency = Depends(get_ingestion_queue)
retrieval_components_dependency = Depends(get_retrieval_components)
upload_file_dependency = File(...)


async def _load_trace_metadata(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    knowledge_base_id: UUID,
    snapshot_id: UUID,
    chunk_ids: set[str],
) -> dict[str, RetrievalTraceMetadata]:
    if not chunk_ids:
        return {}
    revision_ids = tuple(
        await session.scalars(
            select(KnowledgeSnapshotItem.document_revision_id).where(
                KnowledgeSnapshotItem.workspace_id == workspace_id,
                KnowledgeSnapshotItem.knowledge_base_id == knowledge_base_id,
                KnowledgeSnapshotItem.snapshot_id == snapshot_id,
            )
        )
    )
    if not revision_ids:
        return {}
    result = await session.execute(
        select(DocumentChunk, DocumentRevision)
        .join(
            DocumentRevision,
            (DocumentRevision.id == DocumentChunk.document_revision_id)
            & (DocumentRevision.workspace_id == DocumentChunk.workspace_id)
            & (DocumentRevision.knowledge_base_id == DocumentChunk.knowledge_base_id),
        )
        .where(
            DocumentChunk.workspace_id == workspace_id,
            DocumentChunk.knowledge_base_id == knowledge_base_id,
            DocumentChunk.document_revision_id.in_(revision_ids),
            DocumentChunk.chunk_id.in_(chunk_ids),
        )
    )
    return {
        chunk.chunk_id: RetrievalTraceMetadata(
            document_revision_id=str(revision.id),
            locator=dict(chunk.locator),
        )
        for chunk, revision in result
    }


def _playground_response(
    result: RetrievalResult,
    *,
    metadata: dict[str, RetrievalTraceMetadata],
) -> RetrievalPlaygroundResponse:
    trace = result.trace
    return RetrievalPlaygroundResponse(
        snapshot_id=trace.snapshot_id,
        evidence=[
            RetrievalPlaygroundEvidence(
                chunk_id=item.chunk_id,
                document_id=item.document_id,
                document_revision_id=item.document_revision_id,
                source=item.source,
                locator=item.locator,
                retrieval_score=item.retrieval_score,
                rerank_score=item.rerank_score,
                snippet=item.text[:1_000],
            )
            for item in result.evidence
        ],
        stages=project_retrieval_trace(trace, metadata),
        total_latency_ms=trace.total_latency_ms,
    )


@router.post(
    "/api/v1/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/retrieval/playground",
    response_model=RetrievalPlaygroundResponse,
)
async def retrieval_playground(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    payload: RetrievalPlaygroundRequest,
    request: Request,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
    components: RetrievalComponents = retrieval_components_dependency,
) -> RetrievalPlaygroundResponse:
    if not payload.query.strip():
        raise AgentHubError("INVALID_RETRIEVAL_QUERY", "The query must not be blank.", 400)
    result = await HybridKnowledgeRetriever(
        session=session,
        dense_embedder=components.dense,
        sparse_encoder=components.sparse,
        reranker=components.reranker,
        vector_index=components.index,
        rrf_k=request.app.state.settings.knowledge_rrf_k,
        superseded_rank_penalty=request.app.state.settings.knowledge_superseded_rank_penalty,
        min_rerank_score=request.app.state.settings.knowledge_min_rerank_score,
    ).retrieve_with_trace(
        context,
        RetrievalQuery(
            text=payload.query,
            knowledge_base_id=str(knowledge_base_id),
            knowledge_snapshot_id=str(payload.knowledge_snapshot_id),
            dense_top_k=payload.dense_top_k,
            sparse_top_k=payload.sparse_top_k,
            candidate_top_k=payload.candidate_top_k,
            final_top_k=payload.final_top_k,
            min_rerank_score=payload.min_rerank_score,
        ),
    )
    metadata = await _load_trace_metadata(
        session,
        workspace_id=workspace_id,
        knowledge_base_id=knowledge_base_id,
        snapshot_id=payload.knowledge_snapshot_id,
        chunk_ids={
            item.chunk_id
            for stage in (
                result.trace.dense,
                result.trace.sparse,
                result.trace.fusion,
                result.trace.rerank,
            )
            for item in stage.results
        },
    )
    return _playground_response(result, metadata=metadata)


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


@router.post(
    "/api/v1/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/snapshots",
    response_model=KnowledgeSnapshotResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_knowledge_snapshot(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> KnowledgeSnapshotResponse:
    del workspace_id
    snapshot = await KnowledgeSnapshotService().create_current_snapshot(
        session,
        context,
        knowledge_base_id,
    )
    return KnowledgeSnapshotResponse(
        id=snapshot.snapshot_id,
        workspace_id=snapshot.workspace_id,
        knowledge_base_id=snapshot.knowledge_base_id,
        content_hash=snapshot.content_hash,
        snapshot_schema_version=snapshot.snapshot_schema_version,
        item_count=snapshot.item_count,
        created_at=snapshot.created_at,
    )


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
    queue: IngestionQueue = queue_dependency,
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
        queue=queue,
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
    queue: IngestionQueue = queue_dependency,
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
        queue=queue,
    )
    return _upload_response(document, revision, job)


@router.patch(
    "/api/v1/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/documents/{document_id}",
    response_model=DocumentResponse,
)
async def update_document_lifecycle(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    document_id: UUID,
    payload: DocumentLifecycleUpdateRequest,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
) -> DocumentResponse:
    del workspace_id
    document = await KnowledgeService().update_document_lifecycle(
        session,
        context=context,
        knowledge_base_id=knowledge_base_id,
        document_id=document_id,
        effective_date=payload.effective_date,
        superseded_by_document_id=payload.superseded_by_document_id,
        set_effective_date="effective_date" in payload.model_fields_set,
        set_superseded_by="superseded_by_document_id" in payload.model_fields_set,
    )
    return DocumentResponse.model_validate(document, from_attributes=True)


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
