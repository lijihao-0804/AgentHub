"""Real loopback HTTP disconnects against existing SSE routes with controlled model.

Authentication is injected as a real isolated fixture identity: this tests transport
and durable execution, not login or security boundaries. No external model billing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import uvicorn

from apps.api.app import create_app
from apps.api.knowledge_dependencies import get_agent_run_queue, get_agent_run_workspace_context
from benchmarks.evaluation.artifacts import write_json_atomic
from benchmarks.evaluation.formal_support import context_for
from benchmarks.evaluation.live_pilot import ISOLATED_URL
from packages.agent_runtime.adapters.langgraph import configure_windows_asyncio_policy
from packages.agent_runtime.runtime import AgentRunService
from packages.core.config.settings import Settings
from packages.core.database import create_database
from packages.model_gateway.contracts import ModelResponse, ModelStreamEvent, ModelStreamEventType
from packages.observability.timing import capture_timings
from tests.integration.test_m4c_agent_runtime import _seed


class HeldGateway:
    def __init__(self):
        self.release = asyncio.Event()
        self.entered = asyncio.Event()
        self.closed = asyncio.Event()
        self.calls = 0

    def stream_resolved(self, context, plan, request):
        return self.events()

    async def events(self):
        self.calls += 1
        self.entered.set()
        try:
            yield ModelStreamEvent(
                event_type=ModelStreamEventType.MESSAGE_DELTA, message_delta="fixture-start"
            )
            await self.release.wait()
            yield ModelStreamEvent(
                event_type=ModelStreamEventType.MESSAGE_DELTA, message_delta=" fixture-end"
            )
            yield ModelStreamEvent(
                event_type=ModelStreamEventType.COMPLETED,
                response=ModelResponse(
                    content="fixture-start fixture-end", provider="controlled", model="controlled"
                ),
            )
        finally:
            self.closed.set()


@asynccontextmanager
async def server_for(factory, runtime, context, timings=None):
    app = create_app(Settings(testing=True, run_execution_in_worker=False))
    app.state.db_session_factory = factory
    app.state.agent_run_service = runtime
    if timings is not None:

        class TimingMiddleware:
            def __init__(self, app):
                self.app = app

            async def __call__(self, scope, receive, send):
                if scope["type"] != "http":
                    return await self.app(scope, receive, send)
                key = dict(scope.get("headers", [])).get(b"x-probe-id", b"").decode()
                with capture_timings() as timing:
                    try:
                        await self.app(scope, receive, send)
                    finally:
                        timings[key] = {
                            **timing.projection(),
                            "origin": "server_http_request_start",
                        }

        app.add_middleware(TimingMiddleware)

    async def fixture_context(workspace_id: UUID):
        if str(workspace_id) != context.workspace_id:
            raise ValueError("PROBE_WORKSPACE_MISMATCH")
        return context

    app.dependency_overrides[get_agent_run_workspace_context] = fixture_context
    app.dependency_overrides[get_agent_run_queue] = lambda: None
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.setblocking(False)
    server = uvicorn.Server(
        uvicorn.Config(app, lifespan="off", log_level="error", access_log=False)
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("PROBE_SERVER_NOT_STARTED")
                await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        listener.close()


async def frames(response):
    if response.status_code != 200:
        raise RuntimeError(f"PROBE_HTTP_STATUS_{response.status_code}")
    async for line in response.aiter_lines():
        if line.startswith("data:"):
            yield json.loads(line[5:].strip())


async def scenario(factory, mode):
    async with factory() as session:
        base = await _seed(session, label=f"http-stream-{uuid4().hex}")
    context = await context_for(factory, base["user"].id, base["workspace_id"])
    gateway = HeldGateway()
    runtime = AgentRunService(
        factory, model_gateway_factory=lambda db: gateway, stream_grace_seconds=0.5
    )
    workspace = context.workspace_id
    prefix = f"/api/v1/workspaces/{workspace}"
    received, replayed = [], []
    async with server_for(factory, runtime, context) as url:
        async with httpx.AsyncClient(base_url=url, timeout=10) as client:
            async with client.stream(
                "POST",
                f"{prefix}/agent-versions/{base['version'].id}/runs/stream",
                json={"input_text": "controlled HTTP stream probe"},
            ) as response:
                async for frame in frames(response):
                    received.append(frame)
                    if frame["type"] == "message.delta":
                        break
            run_id = received[0]["run_id"]
            after = received[-1]["sequence"]
            if mode == "reconnect_same_service":
                gateway.release.set()
                async with client.stream(
                    "GET", f"{prefix}/agent-runs/{run_id}/stream", params={"after_sequence": after}
                ) as response:
                    replayed = [frame async for frame in frames(response)]
            elif mode == "follow_other_service":
                other = AgentRunService(factory)
                async with server_for(factory, other, context) as other_url:
                    gateway.release.set()
                    async with httpx.AsyncClient(base_url=other_url, timeout=10) as follower:
                        async with follower.stream(
                            "GET",
                            f"{prefix}/agent-runs/{run_id}/stream",
                            params={"after_sequence": after},
                        ) as response:
                            replayed = [frame async for frame in frames(response)]
            elif mode == "abandon_after_grace":
                await asyncio.wait_for(gateway.closed.wait(), 5)
                # Provider cleanup precedes the shielded terminal DB commit.
                # Wait for the actual run state, not the earlier cleanup signal.
                async with asyncio.timeout(5):
                    while (await runtime.get_run(context, UUID(run_id))).status == "RUNNING":
                        await asyncio.sleep(0.02)
            else:
                raise ValueError("PROBE_MODE_UNKNOWN")
            run = await runtime.get_run(context, UUID(run_id))
            expected = "FAILED" if mode == "abandon_after_grace" else "SUCCEEDED"
            if run.status != expected or gateway.calls != 1:
                raise RuntimeError(f"HTTP_PROBE_STATE_{run.status}_CALLS_{gateway.calls}")
            if mode == "abandon_after_grace" and run.failure_code != "AGENT_STREAM_CANCELLED":
                raise RuntimeError("HTTP_PROBE_CANCEL_CODE_MISMATCH")
            if mode != "abandon_after_grace":
                if run.final_output != "fixture-start fixture-end" or not any(
                    f["type"] == "run.completed" for f in replayed
                ):
                    raise RuntimeError("HTTP_PROBE_REPLAY_MISSING_FINAL")
                if any(f["sequence"] <= after for f in replayed):
                    raise RuntimeError("HTTP_PROBE_REPLAY_CURSOR_MISMATCH")
            return {
                "mode": mode,
                "run_id": run_id,
                "status": run.status,
                "failure_code": run.failure_code,
                "provider_calls": gateway.calls,
                "provider_closed": gateway.closed.is_set(),
                "grace_seconds": 0.5,
                "initial_event_types": [f["type"] for f in received],
                "replayed_event_types": [f["type"] for f in replayed],
                "replay_sequences": [f["sequence"] for f in replayed],
                "after_sequence": after,
                "fixture_scope": "new_workspace_per_case",
                "business_writes": 0,
            }


async def run(output):
    if output.exists():
        raise ValueError("PROBE_OUTPUT_EXISTS")
    engine, factory = create_database(ISOLATED_URL)
    report = {
        "status": "RUNNING",
        "provider": "controlled",
        "transport": "real_loopback_tcp_http_sse",
        "limitations": [
            "fixture_identity_injected_not_auth_test",
            "same_process_two_services_not_OS_restart",
            "no_business_write_in_this_probe",
            "message_delta_not_durably_replayed_by_design",
        ],
        "cases": [],
    }
    try:
        for mode in ("reconnect_same_service", "follow_other_service", "abandon_after_grace"):
            async with asyncio.timeout(20):
                report["cases"].append(await scenario(factory, mode))
            write_json_atomic(output, report)
        report["status"] = "PASS"
    except Exception as error:
        report.update(status="FAIL", failure_class=type(error).__name__, failure_code=str(error))
        raise
    finally:
        write_json_atomic(output, report)
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    configure_windows_asyncio_policy()
    asyncio.run(run(args.output))
