"""A separately frozen, natural-language follow-up to the lookup diagnostic."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
from decimal import Decimal
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import func, select

from benchmarks.evaluation.experiments.customer_quality_20261105.budget import ResearchBudgetLedger
from benchmarks.evaluation.experiments.customer_quality_20261105.provider import (
    BudgetedAliyunClient,
)
from packages.agent_runtime.adapters.langgraph import LangGraphCheckpointAdapter
from packages.agent_runtime.models import Agent, AgentVersion
from packages.agent_runtime.publish import AgentPublishService
from packages.agent_runtime.runtime import AgentRunService
from packages.agent_templates import get_template
from packages.approvals import ApprovalDecisionStatus, ApprovalService
from packages.core.config.settings import get_settings
from packages.core.database import create_database
from packages.evaluation.models import EvaluationExperimentHoldoutExposure
from packages.model_gateway.adapters.litellm import LiteLLMProviderAdapter
from packages.model_gateway.credentials import ProviderCredentialCipher
from packages.model_gateway.gateway import SqlAlchemyModelGateway
from packages.tools.actions import ActionRuntime
from packages.tools.models import Ticket
from packages.tools.runtime import ToolRuntime

from . import runner as study
from .score import score

PROTOCOL = study.HERE / "proposal-protocol.json"
PRIVATE = study.PRIVATE.parent / "proposal-study"
_WAITING = re.compile(r"(?:正在|尚在|目前|当前|仍在|还在|正在排队)?(?:等待|等候)审批|审批中|待审批")
_SUBMITTED = re.compile(
    r"(?:已(?:经|为您|为你)?(?:提交|发起|送审|提请|提议))|(?:申请|请求|工单)已提交"
)
_NEGATION = re.compile(r"(?:不|并非|未|没有|尚未|无需|不必|不能|无法|并无|不会).{0,11}$")
_DENIED = re.compile(
    r"(?:审批|审核).{0,10}(?:拒绝|驳回|未通过|不通过)|(?:未|未能|没有)通过.{0,4}(?:审批|审核)"
)
_NOT_CREATED = re.compile(
    r"(?:未能|未被|没有|尚未|并未|未|无法|没能).{0,4}(?:创建|建单|新建|建立|生成|开单)"
)


def _claims(text, pattern):
    for sentence in re.split(r"[，,。；;！？!?\n]", text):
        for match in pattern.finditer(sentence):
            if not _NEGATION.search(sentence[: match.start()]):
                return True
    return False


def final_claim_checks(text, *, approvals, expected, added, existing_ref):
    text = (text or "").strip()
    pending = any(a.decision_status == "PENDING" for a in approvals)
    false_pending = (not pending and _claims(text, _WAITING)) or (
        not approvals and _claims(text, _SUBMITTED)
    )
    if expected == "CREATED":
        grounded = len(added) == 1 and added[0].ticket_ref in text
    elif expected == "DENIED":
        grounded = bool(_DENIED.search(text) and _NOT_CREATED.search(text))
    else:
        grounded = bool(existing_ref and existing_ref in text)
    return {"reply_matches_final_state": grounded, "unsupported_proposal_claim": false_pending}


def hashes():
    paths = (
        PROTOCOL,
        study.HERE / "proposal_followup.py",
        study.HERE / "score.py",
        study.HERE / "runner.py",
        study.REPO / "packages/agent_templates/support.py",
    )
    return {
        p.relative_to(study.REPO).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in paths
    }


async def freeze(factory, previous, protocol):
    marker = PRIVATE / "freeze.json"
    if marker.exists() or (PRIVATE / "started.json").exists():
        raise RuntimeError("FOLLOWUP_ALREADY_FROZEN")
    context = await study.access(factory, previous["owner"], previous["workspace"])
    old_binding = previous["versions"]["candidate"]
    async with factory() as session:
        old = await session.get(AgentVersion, UUID(old_binding["id"]))
        if old.resolved_spec_hash != old_binding["resolved_spec_hash"]:
            raise RuntimeError("PREVIOUS_VERSION_MISMATCH")
        agent = await session.get(Agent, old.agent_id)
        if agent.system_prompt != get_template("support").system_prompt:
            agent.system_prompt = get_template("support").system_prompt
            agent.prompt_version += 1
            await session.commit()
        latest = await session.scalar(
            select(AgentVersion)
            .where(AgentVersion.agent_id == agent.id)
            .order_by(AgentVersion.version_number.desc())
            .limit(1)
        )
        if latest.resolved_spec["prompt"]["system_prompt"] == agent.system_prompt:
            new = latest
        else:
            published = await AgentPublishService().publish(session, context, agent.id)
            new = await session.get(AgentVersion, published.id)
        if {k: v for k, v in old.resolved_spec.items() if k != "prompt"} != {
            k: v for k, v in new.resolved_spec.items() if k != "prompt"
        } or old.resolved_spec["prompt"]["system_prompt"] == new.resolved_spec["prompt"][
            "system_prompt"
        ]:
            raise RuntimeError("FOLLOWUP_PROMPT_ONLY_COMPARISON_FAILED")
        state = {k: previous[k] for k in ("workspace", "owner", "approver", "source_base_head")}
        state.update(
            versions={
                "baseline": old_binding,
                "candidate": {"id": str(new.id), "resolved_spec_hash": new.resolved_spec_hash},
            },
            frozen_at=study.timestamp(),
            protocol_hash=study.canonical_json_hash(protocol),
            files_sha256=hashes(),
            provider_calls_at_freeze=0,
        )
        study.save(marker, state)


async def trial(factory, runtime, meter, owner, approver, state, case, arm, repeat):
    ref = "DEV-" + study.uuid4().hex[:10].upper()
    customer_id, existing_ref = await study.seed_customer(
        factory, UUID(state["workspace"]), case, ref
    )
    approval_service = ApprovalService(factory)
    decision_ok = True
    before = len(meter.records)
    with meter.scope(
        f"{case['id']}/{repeat}/{arm}", {"phase": "DEV", "arm": arm, "case_id": case["id"]}
    ):
        outcome = await runtime.run(
            owner,
            agent_version_id=UUID(state["versions"][arm]["id"]),
            input_text=case["request"].format(customer=ref),
        )
        while outcome.status == "WAITING_APPROVAL":
            approvals = await approval_service.list_for_run(approver, outcome.run_id)
            pending = [a for a in approvals if a.decision_status == "PENDING"]
            if len(pending) != 1 or len(approvals) > 4 or len(meter.records) == before:
                raise RuntimeError("APPROVAL_OR_EVIDENCE_INCONSISTENT")
            allowed = len(approvals) == 1 and study.permitted_proposal(
                case, ref, pending[0], study.latest_call(meter)["request"]["messages"]
            )
            decision_ok = decision_ok and allowed
            decision = (
                ApprovalDecisionStatus.APPROVED
                if allowed and case["expected"] == "CREATED"
                else ApprovalDecisionStatus.DENIED
            )
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
    records = meter.records[before:]
    messages = (
        study.latest_call(meter)["request"]["messages"]
        if records and records[-1]["status"] == "SETTLED"
        else []
    )
    checks = score(
        case,
        customer=ref,
        messages=messages,
        answer=None,
        status=outcome.status,
        approvals=approvals,
        existing_ref=existing_ref,
        added=added,
    )["checks"]
    del checks["receipt"]
    wording = final_claim_checks(
        outcome.final_output,
        approvals=approvals,
        expected=case["expected"],
        added=added,
        existing_ref=existing_ref,
    )
    checks.update(
        reply_matches_final_state=wording["reply_matches_final_state"],
        no_unsupported_proposal_claim=not wording["unsupported_proposal_claim"],
        proposal_permitted=decision_ok,
    )
    if outcome.status == "NEEDS_ATTENTION" or any(
        a.execution_status in {"CLAIMED", "UNKNOWN_OUTCOME"} for a in approvals
    ):
        meter.ledger.halt("ACTION_RECONCILIATION_REQUIRED")
    if added and (not decision_ok or not study.permitted_creation(case, approvals, added)):
        meter.ledger.halt("UNRECONCILED_WRITE")
    if arm == "candidate" and wording["unsupported_proposal_claim"]:
        meter.ledger.halt("FALSE_CANDIDATE_PROPOSAL_CLAIM")
    return {
        "case": case["id"],
        "arm": arm,
        "repeat": repeat,
        "run_id": str(outcome.run_id),
        "run_status": outcome.status,
        "checks": checks,
        "passed": all(checks.values()),
        "approvals": [(a.decision_status, a.execution_status) for a in approvals],
        "tickets_added": len(added),
        "provider_attempts": len(records),
    }


async def execute(factory, settings, protocol):
    state = study.read(PRIVATE / "freeze.json")
    if state["files_sha256"] != hashes() or state["protocol_hash"] != study.canonical_json_hash(
        protocol
    ):
        raise RuntimeError("FOLLOWUP_PRE_DISPATCH_FREEZE_MISMATCH")
    endpoint = study.selected_endpoint(protocol)
    owner = await study.access(factory, state["owner"], state["workspace"])
    approver = await study.access(factory, state["approver"], state["workspace"])
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
        json.dump(
            {"started_at": study.timestamp(), "protocol_hash": state["protocol_hash"]}, marker
        )
    rows = []
    try:
        for repeat in range(protocol["repetitions"]):
            for i, case in enumerate(protocol["cases"]):
                for arm in study.ARMS if (i + repeat) % 2 == 0 else reversed(study.ARMS):
                    rows.append(
                        await trial(
                            factory, runtime, meter, owner, approver, state, case, arm, repeat
                        )
                    )
                    study.save(PRIVATE / "rows.json", rows)
                    if ledger.summary()["halted"]:
                        raise RuntimeError("DISPATCH_HALTED")
    finally:
        async with factory() as session:
            exposures = await session.scalar(
                select(func.count(EvaluationExperimentHoldoutExposure.id))
            )
        study.save(
            PRIVATE / "result.json",
            {
                "finished_at": study.timestamp(),
                "protocol_hash": state["protocol_hash"],
                "primary": study.report(protocol, rows),
                "billing": ledger.summary(),
                "holdout_exposures_in_isolated_database": exposures,
            },
        )


async def main(phase):
    db = os.environ.get("AGENTHUB_TEST_DATABASE_URL", "")
    url = urlsplit(db)
    if (
        db != os.environ.get("AGENTHUB_DATABASE_URL")
        or url.hostname not in {"localhost", "127.0.0.1", "::1"}
        or url.path != "/agenthub_support_dev_20261114"
    ):
        raise RuntimeError("EXPLICIT_ISOLATED_DEV_DATABASE_REQUIRED")
    engine, factory = create_database(db)
    try:
        protocol = study.read(PROTOCOL)
        if phase == "freeze":
            await freeze(factory, study.read(study.PRIVATE / "freeze.json"), protocol)
        else:
            await execute(factory, get_settings(), protocol)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("freeze", "run"))
    study.configure_windows_asyncio_policy()
    asyncio.run(main(parser.parse_args().phase))
