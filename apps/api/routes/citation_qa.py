from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from apps.api.dependencies import get_db_session_factory
from apps.api.knowledge_dependencies import (
    get_citation_qa_generator,
    get_citation_qa_model_plan_resolver,
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
from packages.model_gateway.contracts import ResolvedModelGenerator
from packages.observability import ProductionTraceSink

router = APIRouter(tags=["knowledge"])
context_dependency = Depends(get_query_workspace_context)
retrieval_components_dependency = Depends(get_retrieval_components)
model_gateway_dependency = Depends(get_citation_qa_generator)
model_plan_resolver_dependency = Depends(get_citation_qa_model_plan_resolver)


@router.post("/api/v1/knowledge/query", response_model=CitationQaResponse)
async def citation_qa(
    payload: CitationQaRequest,
    request: Request,
    context: WorkspaceExecutionContext = context_dependency,
    components: RetrievalComponents = retrieval_components_dependency,
    model_gateway: ResolvedModelGenerator = model_gateway_dependency,
    model_plan_resolver=model_plan_resolver_dependency,
) -> CitationQaResponse:
    result = await CitationQaService(
        session_factory=get_db_session_factory(request),
        retrieval_components=components,
        model_gateway=model_gateway,
        output_mode=payload.output_mode,
        verify_refusal_consistency=request.app.state.settings.knowledge_qa_contextual_abstention,
        answer_contract_v1=request.app.state.settings.knowledge_qa_answer_contract_v1,
        question_language_prompt=request.app.state.settings.knowledge_qa_question_language_prompt,
        max_evidence_chars=request.app.state.settings.knowledge_qa_max_evidence_chars,
        min_rerank_score=request.app.state.settings.knowledge_min_rerank_score,
        superseded_rank_penalty=request.app.state.settings.knowledge_superseded_rank_penalty,
        trace_sink=ProductionTraceSink(),
        model_plan_resolver=model_plan_resolver,
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
        insufficient_evidence=result.insufficient_evidence,
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
