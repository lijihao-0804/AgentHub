"""Bounded, read-only readiness of this checkout's optional demo stack."""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import subprocess
import sys
import time
from uuid import UUID

import httpx

from scripts.portfolio_runtime import API, OPS, STATE, WEB, configure


def worker_name(pid: int) -> str:
    return f"portfolio-closeout-{pid}@{socket.gethostname()}"


def worker_probe(pid: int) -> bool:
    configure()
    from apps.worker.celery_app import celery_app

    celery_app.conf.broker_connection_timeout = 1
    celery_app.conf.broker_connection_max_retries = 0
    celery_app.conf.broker_transport_options = {
        "socket_connect_timeout": 1,
        "socket_timeout": 1,
        "max_retries": 0,
    }
    name = worker_name(pid)
    replies = celery_app.control.ping(destination=[name], timeout=1)
    return any(reply.get(name, {}).get("ok") == "pong" for reply in replies)


async def service_checks() -> dict[str, bool]:
    configure()
    from packages.core.config.settings import get_settings
    from packages.mcp.client import McpClientAdapter, McpConnectionTarget
    from packages.mcp.models import McpAuthType

    async def http_ready(client, url):
        try:
            response = await client.get(url)
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def ops_ready():
        try:
            settings = get_settings().model_copy(
                update={"mcp_request_timeout_seconds": 2, "mcp_connect_timeout_seconds": 1}
            )
            result = await McpClientAdapter(settings).discover_tools(
                McpConnectionTarget(UUID(int=0), OPS, McpAuthType.NONE), None
            )
            return (
                result.server is not None
                and result.server.server_name == "ops-mcp"
                and {"query_metrics", "get_deployments", "rollback_deployment"}
                <= {tool.name for tool in result.tools}
            )
        except Exception:
            return False

    async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
        values = await asyncio.gather(
            http_ready(client, API + "/api/v1/ready"),
            http_ready(client, WEB + "/api/v1/health"),
            http_ready(client, WEB + "/login"),
            ops_ready(),
        )
    return dict(zip(("api", "web_proxy", "web_page", "ops_mcp"), values, strict=True))


def await_services(timeout_seconds: float) -> dict[str, bool]:
    deadline = time.monotonic() + timeout_seconds
    last = {}
    while time.monotonic() < deadline:
        last = asyncio.run(service_checks())
        try:
            record = json.loads((STATE / "worker-process.json").read_text(encoding="utf-8"))
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "scripts.portfolio_readiness",
                    "--worker-probe",
                    str(record["pid"]),
                ],
                capture_output=True,
                timeout=min(5, max(0.1, deadline - time.monotonic())),
                check=False,
            )
            last["worker"] = result.returncode == 0
        except (OSError, KeyError, ValueError, subprocess.TimeoutExpired):
            last["worker"] = False
        if all(last.values()):
            return last
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))
    failed = ", ".join(key for key, ready in last.items() if not ready)
    raise RuntimeError("Readiness deadline exceeded: " + failed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout-seconds", type=float, default=90)
    parser.add_argument("--worker-probe", type=int)
    args = parser.parse_args()
    if args.worker_probe is not None:
        try:
            return 0 if worker_probe(args.worker_probe) else 1
        except Exception:
            return 1
    try:
        print(json.dumps(await_services(args.timeout_seconds)))
        return 0
    except Exception as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
