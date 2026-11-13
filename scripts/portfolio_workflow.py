"""Public-API setup and replay of the isolated Incident portfolio workflow."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx

from scripts.portfolio_runtime import API, MODEL, OPS, STATE, configure

MANIFEST = STATE / "demo-manifest.json"


def load_manifest():
    return (
        json.loads(MANIFEST.read_text())
        if MANIFEST.exists()
        else {"mode": "DETERMINISTIC_ENGINEERING_DEMO"}
    )


def save_manifest(value):
    MANIFEST.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


class Client:
    def __init__(self):
        self.cfg = configure()
        self.http = httpx.Client(base_url=API, timeout=60)

    def request(self, method, path, *, token=None, body=None, expected=None):
        response = self.http.request(
            method, path, headers={"Authorization": f"Bearer {token}"} if token else {}, json=body
        )
        if expected is not None:
            assert response.status_code == expected, (path, response.status_code)
        elif not response.is_success:
            data = response.json()
            raise RuntimeError(
                f"{method} {path}: HTTP {response.status_code}, {data.get('error', {}).get('code')}"
            )
        return response.json() if response.content else None

    def login(self, role):
        return self.request("POST", "/api/v1/auth/login", body=self.cfg["accounts"][role])[
            "access_token"
        ]


def seed():
    client = Client()
    manifest = load_manifest()
    for role, account in client.cfg["accounts"].items():
        key = role + "_id"
        if key not in manifest:
            manifest[key] = client.request("POST", "/api/v1/auth/register", body=account)["user_id"]
            save_manifest(manifest)
    owner = client.login("operator")

    def create(key, path, body):
        if key not in manifest:
            manifest[key] = client.request("POST", path, token=owner, body=body)["id"]
            save_manifest(manifest)
        return manifest[key]

    organization = create(
        "organization_id",
        "/api/v1/organizations",
        {"name": "AgentHub — Deterministic Portfolio Lab"},
    )
    workspace = create(
        "workspace_id",
        "/api/v1/workspaces",
        {"organization_id": organization, "name": "FINAL-CLOSE-1 · 模拟工程演示"},
    )
    prefix = f"/api/v1/workspaces/{workspace}"
    if not manifest.get("memberships_ready"):
        for role in ("approver", "viewer"):
            client.request(
                "POST",
                f"/api/v1/organizations/{organization}/members",
                token=owner,
                body={
                    "user_id": manifest[role + "_id"],
                    "role": "ADMIN" if role == "approver" else "MEMBER",
                },
            )
            client.request(
                "POST",
                prefix + "/members",
                token=owner,
                body={
                    "user_id": manifest[role + "_id"],
                    "role": "DEVELOPER" if role == "approver" else "VIEWER",
                },
            )
        manifest["memberships_ready"] = True
        save_manifest(manifest)
    credential = create(
        "credential_id",
        prefix + "/provider-credentials",
        {
            "provider": "openai-compatible",
            "name": "No paid provider — scripted adapter only",
            "secret": "portfolio-not-a-real-provider-key",
        },
    )
    profile = create(
        "model_profile_id",
        prefix + "/model-profiles",
        {
            "provider_credential_id": credential,
            "model": MODEL,
            "temperature": "0",
            "max_tokens": 1024,
            "timeout_seconds": "30",
            "capabilities": {"tool_calling": True, "streaming": True, "max_context_tokens": 32768},
        },
    )
    connection = create(
        "mcp_connection_id",
        prefix + "/mcp-connections",
        {
            "name": "Repository Ops fixture — no production rollback",
            "endpoint_url": OPS,
            "auth_type": "NONE",
        },
    )
    tools = manifest.setdefault("tools", {})
    for name in ("query_metrics", "get_deployments", "rollback_deployment"):
        if name not in tools:
            tool = client.request(
                "POST",
                prefix + f"/mcp-connections/{connection}/import-tool",
                token=owner,
                body={
                    "remote_tool_name": name,
                    "identity": "portfolio_" + name,
                    "name": "模拟 Ops · " + name,
                    "effect": "WRITE" if name == "rollback_deployment" else "READ",
                    "risk_level": "HIGH" if name == "rollback_deployment" else "LOW",
                    "approval_policy": "ALWAYS" if name == "rollback_deployment" else "NEVER",
                    "timeout_seconds": 15,
                },
            )
            tools[name] = {"tool_id": tool["id"], "tool_revision_id": tool["current_revision_id"]}
            save_manifest(manifest)
    agent = create(
        "agent_id",
        prefix + "/agents",
        {
            "name": "Incident · 确定性工程演示（非真实模型）",
            "description": (
                "Real governance/checkpoint/MCP; scripted model and repository simulation. "
                "No production rollback or model-quality claim."
            ),
            "system_prompt": (
                "Controlled portfolio incident. Read metrics and deployments, propose rollback "
                "only behind independent approval, and report denial honestly. "
                "All business data is synthetic."
            ),
            "model_profile_id": profile,
            "model_retry_policy": {"max_attempts": 1},
            "runtime_config": {
                "max_steps": 8,
                "max_tool_calls": 4,
                "max_parallel_reads": 1,
                "memory": {"long_term_memory": False},
            },
        },
    )
    if "agent_version_id" not in manifest:
        client.request(
            "PUT", prefix + f"/agents/{agent}/tool-bindings", token=owner, body=list(tools.values())
        )
        version = client.request("POST", prefix + f"/agents/{agent}/publish", token=owner)
        manifest.update(
            agent_version_id=version["agent_version_id"],
            resolved_spec_hash=version["resolved_spec_hash"],
        )
        save_manifest(manifest)
    print(
        json.dumps(
            {k: manifest[k] for k in ("mode", "workspace_id", "agent_id", "agent_version_id")},
            ensure_ascii=False,
        )
    )


def action_count():
    path = STATE / "ops-actions.jsonl"
    return len(path.read_text().splitlines()) if path.exists() else 0


def begin(label):
    client = Client()
    manifest = load_manifest()
    if label in manifest:
        raise SystemExit(
            "Existing Run evidence retained; choose a fresh label instead of replaying it."
        )
    token = client.login("operator")
    prefix = f"/api/v1/workspaces/{manifest['workspace_id']}"
    thread = client.request(
        "POST",
        prefix + f"/agents/{manifest['agent_id']}/threads",
        token=token,
        body={"title": "Portfolio " + label, "kind": "incident"},
    )
    before = action_count()
    events = []
    with client.http.stream(
        "POST",
        prefix + f"/threads/{thread['id']}/turns/stream",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "input_text": (
                "调查 checkout-api 在 2026-03-14T20:50:00Z 到 21:20:00Z 的异常；"
                "基于指标和部署证据提议回滚至 5.4.1，等待独立人工批准。此为确定性工程演示。"
            ),
            "client_token": "portfolio-" + label,
        },
    ) as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))
    turns = client.request("GET", prefix + f"/threads/{thread['id']}/turns", token=token)
    run_id = turns[-1]["agent_run_id"]
    run = client.request("GET", prefix + f"/agent-runs/{run_id}", token=token)
    assert run["status"] == "WAITING_APPROVAL", (run["status"], run.get("failure_code"))
    approvals = client.request("GET", prefix + "/approvals?decision=PENDING", token=token)["items"]
    approval = next(a for a in approvals if a["run_id"] == run_id)
    assert action_count() == before
    manifest[label] = {
        "thread_id": thread["id"],
        "run_id": run_id,
        "approval_id": approval["id"],
        "before_actions": before,
        "waiting_at": datetime.now(UTC).isoformat(),
        "events": [{"type": e.get("type"), "sequence": e.get("sequence")} for e in events],
    }
    save_manifest(manifest)
    print(json.dumps(manifest[label], ensure_ascii=False))


def decide(label, decision):
    client = Client()
    manifest = load_manifest()
    record = manifest[label]
    token = client.login("approver")
    prefix = f"/api/v1/workspaces/{manifest['workspace_id']}"
    payload = {"reason": "Portfolio denial — do not execute."} if decision == "deny" else None
    result = client.request(
        "POST", prefix + f"/approvals/{record['approval_id']}/{decision}", token=token, body=payload
    )
    assert result["run_id"] == record["run_id"]
    assert result["approval"]["requested_by"] != result["approval"]["decided_by"]
    run = client.request("GET", prefix + f"/agent-runs/{record['run_id']}", token=token)
    assert run["status"] == "SUCCEEDED", (run["status"], run.get("failure_code"))
    expected = record["before_actions"] + (1 if decision == "approve" else 0)
    assert action_count() == expected
    if decision == "deny":
        assert "拒绝" in run["final_output"] and "未执行" in run["final_output"]
    record.update(
        decision=decision,
        after_actions=action_count(),
        run_status=run["status"],
        execution_status=result["approval"]["execution_status"],
        requested_by=result["approval"]["requested_by"],
        decided_by=result["approval"]["decided_by"],
        decided_at=datetime.now(UTC).isoformat(),
    )
    save_manifest(manifest)
    print(json.dumps(record, ensure_ascii=False))


def evaluation():
    import asyncio
    from uuid import UUID

    from packages.core.config.settings import get_settings
    from packages.core.database import create_database
    from scripts.portfolio_evaluation import seed as seed_evaluation

    configure()
    manifest = load_manifest()
    if "evaluation" in manifest:
        print(json.dumps(manifest["evaluation"], ensure_ascii=False))
        return

    async def run():
        engine, factory = create_database(get_settings().database_url)
        try:
            return await seed_evaluation(
                factory,
                UUID(manifest["workspace_id"]),
                UUID(manifest["operator_id"]),
                UUID(manifest["agent_version_id"]),
            )
        finally:
            await engine.dispose()

    manifest["evaluation"] = asyncio.run(run())
    save_manifest(manifest)
    print(json.dumps(manifest["evaluation"], ensure_ascii=False))


def verify():
    """Check actual HTTP projections and denied writes without dumping bodies."""
    from scripts.portfolio_evaluation import PRIVATE_SENTINEL

    client = Client()
    manifest = load_manifest()
    owner, viewer = client.login("operator"), client.login("viewer")
    prefix = f"/api/v1/workspaces/{manifest['workspace_id']}"
    run = manifest["evaluation"]["run_id"]
    base = prefix + f"/evaluation/experiment-runs/{run}"

    def safe(value):
        if isinstance(value, dict):
            assert not {"prompt", "private_facts", "raw_arguments"} & value.keys()
            if "observation" in value:
                assert value["observation"] == {}
            for child in value.values():
                safe(child)
        elif isinstance(value, list):
            for child in value:
                safe(child)
        elif isinstance(value, str):
            assert PRIVATE_SENTINEL not in value

    for token in (owner, viewer):
        for path in (base, base + "/statistics", base + "/case-results?limit=2&offset=0"):
            safe(client.request("GET", path, token=token, expected=200))
        for case in manifest["evaluation"]["case_ids"]:
            safe(
                client.request(
                    "GET", base + f"/case-results/{case}/evidence", token=token, expected=200
                )
            )
    pages = [
        client.request(
            "GET", base + f"/case-results?status=SUCCEEDED&limit=1&offset={i}", token=viewer
        )
        for i in range(3)
    ]
    assert all(page["total"] == 3 and len(page["items"]) == 1 for page in pages)
    assert len({page["items"][0]["id"] for page in pages}) == 3
    client.request("POST", base + "/metrics", token=viewer, expected=403)
    approval = next(
        record
        for record in manifest.values()
        if isinstance(record, dict)
        and record.get("decision") == "approve"
        and "approval_id" in record
    )
    client.request(
        "POST", prefix + f"/approvals/{approval['approval_id']}/approve", token=viewer, expected=403
    )
    redacted = client.request(
        "GET", prefix + f"/agent-runs/{approval['run_id']}", token=viewer, expected=200
    )
    assert redacted["input_text"] is None and redacted["final_output"] is None
    result = {
        "owner_and_viewer_summary_list_detail": 200,
        "viewer_metrics_write": 403,
        "viewer_approval_write": 403,
        "viewer_run_content_redacted": True,
        "private_observation_recursively_absent": True,
        "filtered_pagination_distinct_cases": 3,
        "evaluation_run_id": run,
    }
    (STATE / "http-evidence.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))
