"""One-shot continuation of the November refund study after the S0-038 auth readback failure."""

import argparse
import asyncio
import hashlib
import os
import re
import secrets
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID

import httpx
from sqlalchemy import func, select
from sqlalchemy.engine import make_url

from benchmarks.public_rag.confirmation_meter import MODEL, AliyunMeter
from benchmarks.public_rag.confirmation_receipts import reconcile
from packages.agent_runtime.models import AgentRun, AgentVersion, RunStep
from packages.approvals.models import Approval
from packages.control_plane.models import OrganizationMembership, Workspace, WorkspaceMembership
from packages.core.config.settings import Settings
from packages.knowledge.models import KnowledgeSnapshot
from packages.model_gateway.models import ModelProfile, ProviderCredential
from packages.threads.models import AgentThread, ThreadTurn
from packages.tools.models import Ticket

from . import product_acceptance as acceptance
from .product_setup import api, policy_snapshot, publish
from .runtime_trial import ROOT, read, save

ORIGINAL_COMMIT = "70fe4b12fd2f68ef76fe6360b11f3b502b56c884"
ORIGINAL_SOURCE = "5db8a1d666a6d00fc83d6fa4b76924eda6cad81d625909d5645b12b9d9045be9"
ORIGINAL_REPORT = "4a84c84a0be55a025e0bd1bbb6f690ccc98b1f01005d45b2c97c4c198bfb7193"
PREFIX = [f"S0-{i:03}" for i in range(1, 39)]
DEV = [f"S0-{i:03}" for i in range(1, 51)]
NEW = ["NEW-PICKUP", "NEW-LOCKER"]
SCHEDULE = {**dict.fromkeys(DEV, "refund_dev"), **dict.fromkeys(NEW, "refund_new")}
CLAIM = "refund_suffix_after_s0_038"


def load_prefix(prior):
    if hashlib.sha256((prior / "report.json").read_bytes()).hexdigest() != ORIGINAL_REPORT:
        raise ValueError("Original study report digest differs")
    report = read(prior / "report.json")
    dev = acceptance.reviewed_cases()
    new = read(ROOT / "new_ticket_cases.json")["cases"]
    if not (
        report["code_commit"] == ORIGINAL_COMMIT
        and report["python_source_sha256"] == ORIGINAL_SOURCE
        and report["mode"] == "paid"
        and list(report["schedule"].items()) == list(SCHEDULE.items())
        and [c["case_id"] for c in dev] == DEV
        and [c["case_id"] for c in new] == NEW
        and [r["case_id"] for r in report["rows"]] == PREFIX
        and set(report["groups"]) == {"refund_dev"}
    ):
        raise ValueError("Not the frozen 52-question schedule with the original 38-question prefix")
    records = []
    for case, summary in zip(dev[:38], report["rows"], strict=True):
        record = read(prior / (case["case_id"] + ".json"))
        if (
            record["expected"] != case
            or record["case_id"] != case["case_id"]
            or record["stage"] != "refund_dev"
            or record.get("thread_id") != summary.get("thread_id")
            or record.get("run_id") != summary.get("run_id")
        ):
            raise ValueError("Prefix case differs from original receipt")
        if case["case_id"] != PREFIX[-1]:
            acceptance.require_settled_run(record)
        records.append(record)
    return report, records, dev, new


def check_ledger(db, records):
    if [r["case_id"] for r in records] != PREFIX:
        raise ValueError("Incorrect original prefix")
    if not db.execute("SELECT 1 FROM study_opening WHERE name='refund_dev_and_new'").fetchone():
        raise ValueError("Original single-exposure opening missing")
    if db.execute("SELECT 1 FROM study_opening WHERE name=?", (CLAIM,)).fetchone():
        raise ValueError("Continuation already claimed")
    if db.execute("SELECT 1 FROM wires WHERE status IS NOT 'returned'").fetchone():
        raise ValueError("Unsettled physical request in study ledger")
    wires = [
        dict(row)
        for row in db.execute(
            "SELECT * FROM wires WHERE stage IN ('refund_dev','refund_new') ORDER BY id"
        )
    ]
    expected = []
    for record in records:
        attempts = record["physical_attempts"]
        if not 1 <= len(attempts) <= 4 or any(
            w["task"] != record["case_id"] or w["stage"] != "refund_dev" for w in attempts
        ):
            raise ValueError("Unexpected original physical receipts")
        expected.extend(attempts)
    if wires != expected:
        raise ValueError("Ledger differs from original receipts or tail has already dispatched")


