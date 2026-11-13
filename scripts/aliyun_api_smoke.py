"""Exercise a running AgentHub test API with two synthetic Aliyun model turns.

Run from the checkout with `python -m scripts.aliyun_api_smoke --api-url URL`,
where URL ends in /api/v1. The configured aliyun-rag route reads AGENTHUB_RAG_BASE_URL
and AGENTHUB_RAG_API_KEY from the environment or the project .env. To log in as an
existing synthetic user, set AGENTHUB_SMOKE_EMAIL and AGENTHUB_SMOKE_PASSWORD.
Otherwise the script registers a disposable user. It creates a new organization and
workspace in either case. Results are read through a second HTTP client.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx

from packages.model_gateway.configured_route import ConfiguredRouteError, Endpoint, load_route

ROUTE = Path(__file__).resolve().parents[1] / "benchmarks/public_rag/model-route.json"
MODEL = "deepseek-v4.1-flash-1017"
MARKERS = ("JADE-31", "AMBER-47")


class SmokeFailure(Exception):
    """Contains only local check names and numeric HTTP statuses."""


def require(condition: bool, check: str) -> None:
    if not condition:
        raise SmokeFailure(check)


def check_url(value: str, *, provider: bool = False) -> None:
    url = urlsplit(value)
    host = url.hostname
    require(
        bool(host) and not (url.username or url.password or url.query or url.fragment),
        "URL must have a host and contain no credentials, query, or fragment",
    )
    if provider:
        require(
            url.scheme == "https"
            and url.port is None
            and (host == "dashscope.aliyuncs.com" or host.endswith(".cn-beijing.maas.aliyuncs.com"))
            and url.path.rstrip("/") == "/compatible-mode/v1",
            "aliyun-rag must use the configured Beijing HTTPS endpoint",
        )
    else:
        require(
            (
                url.scheme == "https"
                or (url.scheme == "http" and host in {"localhost", "127.0.0.1", "::1"})
            )
            and url.path.rstrip("/").endswith("/api/v1"),
            "test API must end in /api/v1 and use HTTPS or loopback HTTP",
        )


def request(client: httpx.Client, method: str, path: str, data: dict | None = None):
    response = client.request(method, path, json=data)
    if not response.is_success:
        # Never print remote response bodies, which may contain submitted secrets or content.
        label = path.rsplit("/", 1)[-1]
        raise SmokeFailure(f"{method} {label} returned HTTP {response.status_code}")
    return response.json()


def identifier(value: str) -> str:
    return str(UUID(value))


def publish(client: httpx.Client, route: Endpoint) -> tuple[str, str, str, str]:
    email, password = os.getenv("AGENTHUB_SMOKE_EMAIL"), os.getenv("AGENTHUB_SMOKE_PASSWORD")
    require(bool(email) == bool(password), "supply both synthetic login environment variables")
    auth = request(
        client,
        "POST",
        "auth/login" if email else "auth/register",
        {
            "email": email or f"agenthub-{uuid4().hex}@example.test",
            "password": password or secrets.token_urlsafe(32),
        },
    )
    client.headers["Authorization"] = "Bearer " + auth["access_token"]
    org = request(client, "POST", "organizations", {"name": "Aliyun smoke"})
    ws = request(
        client,
        "POST",
        "workspaces",
        {"name": "Synthetic smoke", "organization_id": identifier(org["id"])},
    )
    workspace = "workspaces/" + identifier(ws["id"])
    credential = request(
        client,
        "POST",
        workspace + "/provider-credentials",
        {
            "name": "Smoke",
            "provider": route.provider,
            "base_url": route.base_url,
            "secret": route.api_key,
        },
    )
    profile = request(
        client,
        "POST",
        workspace + "/model-profiles",
        {
            "provider_credential_id": identifier(credential["id"]),
            "model": route.model,
            "temperature": "0",
            "max_tokens": min(512, route.max_tokens),
            "timeout_seconds": str(route.timeout_seconds),
            "capabilities": route.capabilities,
        },
    )
    agent = request(
        client,
        "POST",
        workspace + "/agents",
        {
            "name": "Synthetic marker assistant",
            "model_profile_id": identifier(profile["id"]),
            "system_prompt": (
                "Return exactly the marker from the latest user's request. Do not add other text."
            ),
            "model_retry_policy": {"max_attempts": 1},
            "runtime_config": {"max_steps": 1},
        },
    )
    agent_path = workspace + "/agents/" + identifier(agent["id"])
    version = request(client, "POST", agent_path + "/publish")
    spec_hash = version["resolved_spec_hash"]
    require(bool(re.fullmatch("[a-f0-9]{64}", spec_hash)), "invalid published SHA-256")
    thread = request(client, "POST", agent_path + "/threads", {"title": "Synthetic smoke"})
    return workspace, identifier(version["agent_version_id"]), spec_hash, identifier(thread["id"])


def stream_turn(client: httpx.Client, turns: str, data: dict, expected: str) -> str:
    deltas, event_types = [], []
    with client.stream("POST", turns + "/stream", json=data) as response:
        require(response.status_code == 200, f"stream returned HTTP {response.status_code}")
        run_id = identifier(response.headers["X-AgentHub-Run-Id"])
        for line in response.iter_lines():
            if line.startswith("data:"):
                event = json.loads(line[5:])
                require(event["run_id"] == run_id, "stream run identity changed")
                event_types.append(event["type"])
                if event["type"] == "message.delta":
                    deltas.append(event["payload"]["delta"])
    require(
        event_types[:1] == ["run.started"]
        and event_types[-1:] == ["run.completed"]
        and "message.completed" in event_types
        and "".join(deltas).strip() == expected,
        "stream lifecycle or reconstructed answer mismatch",
    )
    return run_id


def run(api_url: str) -> dict:
    check_url(api_url)
    routes = load_route(ROUTE, env_file=ROUTE.parents[2] / ".env")
    route = next((item for item in routes.fallbacks if item.name == "aliyun-rag"), None)
    require(
        route is not None and route.model == MODEL and route.provider == "openai-compatible",
        "configured aliyun-rag model missing",
    )
    check_url(route.base_url, provider=True)
    prompts = [
        {"input_text": f"Return exactly the marker {marker}.", "client_token": uuid4().hex}
        for marker in MARKERS
    ]
    options = {
        "base_url": api_url.rstrip("/") + "/",
        "timeout": float(route.timeout_seconds) + 60,
        "trust_env": False,
        "follow_redirects": False,
    }
    with httpx.Client(**options) as client:
        workspace, version_id, spec_hash, thread_id = publish(client, route)
        turns = f"{workspace}/threads/{thread_id}/turns"
        first = request(client, "POST", turns, prompts[0])
        require(first["reused"] is False, "first turn already existed")
        run_id = identifier(first["run_id"])
        first_run = request(client, "GET", f"{workspace}/agent-runs/{run_id}")
        require(
            first_run["status"] == "SUCCEEDED" and first_run["final_output"].strip() == MARKERS[0],
            "synchronous run failed or returned a different answer",
        )
        replay = request(client, "POST", turns, prompts[0])
        require(
            replay["reused"] is True
            and replay["run_id"] == run_id
            and replay["turn"]["id"] == first["turn"]["id"],
            "idempotent replay mismatch",
        )
        run_ids = [run_id, stream_turn(client, turns, prompts[1], MARKERS[1])]
        token = client.headers["Authorization"]

    with httpx.Client(**options, headers={"Authorization": token}) as reader:
        rows = request(reader, "GET", turns)
        require(
            len(set(run_ids)) == 2 and [row["agent_run_id"] for row in rows] == run_ids,
            "persisted turn or run identity mismatch",
        )
        usage = []
        for run_id, turn, prompt, marker in zip(run_ids, rows, prompts, MARKERS, strict=True):
            path = f"{workspace}/agent-runs/{run_id}"
            stored = request(reader, "GET", path)
            require(
                stored["id"] == run_id
                and stored["agent_version_id"] == version_id
                and stored["resolved_spec_hash"] == spec_hash
                and turn["user_input"] == stored["input_text"] == prompt["input_text"]
                and turn["status"] == stored["status"] == "SUCCEEDED"
                and turn["final_output"].strip() == stored["final_output"].strip() == marker
                and stored["model_step_count"] == 1
                and stored["tool_call_count"] == 0,
                "persisted run identity, output or counters mismatch",
            )
            steps = [s for s in request(reader, "GET", path + "/steps") if s["kind"] == "MODEL"]
            require(len(steps) == 1 and steps[0]["status"] == "SUCCEEDED", "model step mismatch")
            metadata = steps[0]["safe_metadata"]
            require(
                metadata.get("provider") == route.provider
                and metadata.get("model") == MODEL
                and metadata.get("usage_source") == "generation",
                "model route mismatch",
            )
            count = stored["total_tokens"]
            require(type(count) is int and count > 0, "missing provider usage")
            usage.append(count)
    return {
        "passed": True,
        "model": MODEL,
        "synchronous": True,
        "sse": True,
        "idempotent_replay": True,
        "fresh_client_readback": True,
        "run_ids": run_ids,
        "spec_sha256": spec_hash,
        "total_tokens_by_run": usage,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api-url", required=True, help="Dedicated test service URL ending in /api/v1"
    )
    args = parser.parse_args()
    try:
        result = run(args.api_url)
    except (SmokeFailure, ConfiguredRouteError) as exc:
        result = {"passed": False, "error": str(exc)}
    except (httpx.HTTPError, KeyError, IndexError, ValueError, TypeError, AttributeError) as exc:
        result = {"passed": False, "error": type(exc).__name__}
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
