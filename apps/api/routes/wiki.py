"""Wiki maintenance and evidence query endpoints; existing workspace RBAC applies."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apps.api.dependencies import get_db_session
from apps.api.knowledge_dependencies import (
    get_model_gateway,
    get_retrieval_components,
    get_workspace_context,
)
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.knowledge.retrieval import HybridKnowledgeRetriever
from packages.model_gateway.contracts import ModelGateway
from packages.wiki.models import WikiCompilation
from packages.wiki.service import WikiService, require, resolve_model_plan

router = APIRouter(
    prefix="/api/v1/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/wiki",
    tags=["wiki"],
)
db = Depends(get_db_session)
ctx = Depends(get_workspace_context)
gateway_dependency = Depends(get_model_gateway)


class CompileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_profile_id: UUID
    review_model_profile_id: UUID | None = None
    review_mode: Literal["BASIC", "MODEL"] | None = None
    manual_conflict_groups: list[list[dict[str, str]]] = Field(default_factory=list, max_length=50)
    source_snapshot_id: UUID | Literal["LATEST"] = "LATEST"
    output_mode: Literal["PROMPT_JSON", "JSON_SCHEMA"] = "PROMPT_JSON"


class WikiQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_snapshot_id: UUID
    query: str = Field(min_length=1, max_length=4000)
    mode: Literal["AUTO", "WIKI", "RAG", "HYBRID"] = "AUTO"
    intent: Literal["overview", "fact", "synthesis"] = "overview"
    require_current: bool = True
    model_profile_id: UUID | None = None


@router.post("/sources/{revision_id}/revoke")
async def revoke_source(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    revision_id: UUID,
    context: WorkspaceExecutionContext = ctx,
    session: AsyncSession = db,
):
    await WikiService().revoke_source(session, context, knowledge_base_id, revision_id)
    return {"document_revision_id": revision_id, "scope": "WIKI", "status": "REVOKED"}


def compilation_response(row):
    return {
        k: getattr(row, k)
        for k in (
            "id",
            "source_snapshot_id",
            "source_snapshot_hash",
            "identity_hash",
            "compiler_spec",
            "status",
            "attempt_count",
            "error_code",
            "content_hash",
            "telemetry",
            "created_at",
            "published_at",
            "published_by",
        )
    }


def enqueue(request, row):
    if row.status not in {"PENDING", "PROCESSING"}:
        return
    # The durable job precedes broker delivery. Beat recovers a lost enqueue.
    try:
        from apps.worker.tasks.wiki import compile_wiki

        compile_wiki.delay(str(row.id))
    except Exception:
        pass


@router.post("/compilations", status_code=202)
async def compile_source(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    payload: CompileRequest,
    request: Request,
    context: WorkspaceExecutionContext = ctx,
    session: AsyncSession = db,
):
    row = await WikiService().request(
        session,
        context,
        knowledge_base_id,
        payload.model_profile_id,
        payload.source_snapshot_id,
        output_mode=payload.output_mode,
        review_model_profile_id=payload.review_model_profile_id,
        review_mode=payload.review_mode,
        manual_conflict_groups=payload.manual_conflict_groups,
    )
    enqueue(request, row)
    return compilation_response(row)


@router.get("/compilations")
async def compilations(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    context: WorkspaceExecutionContext = ctx,
    session: AsyncSession = db,
):
    require(context, "knowledge_run")
    rows = await session.scalars(
        select(WikiCompilation)
        .where(
            WikiCompilation.workspace_id == UUID(context.workspace_id),
            WikiCompilation.knowledge_base_id == knowledge_base_id,
        )
        .order_by(WikiCompilation.created_at.desc(), WikiCompilation.id)
        .limit(100)
    )
    return [compilation_response(r) for r in rows]


@router.get("/compilations/{compilation_id}")
async def compilation(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    compilation_id: UUID,
    context: WorkspaceExecutionContext = ctx,
    session: AsyncSession = db,
):
    return compilation_response(
        await WikiService().get(session, context, knowledge_base_id, compilation_id)
    )


@router.get("/compilations/{compilation_id}/pages")
async def pages(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    compilation_id: UUID,
    context: WorkspaceExecutionContext = ctx,
    session: AsyncSession = db,
):
    return await WikiService().pages(session, context, knowledge_base_id, compilation_id)


@router.get("/compilations/{compilation_id}/lint")
async def lint(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    compilation_id: UUID,
    context: WorkspaceExecutionContext = ctx,
    session: AsyncSession = db,
):
    return await WikiService().lint(session, context, knowledge_base_id, compilation_id)


@router.post("/compilations/{compilation_id}/publish")
async def publish(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    compilation_id: UUID,
    context: WorkspaceExecutionContext = ctx,
    session: AsyncSession = db,
):
    return compilation_response(
        await WikiService().publish(session, context, knowledge_base_id, compilation_id)
    )


@router.post("/compilations/{compilation_id}/retry", status_code=202)
async def retry(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    compilation_id: UUID,
    request: Request,
    context: WorkspaceExecutionContext = ctx,
    session: AsyncSession = db,
):
    await WikiService().retry(session, context, knowledge_base_id, compilation_id)
    row = await WikiService().get(session, context, knowledge_base_id, compilation_id)
    enqueue(request, row)
    return compilation_response(row)


@router.post("/compilations/{compilation_id}/query")
async def query(
    workspace_id: UUID,
    knowledge_base_id: UUID,
    compilation_id: UUID,
    payload: WikiQueryRequest,
    request: Request,
    context: WorkspaceExecutionContext = ctx,
    session: AsyncSession = db,
    gateway: ModelGateway = gateway_dependency,
):
    from packages.wiki.query import query_wiki

    class LazyRawRetriever:
        async def retrieve(self, context, query):
            components = await get_retrieval_components(request)
            settings = request.app.state.settings
            return await HybridKnowledgeRetriever(
                session=session,
                dense_embedder=components.dense,
                sparse_encoder=components.sparse,
                reranker=components.reranker,
                vector_index=components.index,
                rrf_k=settings.knowledge_rrf_k,
                superseded_rank_penalty=settings.knowledge_superseded_rank_penalty,
                min_rerank_score=settings.knowledge_min_rerank_score,
            ).retrieve(context, query)

    result = await query_wiki(
        session,
        context,
        knowledge_base_id,
        compilation_id,
        payload.source_snapshot_id,
        payload.query,
        mode=payload.mode,
        intent=payload.intent,
        raw_retriever=LazyRawRetriever(),
        require_current=payload.require_current,
    )
    if payload.model_profile_id is not None:
        from packages.wiki.answer import answer_from_evidence

        plan = await resolve_model_plan(session, context, payload.model_profile_id)
        result["generated_answer"] = await answer_from_evidence(
            gateway, plan, context, payload.query, result
        )
    return result
