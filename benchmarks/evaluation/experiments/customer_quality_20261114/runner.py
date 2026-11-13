"""Freeze and run a single paired synthetic DEV diagnostic on an isolated local DB."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import UUID, uuid4

from sqlalchemy import func, select

from benchmarks.evaluation.experiments.customer_quality_20261105.budget import ResearchBudgetLedger
from benchmarks.evaluation.experiments.customer_quality_20261105.provider import (
    BudgetedAliyunClient,
)
from benchmarks.evaluation.experiments.customer_quality_20261105.runner import access, read, save
from packages.agent_runtime.adapters.langgraph import (
    LangGraphCheckpointAdapter,
    configure_windows_asyncio_policy,
)
from packages.agent_runtime.models import Agent, AgentVersion
from packages.agent_runtime.publish import AgentPublishService
from packages.agent_runtime.runtime import AgentRunService
from packages.approvals import ApprovalDecisionStatus, ApprovalService
from packages.control_plane.product_control_plane import ProductControlPlaneService
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.config.settings import get_settings
from packages.core.database import create_database
from packages.evaluation.models import EvaluationExperimentHoldoutExposure
from packages.model_gateway.adapters.litellm import LiteLLMProviderAdapter
from packages.model_gateway.configured_route import load_route
from packages.model_gateway.credentials import (
    ProviderCredentialCipher,
    encrypt_provider_credential_secret,
)
from packages.model_gateway.gateway import SqlAlchemyModelGateway
from packages.model_gateway.models import ModelProfile, ProviderCredential
from packages.tools.actions import ActionRuntime
from packages.tools.models import Customer, Ticket
from packages.tools.runtime import ToolRuntime
from tests.support.ae6b_queue import seed_queue_agent

from .score import permitted_proposal, score

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
STUDY_ROOT = Path(
    os.environ.get(
        "AGENTHUB_CUSTOMER_QUALITY_STUDY_DIR", REPO / ".scratch/customer-quality-20261114"
    )
)
PRIVATE = STUDY_ROOT / "study"
PROTOCOL = HERE / "protocol.json"
LEGACY = HERE.with_name("customer_quality_20261105") / "protocol.json"
PUBLISHED_PROMPTS = HERE / "published-prompts.json"
ARMS = ("baseline", "candidate")


def timestamp():
    return datetime.now(UTC).isoformat()


def published_prompt(name):
    snapshot = read(PUBLISHED_PROMPTS)[name]
    prompt = snapshot["system_prompt"]
    if hashlib.sha256(prompt.encode("utf-8")).hexdigest() != snapshot["prompt_sha256"]:
        raise RuntimeError("PUBLISHED_PROMPT_DIGEST_MISMATCH")
    return prompt


def identities():
    paths = [
        PROTOCOL,
        HERE / "runner.py",
        HERE / "score.py",
        LEGACY,
        PUBLISHED_PROMPTS,
        REPO / "packages/tools/builtins/query_customer.py",
        REPO / "packages/control_plane/product_control_plane.py",
    ]
    return {
        str(path.relative_to(REPO).as_posix()): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in paths
    }


def selected_endpoint(protocol):
    config = load_route(REPO / "benchmarks/public_rag/model-route.json", env_file=REPO / ".env")
    route = next(endpoint for endpoint in config.fallbacks if endpoint.name == protocol["provider"])
    if route.model != protocol["model"] or route.provider != "openai-compatible":
        raise RuntimeError("CONFIGURED_MODEL_MISMATCH")
    return route


async def publish(factory, settings, endpoint, protocol):
    seeded = await seed_queue_agent(factory, settings)
    owner = await access(factory, seeded.owner_id, seeded.workspace_id)
    cipher = ProviderCredentialCipher.from_settings(settings)
    async with factory() as session:
        seed_version = await session.get(AgentVersion, seeded.agent_version_id)
        agent = await session.get(Agent, seed_version.agent_id)
        profile = await session.get(ModelProfile, agent.model_profile_id)
        cred = await session.get(ProviderCredential, profile.provider_credential_id)
        cred.provider, cred.base_url = endpoint.provider, endpoint.base_url
        encrypt_provider_credential_secret(cred, endpoint.api_key, cipher=cipher)
        profile.model, profile.temperature = endpoint.model, Decimal("0")
        profile.max_tokens = protocol["max_output_tokens"]
        profile.timeout_seconds = endpoint.timeout_seconds
        profile.capabilities = endpoint.capabilities
        profile.fallback_profile_id = None
        agent.model_retry_policy = {"max_attempts": 1}
        agent.runtime_config = {"max_steps": protocol["max_calls_per_trial"], "max_tool_calls": 4}
        versions = {}
        for arm in ARMS:
            if arm == "baseline":
                agent.system_prompt = read(LEGACY)["variants"]["candidate"]["system_prompt"]
            else:
                control = ProductControlPlaneService()
                bindings = []
                for identity in ("query_customer", "create_ticket"):
                    tool = await control.create_tool(session, owner, identity)
                    bindings.append(
                        {"tool_id": tool["id"], "tool_revision_id": tool["current_revision_id"]}
                    )
                await control.replace_tool_bindings(session, owner, agent.id, bindings)
                agent.system_prompt = published_prompt("lookup_candidate")
            agent.prompt_version += 1
            await session.commit()
            version = await AgentPublishService().publish(session, owner, agent.id)
            versions[arm] = {
                "id": str(version.id),
                "resolved_spec_hash": version.resolved_spec_hash,
            }
        return {
            "workspace": str(seeded.workspace_id),
            "owner": str(seeded.owner_id),
            "approver": str(seeded.approver_id),
            "versions": versions,
        }


async def freeze(factory, settings, endpoint, protocol):
    PRIVATE.mkdir(parents=True, exist_ok=True)
    marker = PRIVATE / "freeze.json"
    if marker.exists() or (PRIVATE / "started.json").exists():
        raise RuntimeError("STUDY_ALREADY_FROZEN")
    identity = await publish(factory, settings, endpoint, protocol)
    manifest = {
        "frozen_at": timestamp(),
        "provider_calls_at_freeze": 0,
        "files_sha256": identities(),
        "protocol_hash": canonical_json_hash(protocol),
        "source_base_head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO, text=True
        ).strip(),
        **identity,
    }
    save(marker, manifest)
    print(f"Frozen {len(protocol['cases'])} DEV groups, two published versions, zero calls.")


async def seed_customer(factory, workspace, case, customer):
    async with factory() as session:
        row = Customer(workspace_id=workspace, customer_ref=customer, name="Synthetic DEV Customer")
        session.add(row)
        await session.flush()
        for index in range(case["other_open"]):
            session.add(
                Ticket(
                    workspace_id=workspace,
                    customer_id=row.id,
                    ticket_ref=f"{customer}-{index:02}",
                    subject=f"其他客服咨询 {index:02}",
                    status="OPEN",
                )
            )
        existing_ref = None
        if case["matching_status"]:
            existing_ref = customer + "-ZZ"
            session.add(
                Ticket(
                    workspace_id=workspace,
                    customer_id=row.id,
                    ticket_ref=existing_ref,
                    subject=case["subject"],
                    status=case["matching_status"],
                )
            )
        await session.commit()
        return row.id, existing_ref


def latest_call(meter):
    key = meter.records[-1]["logical_call_key"]
    return read(meter.private_dir / (quote(key, safe="") + ".json"))


async def trial(factory, runtime, meter, owner, approver, state, case, arm, repeat, protocol):
    ref = "DEV-" + uuid4().hex[:10].upper()
    customer_id, existing_ref = await seed_customer(factory, UUID(state["workspace"]), case, ref)
    key = f"{case['id']}/{repeat}/{arm}"
    metadata = {"phase": "DEV", "arm": arm, "case_id": case["id"]}
    prompt = case["request"].format(customer=ref) + "\n" + protocol["answer_instruction"]
    approval_service = ApprovalService(factory)
    proposal_ok = True
    previous_records = len(meter.records)
    with meter.scope(key, metadata):
        outcome = await runtime.run(
            owner, agent_version_id=UUID(state["versions"][arm]["id"]), input_text=prompt
        )
        while outcome.status == "WAITING_APPROVAL":
            approvals = await approval_service.list_for_run(approver, outcome.run_id)
            pending = [a for a in approvals if a.decision_status == "PENDING"]
            if len(pending) != 1 or len(approvals) > 4 or len(meter.records) == previous_records:
                raise RuntimeError("APPROVAL_OR_EVIDENCE_INCONSISTENT")
            allowed = len(approvals) == 1 and permitted_proposal(
                case, ref, pending[0], latest_call(meter)["request"]["messages"]
            )
            proposal_ok = proposal_ok and allowed
            decision = ApprovalDecisionStatus.DENIED
            if allowed and case["expected"] == "CREATED":
                decision = ApprovalDecisionStatus.APPROVED
            await approval_service.decide(approver, pending[0].id, decision=decision)
            outcome = await runtime.resume(
                approver, run_id=outcome.run_id, approval_id=pending[0].id
            )
    approvals = await approval_service.list_for_run(approver, outcome.run_id)
    async with factory() as session:
        added = list(
            await session.scalars(
                select(Ticket).where(
                    Ticket.customer_id == customer_id, Ticket.idempotency_key.is_not(None)
                )
            )
        )
    records = meter.records[previous_records:]
    messages = (
        latest_call(meter)["request"]["messages"]
        if records and records[-1]["status"] == "SETTLED"
        else []
    )
    result = score(
        case,
        customer=ref,
        messages=messages,
        answer=outcome.final_output,
        status=outcome.status,
        approvals=approvals,
        existing_ref=existing_ref,
        added=added,
    )
    result["checks"]["proposal_permitted"] = proposal_ok
    result["passed"] = all(result["checks"].values())
    unknown_action = any(a.execution_status in {"CLAIMED", "UNKNOWN_OUTCOME"} for a in approvals)
    if outcome.status == "NEEDS_ATTENTION" or unknown_action:
        meter.ledger.halt("ACTION_RECONCILIATION_REQUIRED")
    if added and (not proposal_ok or not permitted_creation(case, approvals, added)):
        meter.ledger.halt("UNRECONCILED_WRITE")
    if arm == "candidate" and result["false_creation_receipt"]:
        meter.ledger.halt("FALSE_CANDIDATE_CREATION_RECEIPT")
    return {
        **result,
        "case": case["id"],
        "arm": arm,
        "repeat": repeat,
        "run_id": str(outcome.run_id),
        "run_status": outcome.status,
        "failure_code": outcome.failure_code,
        "provider_attempts": len(records),
        "all_usage_known": bool(records) and all(r["usage"] for r in records),
    }


def permitted_creation(case, approvals, added):
    return (
        case["expected"] == "CREATED"
        and len(approvals) == len(added) == 1
        and approvals[0].decision_status == "APPROVED"
        and approvals[0].execution_status == "SUCCEEDED"
        and (approvals[0].safe_result or {}).get("ticket_ref") == added[0].ticket_ref
    )


def report(protocol, rows):
    counts = {}
    for arm in ARMS:
        arm_rows = [row for row in rows if row["arm"] == arm]
        groups = {}
        for case in protocol["cases"]:
            group = [row for row in arm_rows if row["case"] == case["id"]]
            groups[case["id"]] = (
                len(group) == protocol["repetitions"]
                and {r["repeat"] for r in group} == set(range(protocol["repetitions"]))
                and all(r["passed"] for r in group)
            )
        counts[arm] = {
            "completed_trials": len(arm_rows),
            "passing_trials": sum(row["passed"] for row in arm_rows),
            "passing_groups": sum(groups.values()),
            "groups": groups,
        }
    return counts


async def execute(factory, settings, endpoint, protocol):
    state = read(PRIVATE / "freeze.json")
    if state["files_sha256"] != identities() or state["protocol_hash"] != canonical_json_hash(
        protocol
    ):
        raise RuntimeError("PRE_DISPATCH_FREEZE_MISMATCH")
    owner = await access(factory, state["owner"], state["workspace"])
    approver = await access(factory, state["approver"], state["workspace"])
    ledger = ResearchBudgetLedger(
        PRIVATE / "ledger.sqlite", cap_cny=Decimal(protocol["cap_estimated_cny"])
    )
    meter = BudgetedAliyunClient(ledger, endpoint.base_url, PRIVATE / "calls")
    cipher = ProviderCredentialCipher.from_settings(settings)
    runtime = AgentRunService(
        factory,
        model_gateway_factory=lambda session: SqlAlchemyModelGateway(
            session, adapter=LiteLLMProviderAdapter(client=meter), credential_cipher=cipher
        ),
        credential_cipher=cipher,
        approval_service=ApprovalService(factory),
        action_runtime=ActionRuntime(session_factory=factory),
        tool_runtime=ToolRuntime(session_factory=factory),
        checkpoint_adapter=LangGraphCheckpointAdapter(settings.database_url),
    )
    with (PRIVATE / "started.json").open("x", encoding="utf-8") as marker:
        json.dump({"started_at": timestamp(), "protocol_hash": state["protocol_hash"]}, marker)
    rows = []
    try:
        for repeat in range(protocol["repetitions"]):
            for index, case in enumerate(protocol["cases"]):
                arms = ARMS if (repeat + index) % 2 == 0 else reversed(ARMS)
                for arm in arms:
                    row = await trial(
                        factory, runtime, meter, owner, approver, state, case, arm, repeat, protocol
                    )
                    rows.append(row)
                    save(PRIVATE / "rows.json", rows)
                    if ledger.summary()["halted"]:
                        raise RuntimeError("DISPATCH_HALTED")
    finally:
        async with factory() as session:
            exposures = await session.scalar(
                select(func.count(EvaluationExperimentHoldoutExposure.id))
            )
        save(
            PRIVATE / "result.json",
            {
                "finished_at": timestamp(),
                "protocol_hash": state["protocol_hash"],
                "results": report(protocol, rows),
                "billing": ledger.summary(),
                "holdout_exposures_in_isolated_database": exposures,
                "completed_trials": len(rows),
                "expected_trials": len(protocol["cases"]) * protocol["repetitions"] * len(ARMS),
            },
        )
    print(json.dumps(report(protocol, rows), sort_keys=True))


async def main(phase):
    db = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "")
    url = urlsplit(db)
    if (
        db != os.environ.get("AGENTHUB_DATABASE_URL")
        or url.hostname not in {"127.0.0.1", "::1", "localhost"}
        or url.path != "/agenthub_support_dev_20261114"
    ):
        raise RuntimeError("EXPLICIT_ISOLATED_DEV_DATABASE_REQUIRED")
    settings = get_settings()
    protocol = read(PROTOCOL)
    endpoint = selected_endpoint(protocol)
    engine, factory = create_database(db)
    try:
        if phase == "freeze":
            await freeze(factory, settings, endpoint, protocol)
        else:
            await execute(factory, settings, endpoint, protocol)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("freeze", "run"))
    configure_windows_asyncio_policy()
    asyncio.run(main(parser.parse_args().phase))
