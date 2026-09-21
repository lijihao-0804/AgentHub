from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import StreamingResponse

from apps.api.agent_runtime_dependencies import get_production_agent_run_service
from apps.api.knowledge_dependencies import (
    get_agent_run_queue,
    get_agent_run_workspace_context,
)
from apps.api.schemas.agent_runs import (
    AgentRunCreateRequest,
    AgentRunResponse,
    RunStepResponse,
)
from apps.api.schemas.approvals import ApprovalResponse
from packages.agent_runtime.events import AgentEvent
from packages.agent_runtime.queue import AgentRunQueue
from packages.agent_runtime.runtime import AgentRunService
from packages.agent_runtime.sse import event_to_sse
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext

router = APIRouter(prefix="/api/v1/workspaces/{workspace_id}", tags=["agent-runs"])
context_dependency = Depends(get_agent_run_workspace_context)
agent_run_queue_dependency = Depends(get_agent_run_queue)


def _service(request: Request) -> AgentRunService:
    return get_production_agent_run_service(request)


async def _sse_frames(stream: AsyncIterator[AgentEvent], request: Request):
    """Serialize an event stream as SSE, keeping the connection warm.

    Closing this generator -- which is what a client disconnect does -- closes
    the subscription, not the run. Whether the run then survives is the stream
    hub's decision, and it only aborts once nobody has come back for it.
    """

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


def _approval_service(request: Request):
    service = _service(request)
    if service.approval_service is None:
        raise AgentHubError(
            "DATABASE_NOT_CONFIGURED", "Database access is not configured.", 503
        )
    return service.approval_service


@router.post(
    "/agent-versions/{agent_version_id}/runs",
    response_model=AgentRunResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_agent_run(
    workspace_id: UUID,
    agent_version_id: UUID,
    payload: AgentRunCreateRequest,
    request: Request,
    context: WorkspaceExecutionContext = context_dependency,
) -> AgentRunResponse:
    del workspace_id
    service = _service(request)
    run = await service.run(
        context,
        agent_version_id=agent_version_id,
        input_text=payload.input_text,
    )
    return AgentRunResponse.model_validate(await service.get_run(context, run.run_id))


@router.post(
    "/agent-versions/{agent_version_id}/runs/stream",
    response_class=StreamingResponse,
)
async def stream_agent_run(
    workspace_id: UUID,
    agent_version_id: UUID,
    payload: AgentRunCreateRequest,
    request: Request,
    context: WorkspaceExecutionContext = context_dependency,
    queue: AgentRunQueue = agent_run_queue_dependency,
) -> StreamingResponse:
    del workspace_id
    service = _service(request)
    prepared_run = await service.prepare_stream(
        context, agent_version_id=agent_version_id, input_text=payload.input_text
    )

    if request.app.state.settings.run_execution_in_worker:
        # The run is handed to the worker and this request becomes just another
        # follower of it -- so the response it serves is the same replayable
        # stream a reconnecting client would get, not a privileged one.
        await queue.enqueue(
            workspace_id=UUID(context.workspace_id),
            run_id=prepared_run.id,
            request_id=context.request_id,
        )
        stream = service.attach_stream(context, run_id=prepared_run.id)
    else:
        stream = service.stream(
            context,
            agent_version_id=agent_version_id,
            input_text=payload.input_text,
            prepared_run=prepared_run,
        )
    return StreamingResponse(
        _sse_frames(stream, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get(
    "/agent-runs/{run_id}/stream",
    response_class=StreamingResponse,
)
async def follow_agent_run(
    workspace_id: UUID,
    run_id: UUID,
    request: Request,
    after_sequence: int = Query(default=0, ge=0),
    context: WorkspaceExecutionContext = context_dependency,
) -> StreamingResponse:
    """Attach to a run this connection did not start.

    The POST endpoint starts a run; this one follows it. A client that drops
    its connection reconnects here with the last ``sequence`` it saw and
    receives everything it missed from the durable event log before rejoining
    the live stream, so a reload or a network blip costs frames, not the run.
    """

    del workspace_id
    service = _service(request)
    stream = service.attach_stream(context, run_id=run_id, after_sequence=after_sequence)
    return StreamingResponse(
        _sse_frames(stream, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/agent-runs/{run_id}", response_model=AgentRunResponse)
async def get_agent_run(
    workspace_id: UUID,
    run_id: UUID,
    request: Request,
    context: WorkspaceExecutionContext = context_dependency,
) -> AgentRunResponse:
    del workspace_id
    return AgentRunResponse.model_validate(await _service(request).get_run(context, run_id))


@router.get("/agent-runs/{run_id}/steps", response_model=list[RunStepResponse])
async def list_agent_run_steps(
    workspace_id: UUID,
    run_id: UUID,
    request: Request,
    context: WorkspaceExecutionContext = context_dependency,
) -> list[RunStepResponse]:
    del workspace_id
    steps = await _service(request).list_steps(context, run_id)
    return [RunStepResponse.model_validate(step) for step in steps]


@router.get("/agent-runs/{run_id}/approvals", response_model=list[ApprovalResponse])
async def list_agent_run_approvals(
    workspace_id: UUID,
    run_id: UUID,
    request: Request,
    context: WorkspaceExecutionContext = context_dependency,
) -> list[ApprovalResponse]:
    del workspace_id
    approvals = await _approval_service(request).list_for_run(context, run_id)
    return [ApprovalResponse.model_validate(approval) for approval in approvals]


@router.post("/agent-runs/{run_id}/cancel", response_model=AgentRunResponse)
async def cancel_agent_run(
    workspace_id: UUID,
    run_id: UUID,
    request: Request,
    context: WorkspaceExecutionContext = context_dependency,
) -> AgentRunResponse:
    del workspace_id
    result = await _service(request).cancel(context, run_id=run_id)
    return AgentRunResponse.model_validate(await _service(request).get_run(context, result.run_id))


__all__ = ["router"]
