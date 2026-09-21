from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.dependencies import get_db_session
from apps.api.knowledge_dependencies import (
    get_model_gateway,
    get_query_workspace_context,
    get_retrieval_components,
)
from apps.api.retrieval_projection import project_retrieval_trace
from apps.api.schemas.citation_qa import (
    CitationQaCitationResponse,
    CitationQaRequest,
    CitationQaResponse,
    CitationQaRetrievalTraceResponse,
)
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.knowledge.citation_qa import CitationQaResult, CitationQaService
from packages.knowledge.composition import RetrievalComponents
from packages.model_gateway.contracts import ModelGateway
from packages.observability import ProductionTraceSink

router = APIRouter(tags=["knowledge"])
db_session_dependency = Depends(get_db_session)
context_dependency = Depends(get_query_workspace_context)
retrieval_components_dependency = Depends(get_retrieval_components)
model_gateway_dependency = Depends(get_model_gateway)


@router.post("/api/v1/knowledge/query", response_model=CitationQaResponse)
async def citation_qa(
    payload: CitationQaRequest,
    request: Request,
    context: WorkspaceExecutionContext = context_dependency,
    session: AsyncSession = db_session_dependency,
    components: RetrievalComponents = retrieval_components_dependency,
    model_gateway: ModelGateway = model_gateway_dependency,
) -> CitationQaResponse:
    result = await CitationQaService(
        session=session,
        retrieval_components=components,
        model_gateway=model_gateway,
        max_evidence_chars=request.app.state.settings.knowledge_qa_max_evidence_chars,
        min_rerank_score=request.app.state.settings.knowledge_min_rerank_score,
        superseded_rank_penalty=request.app.state.settings.knowledge_superseded_rank_penalty,
        trace_sink=ProductionTraceSink(),
    ).answer(
        context=context,
        knowledge_base_id=payload.knowledge_base_id,
        knowledge_snapshot_id=payload.knowledge_snapshot_id,
        model_profile_id=payload.model_profile_id,
        query=payload.query,
    )
    return _response(result)


def _response(result: CitationQaResult) -> CitationQaResponse:
    return CitationQaResponse(
        answer=result.answer,
        citations=[
            CitationQaCitationResponse(
                id=item.id,
                document_id=item.document_id,
                document_revision_id=item.document_revision_id,
                chunk_id=item.chunk_id,
                source=item.source,
                locator=item.locator,
                excerpt=item.excerpt,
                retrieval_score=item.retrieval_score,
                rerank_score=item.rerank_score,
            )
            for item in result.citations
        ],
        retrieval_trace=CitationQaRetrievalTraceResponse(
            snapshot_id=result.retrieval_trace.snapshot_id,
            stages=project_retrieval_trace(result.retrieval_trace),
            total_latency_ms=result.retrieval_trace.total_latency_ms,
        ),
    )


__all__ = ["router"]