def ledger_gate(path, records, *, claim=False):
    mode = "rw" if claim else "ro"
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode={mode}", uri=True)) as db, db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE" if claim else "BEGIN")
        check_ledger(db, records)
        if claim:
            db.execute("INSERT INTO study_opening(name) VALUES (?)", (CLAIM,))


def retained_settings(prior, name):
    base = Settings()
    original = make_url(base.database_url)
    local = {"127.0.0.1", "::1", "localhost"}
    if (
        not re.fullmatch(r"refund_product_[a-f0-9]{16}", name)
        or original.host not in local
        or urlparse(base.qdrant_url).hostname not in local
        or not (prior / "blobs").is_dir()
    ):
        raise ValueError("Expected retained local refund database, Qdrant and blobs")
    url = original.set(
        database=name, host="127.0.0.1" if original.host == "localhost" else original.host
    )
    return base.model_copy(
        update={
            "database_url": url.render_as_string(hide_password=False),
            "knowledge_qdrant_collection": name,
            "blob_root": str((prior / "blobs").resolve()),
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


async def check_database(db, report, records, users):
    ws = UUID(report["workspace_id"])
    group = report["groups"]["refund_dev"]
    snapshot, version = group["snapshot"], group["version"]
    agent_version = await db.get(AgentVersion, UUID(version["id"]))
    kb = await db.get(KnowledgeSnapshot, UUID(snapshot["id"]))
    workspace = await db.get(Workspace, ws)
    if not (
        agent_version
        and kb
        and workspace
        and agent_version.workspace_id == kb.workspace_id == ws
        and agent_version.resolved_spec_hash == version["resolved_spec_hash"]
        and agent_version.resolved_spec == version["resolved_spec"]
        and str(agent_version.agent_id) == version["agent_id"]
        and str(kb.knowledge_base_id) == snapshot["knowledge_base_id"]
        and kb.content_hash == snapshot["content_hash"]
        and version["resolved_spec"]["retrieval"]["knowledge_snapshots"]
        == [{"snapshot_id": snapshot["id"], "snapshot_hash": kb.content_hash}]
        and str(agent_version.created_by) == users[0]
    ):
        raise ValueError("Retained DEV version, owner, workspace or snapshot differs")
    if list(await db.scalars(select(AgentVersion.id).where(AgentVersion.workspace_id == ws))) != [
        agent_version.id
    ]:
        raise ValueError("Unexpected version in retained workspace")
    org_roles = dict(
        (
            await db.execute(
                select(OrganizationMembership.user_id, OrganizationMembership.role).where(
                    OrganizationMembership.organization_id == workspace.organization_id
                )
            )
        ).all()
    )
    ws_roles = dict(
        (
            await db.execute(
                select(WorkspaceMembership.user_id, WorkspaceMembership.role).where(
                    WorkspaceMembership.workspace_id == ws
                )
            )
        ).all()
    )
    if (
        org_roles != {UUID(users[0]): "OWNER", UUID(users[1]): "MEMBER"}
        or ws_roles.get(UUID(users[1])) != "DEVELOPER"
    ):
        raise ValueError("Original tenant principals/roles differ")
    for model in (Approval, Ticket):
        if await db.scalar(select(func.count()).select_from(model).where(model.workspace_id == ws)):
            raise ValueError("Existing approval or write in DEV prefix")
    by_key = {}
    for run, turn, thread in (
        await db.execute(
            select(AgentRun, ThreadTurn, AgentThread)
            .join(ThreadTurn, ThreadTurn.agent_run_id == AgentRun.id)
            .join(AgentThread, AgentThread.id == ThreadTurn.thread_id)
            .where(AgentRun.workspace_id == ws)
        )
    ).all():
        if thread.title in by_key:
            raise ValueError("Duplicate prefix thread")
        by_key[thread.title] = (run, turn, thread)
    if set(by_key) != set(PREFIX):
        raise ValueError("Database does not contain exactly the prefix cases")
    for model in (AgentRun, ThreadTurn, AgentThread):
        if await db.scalar(
            select(func.count()).select_from(model).where(model.workspace_id == ws)
        ) != len(PREFIX):
            raise ValueError("Unbound run, turn or thread in retained workspace")
    for record in records:
        key = record["case_id"]
        run, turn, thread = by_key[key]
        if not (
            run.status in {"SUCCEEDED", "FAILED"}
            and run.completed_at
            and run.agent_version_id == agent_version.id
            and run.resolved_spec_hash == version["resolved_spec_hash"]
            and run.thread_turn_id == turn.id
            and run.thread_id == thread.id
            and turn.workspace_id == thread.workspace_id == ws
            and thread.agent_id == agent_version.agent_id
            and thread.title == key
            and str(thread.created_by) == users[1]
            and str(thread.id) == record["thread_id"]
            and (not record.get("run_id") or str(run.id) == record["run_id"])
            and turn.user_input == run.input_text == record["expected"]["input"]
            and not run.resume_pending
            and not run.waiting_approval_id
            and (run.status != "SUCCEEDED" or run.final_output)
        ):
            raise ValueError(f"{key}: retained run or turn is incomplete")
        if key != PREFIX[-1] and (
            run.status != record["run"]["status"] or str(run.id) != record["run_id"]
        ):
            raise ValueError("Prior result differs from durable run")
        if key == PREFIX[-1]:
            attempts = record["physical_attempts"]
            if (run.total_input_tokens, run.total_output_tokens) != (
                sum(w["input"] for w in attempts),
                sum(w["output"] for w in attempts),
            ):
                raise ValueError("S0-038 usage differs from settled ledger")
            finish = list(
                await db.scalars(
                    select(RunStep.status).where(
                        RunStep.agent_run_id == run.id, RunStep.kind == "FINISH"
                    )
                )
            )
            if finish != [run.status]:
                raise ValueError("S0-038 missing terminal step")
    primary = version["resolved_spec"]["model"]
    profile = await db.get(ModelProfile, UUID(primary["profile_id"]))
    credential = await db.get(ProviderCredential, UUID(primary["credential_ref"]))
    if not (
        credential
        and profile
        and profile.workspace_id == ws
        and profile.provider_credential_id == credential.id
        and not credential.enabled
        and credential.workspace_id == ws
        and primary["model"] == MODEL
        and not primary.get("fallback_profiles")
        and primary["retry_policy"]["max_attempts"] == 1
    ):
        raise ValueError("Original disabled route or frozen profile differs")
    return str(credential.id), primary["profile_id"]


async def login(client, role):
    result = await api(
        client,
        "POST",
        "auth/login",
        {
            "email": os.environ[f"REFUND_STUDY_{role}_EMAIL"],
            "password": os.environ[f"REFUND_STUDY_{role}_PASSWORD"],
        },
    )
    client.headers["Authorization"] = "Bearer " + result["access_token"]
    return result["user_id"]


async def run(args):
    if args.ledger.resolve() != (Path.cwd() / ".scratch/approved-study.sqlite").resolve():
        raise ValueError("Use the original authorized study ledger in this checkout")
    prior, out = args.prior.resolve(), args.output.resolve()
    if prior == out or out.is_relative_to(prior) or out.exists():
        raise ValueError("A fresh sibling output directory is required")
    source_paths = [
        "apps",
        "packages",
        "scripts",
        "migrations",
        str(ROOT.relative_to(Path.cwd())) if ROOT.is_absolute() else str(ROOT),
        "benchmarks/public_rag/confirmation_meter.py",
        "benchmarks/public_rag/confirmation_receipts.py",
    ]
    if subprocess.check_output(
        ["git", "status", "--porcelain", "--", *source_paths], text=True
    ).strip():
        raise ValueError("Commit the continuation implementation before paid exercise")
    changed = set(
        subprocess.check_output(
            [
                "git",
                "diff",
                "--name-only",
                "--diff-filter=DMRT",
                ORIGINAL_COMMIT,
                "HEAD",
                "--",
                *source_paths[:-1],
            ],
            text=True,
        ).splitlines()
    )
    allowed = {
        str(p.relative_to(Path.cwd()).as_posix())
        for p in (Path(__file__).resolve(), Path(acceptance.__file__).resolve())
    }
    if any(p not in allowed for p in changed if p.endswith((".py", ".json", ".md"))):
        raise ValueError("Frozen exercise sources or fixtures changed outside the continuation")
    report, records, dev, new = load_prefix(prior)
    reconcile(args.ledger)
    ledger_gate(args.ledger, records)
    settings = retained_settings(prior, report["db_name"])
    engine, factory, app, queue, components, runtime, store, retriever = await acceptance.assemble(
        settings
    )
    try:
        opts = {
            "transport": httpx.ASGITransport(app=app),
            "base_url": "https://product.local/api/v1/",
        }
        async with httpx.AsyncClient(**opts) as owner, httpx.AsyncClient(**opts) as operator:
            users = [await login(owner, "OWNER"), await login(operator, "OPERATOR")]
            async with factory() as db:
                cid, profile = await check_database(db, report, records, users)
            scope = "workspaces/" + report["workspace_id"]
            dev_group = report["groups"]["refund_dev"]
            version = dev_group["version"]
            agent = scope + "/agents/" + version["agent_id"]
            retained = await api(owner, "GET", agent + "/versions/" + version["id"])
            if retained != version:
                raise ValueError("HTTP DEV version readback differs")
            credential_path = scope + "/provider-credentials/" + cid
            route = acceptance.aliyun_route()
            credential = await api(owner, "GET", credential_path)
            if credential["base_url"] != route.base_url or credential["enabled"]:
                raise ValueError("Retained credential endpoint or cleanup state differs")
            await acceptance.refresh_roles(owner, operator, users)
            dev_chunks = await acceptance.group_chunks(
                factory, dev_group["snapshot"]["knowledge_base_id"], dev_group["documents"]
            )
            if not dev_chunks:
                raise ValueError("Original DEV chunks missing")
            out.mkdir(parents=True, exist_ok=False)
            tail = {k: report[k] for k in ("schedule", "mode", "db_name", "workspace_id")}
            tail.update(
                rows=[],
                groups={"refund_dev": dev_group},
                original_commit=ORIGINAL_COMMIT,
                original_python_source_sha256=ORIGINAL_SOURCE,
                prior_report_sha256=hashlib.sha256(
                    (prior / "report.json").read_bytes()
                ).hexdigest(),
                code_commit=subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], text=True
                ).strip(),
                continuation_schedule={k: v for k, v in SCHEDULE.items() if k not in PREFIX},
            )
            save(out / "report.json", tail)
            ledger_gate(args.ledger, records, claim=True)
            try:
                await api(
                    owner, "POST", credential_path + "/rotate-secret", {"secret": route.api_key}
                )
                await api(owner, "PATCH", credential_path, {"enabled": True})
                for stage, cases in (("refund_dev", dev[38:]), ("refund_new", new)):
                    await acceptance.refresh_roles(owner, operator, users)
                    if stage == "refund_new":
                        policies = [
                            dict(c, effective_from=d)
                            for c, d in zip(new, ["2026-10-01", "2026-09-15"], strict=True)
                        ]
                        kb, snap, uploaded = await policy_snapshot(
                            owner, scope, policies, components, settings, queue
                        )
                        agent, version = await publish(
                            owner, scope, users[0], factory, snap, profile
                        )
                        tail["groups"][stage] = {
                            "snapshot": snap,
                            "version": version,
                            "documents": uploaded,
                        }
                        save(out / "report.json", tail)
                        chunks = await acceptance.group_chunks(factory, kb, uploaded)
                    else:
                        chunks = dev_chunks
                    await acceptance.exercise_cases(
                        cases,
                        stage,
                        AliyunMeter(args.ledger, stage),
                        args,
                        owner,
                        operator,
                        users,
                        scope,
                        agent,
                        store,
                        runtime,
                        retriever,
                        settings,
                        chunks,
                        out,
                        tail,
                    )
                return all(r["contract_pass"] for r in tail["rows"])
            finally:
                # Reauthenticate independently even if a role's refresh cookie expired.
                if await login(owner, "OWNER") != users[0]:
                    raise ValueError("Unexpected cleanup principal")
                await api(owner, "PATCH", credential_path, {"enabled": False})
                await api(
                    owner,
                    "POST",
                    credential_path + "/rotate-secret",
                    {"secret": secrets.token_urlsafe(32)},
                )
                if (await api(owner, "GET", credential_path))["enabled"]:
                    raise ValueError("Credential cleanup not confirmed")
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ("prior", "ledger", "output"):
        parser.add_argument("--" + arg, type=Path, required=True)
    args = parser.parse_args()
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    acceptance.configure_windows_asyncio_policy()
    return 0 if asyncio.run(run(args)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
