"""Isolated, retained PostgreSQL, Qdrant and public HTTP fixtures for refund acceptance."""

import asyncio
import os
import secrets
import subprocess
import sys
from urllib.parse import urlparse
from uuid import UUID, uuid4

import asyncpg
from sqlalchemy.engine import make_url

from apps.worker.tasks.knowledge import _process_knowledge_ingestion
from packages.agent_runtime.models import Tool, ToolRevision
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.config.settings import Settings
from scripts.bootstrap_checkpoint import bootstrap

from .fixture_tools import fixture_tool_specs
from .runtime_trial import ROOT, read


async def isolated_settings(output):
    name = "refund_product_" + uuid4().hex[:16]
    base = Settings()
    original = make_url(base.database_url)
    if original.host not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("acceptance requires local PostgreSQL")
    if urlparse(base.qdrant_url).hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("acceptance requires local Qdrant")
    if original.host == "localhost":
        # Docker Desktop exposes the published IPv4 bind only. psycopg can wait
        # for an unreachable localhost IPv6 address on every checkpoint scope.
        original = original.set(host="127.0.0.1")
    admin = original.set(database="postgres", drivername="postgresql").render_as_string(
        hide_password=False
    )
    connection = await asyncpg.connect(admin)
    try:
        await connection.execute(f'CREATE DATABASE "{name}"')
    finally:
        await connection.close()
    url = original.set(database=name).render_as_string(hide_password=False)
    settings = base.model_copy(
        update={
            "database_url": url,
            "knowledge_qdrant_collection": name,
            "blob_root": str((output / "blobs").resolve()),
            "knowledge_chunk_size_chars": 1200,
            "knowledge_chunk_overlap_chars": 200,
            "knowledge_embedding_device": "cuda",
            "knowledge_reranker_device": "cuda",
            "knowledge_min_rerank_score": None,
            "knowledge_superseded_rank_penalty": 0.0,
            "knowledge_onyx_selector_base_url": None,
            "knowledge_onyx_selector_api_key": None,
        }
    )
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        check=True,
        env=dict(os.environ, AGENTHUB_DATABASE_URL=url),
    )
    await asyncio.to_thread(bootstrap, url)
    return settings


async def api(client, method, path, body=None, **kwargs):
    response = await client.request(method, path, json=body, **kwargs)
    if not response.is_success:
        raise ValueError(f"{method} {path} {response.status_code} {response.text[:500]}")
    return response.json()


async def tenant(owner, operator):
    users = []
    for client in [owner, operator]:
        user = await api(
            client,
            "POST",
            "auth/register",
            {"email": f"refund-{uuid4().hex}@example.test", "password": secrets.token_urlsafe(32)},
        )
        client.headers["Authorization"] = "Bearer " + user["access_token"]
        users.append(user["user_id"])
    org = await api(owner, "POST", "organizations", {"name": "退款政策验收"})
    ws = await api(
        owner, "POST", "workspaces", {"organization_id": org["id"], "name": "合成业务记录"}
    )
    scope = "workspaces/" + ws["id"]
    await api(
        owner,
        "POST",
        "organizations/" + org["id"] + "/members",
        {"user_id": users[1], "role": "MEMBER"},
    )
    await api(owner, "POST", scope + "/members", {"user_id": users[1], "role": "DEVELOPER"})
    return scope, users


async def policy_snapshot(owner, scope, documents, components, settings, queue):
    kb = await api(owner, "POST", scope + "/knowledge-bases", {"name": "虚构退款政策"})
    path = scope + "/knowledge-bases/" + kb["id"]
    uploaded = {}
    for doc in documents:
        sid = doc["source_id"]
        name = sid + "__policy.md"
        body = (
            doc["source"]
            if "source" in doc
            else (ROOT / "data" / doc["path"]).read_text(encoding="utf8")
        )
        upload = await api(
            owner,
            "POST",
            path + "/documents",
            files={"file": (name, body.encode(), "text/markdown")},
        )
        job = UUID(upload["ingestion_job"]["id"])
        if queue.job_ids.pop() != job:
            raise ValueError("ingestion job identity mismatch")
        await _process_knowledge_ingestion(job, settings, indexing_factory=lambda _: components)
        uploaded[sid] = upload
    for doc in documents:
        sid = doc["source_id"]
        successor = doc.get("lifecycle", {}).get("superseded_by")
        lifecycle = {
            "effective_date": doc["effective_from"][:10],
            "superseded_by_document_id": uploaded[successor]["document"]["id"]
            if successor
            else None,
        }
        updated = await api(
            owner, "PATCH", path + "/documents/" + uploaded[sid]["document"]["id"], lifecycle
        )
        if any(updated[key] != value for key, value in lifecycle.items()):
            raise ValueError("policy lifecycle readback mismatch")
        uploaded[sid]["document"] = updated
    snapshot = await api(owner, "POST", path + "/snapshots")
    if snapshot["item_count"] != len(documents):
        raise ValueError("snapshot lost uploaded policies")
    return kb["id"], snapshot, uploaded


async def publish(
    owner, scope, principal, factory, snapshot, profile_id, *, support_answer_policy=None
):
    bindings = []
    for identity in ("search_knowledge", "query_customer", "create_ticket"):
        tool = await api(
            owner,
            "POST",
            scope + "/tools",
            {"builtin_identity": identity, "name": identity + "-" + snapshot["id"][:8]},
        )
        bindings.append({"tool_id": tool["id"], "tool_revision_id": tool["current_revision_id"]})
    ws = UUID(scope.split("/")[-1])
    # Only the two synthetic commerce READs have no product catalog entry.
    async with factory() as db:
        for spec in fixture_tool_specs():
            if spec["identity"] == "query_customer":
                continue
            tool = Tool(workspace_id=ws, name=spec["identity"] + "-" + snapshot["id"][:8])
            db.add(tool)
            await db.flush()
            rev = ToolRevision(
                workspace_id=ws,
                tool_id=tool.id,
                revision_number=1,
                spec=spec,
                spec_hash=canonical_json_hash(spec),
                created_by=UUID(principal),
            )
            db.add(rev)
            await db.flush()
            bindings.append({"tool_id": str(tool.id), "tool_revision_id": str(rev.id)})
        await db.commit()
    template = await api(owner, "GET", scope + "/agent-templates/support")
    runtime = read(ROOT / "protocol.json")["runtime_target"]
    agent = await api(
        owner,
        "POST",
        scope + "/agents",
        {
            "name": "退款政策验收",
            "system_prompt": template["system_prompt"],
            "model_profile_id": profile_id,
            "model_retry_policy": {"max_attempts": 1},
            "retrieval_config": {"final_top_k": 6, "candidate_top_k": 20},
            "runtime_config": {
                **(
                    {"support_answer_policy": support_answer_policy}
                    if support_answer_policy
                    else {}
                ),
                "max_steps": 4,
                "max_tool_calls": 12,
                "max_parallel_reads": 3,
                "context_budget": runtime["context_budget"],
            },
        },
    )
    path = scope + "/agents/" + agent["id"]
    await api(owner, "PUT", path + "/tool-bindings", bindings)
    await api(
        owner,
        "PUT",
        path + "/knowledge-bindings",
        [
            {
                "knowledge_base_id": snapshot["knowledge_base_id"],
                "binding_mode": "PINNED",
                "snapshot_id": snapshot["id"],
            }
        ],
    )
    published = await api(owner, "POST", path + "/publish")
    version = await api(owner, "GET", path + "/versions/" + published["agent_version_id"])
    return path, version
