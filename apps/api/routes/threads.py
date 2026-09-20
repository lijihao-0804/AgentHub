from __future__ import annotations

import asyncio
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import StreamingResponse

from apps.api.agent_runtime_dependencies import get_production_agent_run_service
from apps.api.knowledge_dependencies import get_agent_run_workspace_context
from apps.api.schemas.threads import (
    ThreadCreateRequest,
    ThreadPatchRequest,
    ThreadResponse,
    ThreadTurnCreateRequest,
    ThreadTurnDetailResponse,
    ThreadTurnSubmitResponse,
)
from packages.agent_runtime.sse import event_to_sse
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.threads.service import ThreadService

PREFIX = "/api/v1/workspaces/{workspace_id}"

router = APIRouter(prefix=PREFIX, tags=["threads"])
context_dependency = Depends(get_agent_run_workspace_context)
agent_id_query = Query(default=None)
limit_query = Query(default=50, ge=1, le=200)
offset_query = Query(default=0, ge=0)


def _service(request: Request) -> ThreadService:
    run_service = get_production_agent_run_service(request)
    return ThreadService(run_service.session_factory, run_service=run_service)


@router.post(
    "/agents/{agent_id}/threads",
    response_model=ThreadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_thread(
    workspace_id: UUID,
    agent_id: UUID,
    payload: ThreadCreateRequest,
    request: Request,
    context: WorkspaceExecutionContext = context_dependency,
) -> ThreadResponse:
    del workspace_id
    thread = await _service(request).create_thread(context, agent_id=agent_id, title=payload.title)
    return ThreadResponse.model_validate(thread)


@router.get("/threads", response_model=list[ThreadResponse])
async def list_threads(
    workspace_id: UUID,
    request: Request,
    agent_id: UUID | None = agent_id_query,
    limit: int = limit_query,
    offset: int = offset_query,
    context: WorkspaceExecutionContext = context_dependency,
) -> list[ThreadResponse]:
    del workspace_id
    threads = await _service(request).list_threads(
        context, agent_id=agent_id, limit=limit, offset=offset
    )
    return [ThreadResponse.model_validate(thread) for thread in threads]


@router.get("/threads/{thread_id}", response_model=ThreadResponse)
async def get_thread(
    workspace_id: UUID,
    thread_id: UUID,
    request: Request,
    context: WorkspaceExecutionContext = context_dependency,
) -> ThreadResponse:
    del workspace_id
    return ThreadResponse.model_validate(await _service(request).get_thread(context, thread_id))


@router.patch("/threads/{thread_id}", response_model=ThreadResponse)
async def patch_thread(
    workspace_id: UUID,
    thread_id: UUID,
    payload: ThreadPatchRequest,
    request: Request,
    context: WorkspaceExecutionContext = context_dependency,
) -> ThreadResponse:
    del workspace_id
    thread = await _service(request).update_thread(context, thread_id, title=payload.title)
    return ThreadResponse.model_validate(thread)


@router.delete("/threads/{thread_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_thread(
    workspace_id: UUID,
    thread_id: UUID,
    request: Request,
    context: WorkspaceExecutionContext = context_dependency,
) -> Response:
    del workspace_id
    await _service(request).delete_thread(context, thread_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/threads/{thread_id}/turns", response_model=list[ThreadTurnDetailResponse])
async def list_turns(
    workspace_id: UUID,
    thread_id: UUID,
    request: Request,
    context: WorkspaceExecutionContext = context_dependency,
) -> list[ThreadTurnDetailResponse]:
    del workspace_id
    rows = await _service(request).turn_summaries(context, thread_id)
    return [
        ThreadTurnDetailResponse(
            id=turn.id,
            thread_id=turn.thread_id,
            sequence=turn.sequence,
            user_input=turn.user_input,
            agent_run_id=turn.agent_run_id,
            created_at=turn.created_at,
            status=run.status if run is not None else None,
            final_output=run.final_output if run is not None else None,
            failure_code=run.failure_code if run is not None else None,
        )
        for turn, run in rows
    ]


@router.post(
    "/threads/{thread_id}/turns",
    response_model=ThreadTurnSubmitResponse,
    status_code=status.HTTP_201_CREATED,
)
async def submit_turn(
    workspace_id: UUID,
    thread_id: UUID,
    payload: ThreadTurnCreateRequest,
    request: Request,
    context: WorkspaceExecutionContext = context_dependency,
) -> ThreadTurnSubmitResponse:
    del workspace_id
    submitted = await _service(request).submit_turn(
        context,
        thread_id,
        user_input=payload.input_text,
        client_token=payload.client_token,
    )
    return ThreadTurnSubmitResponse(
        turn=submitted.turn,
        run_id=submitted.run_id,
        agent_version_id=submitted.agent_version_id,
        reused=submitted.reused,
    )


@router.post("/threads/{thread_id}/turns/stream", response_class=StreamingResponse)
async def stream_turn(
    workspace_id: UUID,
    thread_id: UUID,
    payload: ThreadTurnCreateRequest,
    request: Request,
    context: WorkspaceExecutionContext = context_dependency,
) -> StreamingResponse:
    """Submit a turn and stream the run that answers it.

    The run itself goes down the existing streaming path unchanged; the only
    addition is recording the turn first and linking the run to it, so a
    reader of the thread can still open ``Run #xxx``.
    """

    del workspace_id
    service = _service(request)
    run_service = get_production_agent_run_service(request)
    thread, agent_version_id = await service.resolve_agent_version(context, thread_id)
    turn = await service.open_turn(
        context,
        thread_id,
        user_input=payload.input_text,
        client_token=payload.client_token,
    )
    if turn is None:
        # A repeated client_token. The caller already has a run; it is not
        # started a second time, and the stream is simply empty.
        async def empty():
            yield ": duplicate\n\n"

        return StreamingResponse(
            empty(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    prepared_run = await run_service.prepare_stream(
        context,
        agent_version_id=agent_version_id,
        input_text=turn.user_input,
        thread_id=thread.id,
    )
    await service.attach_run(context, turn_id=turn.id, run_id=prepared_run.id)

    async def frames():
        stream = run_service.stream(
            context,
            agent_version_id=agent_version_id,
            input_text=turn.user_input,
            prepared_run=prepared_run,
        )
        iterator = stream.__aiter__()
        pending = asyncio.create_task(iterator.__anext__())
        try:
            while True:
                done, _ = await asyncio.wait(
                    {pending}, timeout=request.app.state.settings.sse_heartbeat_seconds
                )
                if not done:
                    yield ": heartbeat\n\n"
                    continue
                try:
                    event = pending.result()
                except StopAsyncIteration:
                    break
                yield event_to_sse(event)
                pending = asyncio.create_task(iterator.__anext__())
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await stream.aclose()

    return StreamingResponse(
        frames(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            # The run id before the first frame, so the UI can offer
            # `View Run` while the answer is still streaming.
            "X-AgentHub-Run-Id": str(prepared_run.id),
            "X-AgentHub-Turn-Id": str(turn.id),
        },
    )


__all__ = ["router"]
