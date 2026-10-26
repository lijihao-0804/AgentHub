from __future__ import annotations

import asyncio

import httpx2
import pytest

from packages.mcp.client import (
    MCP_TARGET_FORBIDDEN,
    GuardedAsyncTransport,
    McpRemoteError,
    _Observation,
)


class RecordingPeer:
    def __init__(self, address: str) -> None:
        self.address = address

    def get_extra_info(self, name: str):
        return (self.address, 443) if name == "server_addr" else None


class RecordingInner(httpx2.AsyncBaseTransport):
    def __init__(self, address: str) -> None:
        self.address = address
        self.requests_sent = 0
        self.closed = 0
        self.authorization_seen: str | None = None

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.requests_sent += 1
        self.authorization_seen = request.headers.get("Authorization")
        return httpx2.Response(
            200,
            request=request,
            content=b"sent",
            extensions={"network_stream": RecordingPeer(self.address)},
        )

    async def aclose(self) -> None:
        self.closed += 1


async def main() -> None:
    sentinel = "Bearer probe-secret-sentinel"
    request = httpx2.Request(
        "POST", "https://public.example/mcp", headers={"Authorization": sentinel}
    )
    inner = RecordingInner("169.254.169.254")
    guard = GuardedAsyncTransport(inner, allow_private_targets=False, observation=_Observation())
    rejected = False
    try:
        await guard.handle_async_request(request)
    except McpRemoteError as exc:
        rejected = True
        print(
            "private_peer",
            "rejected",
            exc.failure_code,
            "requests_sent",
            inner.requests_sent,
            "authorization_seen",
            inner.authorization_seen,
        )
        assert exc.failure_code == MCP_TARGET_FORBIDDEN
        assert inner.requests_sent == 1
        assert inner.authorization_seen == sentinel

    assert rejected
    await guard.aclose()
    print("inner_aclose_calls", inner.closed)


if __name__ == "__main__":
    asyncio.run(main())


@pytest.mark.asyncio
async def test_peer_guard_runs_after_transport_saw_bearer():
    await main()
