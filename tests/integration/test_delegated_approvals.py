"""Real membership, business rows, LangGraph checkpoint and local tool effects."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from apps.api.app import create_app
from packages.agent_runtime.adapters.langgraph import LangGraphCheckpointAdapter
from packages.agent_runtime.models import AgentVersion, Tool, ToolRevision
from packages.agent_runtime.runtime import AgentRunExecutionOverrides, AgentRunService
from packages.approvals import ApprovalDecisionStatus, ApprovalService
from packages.approvals import service as approval_module
from packages.approvals.delegation import DelegationLevel, read_preferences, write_preferences
from packages.approvals.models import Approval
from packages.control_plane.models import AuditLog, OrganizationMembership, WorkspaceMembership
from packages.control_plane.services import TenantService
from packages.core.auth.security import issue_access_token
from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.errors.exceptions import AgentHubError
from packages.model_gateway.contracts import ModelResponse, ModelToolCall
from packages.tools.actions import ActionExecutionResult, ActionRegistry, ActionRuntime
from packages.tools.models import Customer, Ticket
from packages.tools.runtime import PublishedToolResolver, ToolRuntime
from tests.integration.test_m4c_agent_runtime import ScriptedGateway, _seed
from tests.integration.test_m5a_approval_runtime import TEST_DATABASE_URL
from tests.integration.test_m5a_approval_runtime import db_factory as db_factory
from tests.integration.test_m5a_approval_runtime import migrated_database as migrated_database
from tests.support.settings import declared_settings

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.skipif(not TEST_DATABASE_URL, reason="Explicit PostgreSQL test URL required"),
]


def _spec(identity="create_ticket", risk="MEDIUM", policy="ALWAYS"):
    keys = ["expression"] if identity == "calculator" else ["customer_ref", "subject"]
    return {
        "kind": "builtin",
        "identity": identity,
        "risk_level": risk,
        "approval_policy": policy,
        "effect": "READ" if identity == "calculator" else "WRITE",
        "input_schema": {
            "type": "object",
            "required": keys,
            "properties": {k: {"type": "string"} for k in keys},
            "additionalProperties": False,
        },
    }


async def _fixture(factory, spec=None):
    async with factory() as session:
        base = await _seed(session, label=uuid4().hex, tool_spec=spec or _spec())
        session.add(
            Customer(workspace_id=base["workspace_id"], customer_ref="local-1", name="Synthetic")
        )
        await session.commit()
        ctx = (
            await TenantService().get_workspace_access(
                session,
                principal=base["context"].organization.principal,
                workspace_id=base["workspace_id"],
            )
        ).context
    return base, ctx


async def _set(factory, ctx, level, limit=2):
    async with factory() as session:
        return await write_preferences(
            session, ctx, level=DelegationLevel(level), max_auto_denials=limit
        )


def _service(factory, *, identity="create_ticket", arguments=None, approvals=None, executor=None):
    arguments = arguments or {"customer_ref": "local-1", "subject": "Synthetic request"}
    gateway = ScriptedGateway(
        [
            ModelResponse(
                content="",
                provider="fake",
                model="fake",
                tool_calls=(ModelToolCall(identity, arguments, provider_tool_call_id=uuid4().hex),),
            ),
            ModelResponse(content="Recorded.", provider="fake", model="fake"),
        ]
    )
    registry = (
        ActionRegistry(session_factory=factory, overrides={identity: executor})
        if executor
        else None
    )
    return AgentRunService(
        factory,
        model_gateway_factory=lambda _: gateway,
        approval_service=approvals or ApprovalService(factory),
        action_runtime=ActionRuntime(session_factory=factory, registry=registry),
        checkpoint_adapter=LangGraphCheckpointAdapter(TEST_DATABASE_URL),
    )


async def _propose(factory, base, ctx, **kwargs):
    runtime = _service(factory, **kwargs)
    result = await runtime.run(
        ctx, agent_version_id=base["version"].id, input_text="Synthetic request"
    )
    rows = await ApprovalService(factory).list_for_run(ctx, result.run_id)
    assert len(rows) == 1, (result.status, result.failure_code, rows)
    return runtime, result, rows[0]


async def test_delegated_execution_persists_one_decision_claim_and_ticket(db_factory):
    base, ctx = await _fixture(db_factory)
    async with db_factory() as session:
        assert (await read_preferences(session, ctx)).level == "MANUAL"
    # Even a run created just before opting in stays manual, as do formal evals.
    old_service = _service(db_factory)
    old = await old_service.prepare_run(ctx, agent_version_id=base["version"].id, input_text="old")
    await _set(db_factory, ctx, "MEDIUM")
    old_result = await old_service.execute_prepared_run(ctx, run_id=old.id)
    assert old_result.status == "WAITING_APPROVAL"
    eval_service = _service(db_factory)
    evaluation = await eval_service.prepare_run(
        ctx,
        agent_version_id=base["version"].id,
        input_text="evaluation",
        execution_overrides=AgentRunExecutionOverrides.for_evaluation([]),
    )
    assert (
        await eval_service.execute_prepared_run(ctx, run_id=evaluation.id)
    ).status == "WAITING_APPROVAL"
    runtime, result, row = await _propose(db_factory, base, ctx)
    assert (
        result.status,
        row.decision_status,
        row.execution_status,
        row.execution_attempt_count,
    ) == ("SUCCEEDED", "APPROVED", "SUCCEEDED", 1)
    assert (row.decision_source, row.routing_reason, row.decided_by) == (
        "DELEGATED",
        "WITHIN_LEVEL",
        base["user"].id,
    )
    assert (
        await runtime.resume(ctx, run_id=result.run_id, approval_id=row.id)
    ).status == "SUCCEEDED"
    async with db_factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Ticket)
                .where(Ticket.workspace_id == base["workspace_id"])
            )
            == 1
        )
        audits = (
            await session.scalars(
                select(AuditLog).where(
                    AuditLog.resource_id == str(row.id), AuditLog.action == "approval.decide"
                )
            )
        ).all()
        assert len(audits) == 1 and audits[0].safe_metadata["level"] == "MEDIUM"
        assert "Synthetic request" not in str(audits[0].safe_metadata)


async def test_same_operation_concurrent_refusals_escalate_and_a_human_resets_only_that_streak(
    db_factory,
):
    base, ctx = await _fixture(db_factory)
    await _set(db_factory, ctx, "LOW", limit=1)
    results = await asyncio.gather(_propose(db_factory, base, ctx), _propose(db_factory, base, ctx))
    decisions = {row.decision_status: (runtime, result, row) for runtime, result, row in results}
    assert set(decisions) == {"DENIED", "PENDING"}
    refused, waiting = decisions["DENIED"][2], decisions["PENDING"][2]
    assert refused.execution_status == "NOT_STARTED" and refused.decision_source == "DELEGATED"
    assert waiting.routing_reason == "DENIAL_LIMIT_REACHED" and waiting.decision_source is None
    assert decisions["PENDING"][1].status == "WAITING_APPROVAL"
    # Another request and another principal/workspace cannot consume the streak.
    other = {"subject": "different", "customer_ref": "local-1"}
    assert (await _propose(db_factory, base, ctx, arguments=other))[2].decision_status == "DENIED"
    approvals = ApprovalService(db_factory)
    decided = await approvals.decide(ctx, waiting.id, decision=ApprovalDecisionStatus.APPROVED)
    assert decided.decision_source == "HUMAN"
    resumed = await decisions["PENDING"][0].resume(
        ctx, run_id=waiting.run_id, approval_id=waiting.id
    )
    assert resumed.status == "SUCCEEDED"
    assert (await _propose(db_factory, base, ctx))[2].decision_status == "DENIED"
    assert (await _propose(db_factory, base, ctx, arguments=other))[
        2
    ].routing_reason == "DENIAL_LIMIT_REACHED"
    await _set(db_factory, ctx, "LOW", limit=0)
    assert (await _propose(db_factory, base, ctx, arguments={**other, "subject": "fresh"}))[
        2
    ].routing_reason == "DENIAL_LIMIT_REACHED"


@pytest.mark.parametrize("risk,policy,human", [("HIGH", "NEVER", True), ("LOW", "ALWAYS", False)])
async def test_governed_read_requires_its_exact_claim_then_returns_real_data(
    db_factory, risk, policy, human
):
    base, ctx = await _fixture(db_factory, _spec("calculator", risk, policy))
    await _set(db_factory, ctx, "MEDIUM")
    arguments = {"expression": "20+22"}
    direct = ToolRuntime(session_factory=db_factory)
    request = dict(
        context=ctx,
        agent_version_id=base["version"].id,
        tool_identity="calculator",
        arguments=arguments,
        tool_call_id="direct",
    )
    assert (await direct.execute(**request)).error_code == "TOOL_APPROVAL_NOT_AVAILABLE"
    runtime, result, row = await _propose(
        db_factory, base, ctx, identity="calculator", arguments=arguments
    )
    if human:
        assert result.status == "WAITING_APPROVAL" and row.routing_reason == "SENSITIVE_OPERATION"
        assert (
            await direct.execute(**request, approval_claim=(row.run_id, row.id))
        ).error_code == "TOOL_APPROVAL_NOT_AVAILABLE"
        await ApprovalService(db_factory).decide(
            ctx, row.id, decision=ApprovalDecisionStatus.APPROVED
        )
        result = await runtime.resume(ctx, run_id=row.run_id, approval_id=row.id)
    stored = await ApprovalService(db_factory).get(ctx, row.id)
    assert (result.status, stored.execution_status, stored.safe_result["value"]) == (
        "SUCCEEDED",
        "SUCCEEDED",
        42,
    )
    assert stored.decision_source == ("HUMAN" if human else "DELEGATED")
    assert (
        await direct.execute(**request, approval_claim=(row.run_id, row.id))
    ).error_code == "TOOL_APPROVAL_NOT_AVAILABLE"


async def test_delegated_write_keeps_the_automatic_read_in_the_same_model_batch(db_factory):
    base, ctx = await _fixture(db_factory)
    await _set(db_factory, ctx, "MEDIUM")
    source = base["version"]
    async with db_factory() as session:
        read_spec = _spec("calculator", "LOW", "NEVER")
        tool = Tool(workspace_id=source.workspace_id, name="calculator")
        session.add(tool)
        await session.flush()
        read = ToolRevision(
            workspace_id=tool.workspace_id,
            tool_id=tool.id,
            revision_number=1,
            spec=read_spec,
            spec_hash=canonical_json_hash(read_spec),
            created_by=source.created_by,
        )
        session.add(read)
        await session.flush()
        spec = deepcopy(source.resolved_spec)
        spec["tools"].append(
            {
                "tool_revision_id": str(read.id),
                "tool_spec_hash": read.spec_hash,
                **{key: read_spec[key] for key in ("approval_policy", "risk_level", "effect")},
            }
        )
        version = AgentVersion(
            workspace_id=source.workspace_id,
            agent_id=source.agent_id,
            version_number=2,
            spec_schema_version=source.spec_schema_version,
            resolved_spec=spec,
            resolved_spec_hash=canonical_json_hash(spec),
            created_by=source.created_by,
        )
        session.add(version)
        await session.commit()
    gateway = ScriptedGateway(
        [
            ModelResponse(
                content="",
                provider="fake",
                model="fake",
                tool_calls=(
                    ModelToolCall(
                        "create_ticket",
                        {"subject": "batch", "customer_ref": "local-1"},
                        provider_tool_call_id="write",
                    ),
                    ModelToolCall(
                        "calculator", {"expression": "20+22"}, provider_tool_call_id="read"
                    ),
                ),
            ),
            ModelResponse(content="Recorded.", provider="fake", model="fake"),
        ]
    )
    service = _service(db_factory)
    service.model_gateway_factory = lambda _: gateway
    result = await service.run(ctx, agent_version_id=version.id, input_text="Synthetic batch")
    assert result.status == "SUCCEEDED"
    observations = {
        message.tool_call_id: message.content
        for message in gateway.requests[1].messages
        if message.role == "tool"
    }
    assert "42" in observations["read"] and '"APPROVED"' in observations["write"]


async def _demote(factory, base):
    async with factory() as session:
        org = await session.get(OrganizationMembership, (base["organization_id"], base["user"].id))
        org.role = "MEMBER"
        session.add(
            WorkspaceMembership(
                workspace_id=base["workspace_id"], user_id=base["user"].id, role="DEVELOPER"
            )
        )
        await session.commit()


async def test_failed_decision_transaction_and_concurrent_replay_do_not_consume_extra_denials(
    db_factory, monkeypatch
):
    base, ctx = await _fixture(db_factory)
    await _set(db_factory, ctx, "LOW", limit=1)
    runtime = _service(db_factory)
    run = await runtime.prepare_run(
        ctx, agent_version_id=base["version"].id, input_text="Atomic proposal"
    )
    async with db_factory() as session:
        definition = await PublishedToolResolver(session).resolve(
            workspace_id=run.workspace_id,
            agent_version_id=run.agent_version_id,
            tool_identity="create_ticket",
        )
    approvals = ApprovalService(db_factory)
    params = dict(
        run_id=run.id,
        agent_version_id=run.agent_version_id,
        tool_revision_id=definition.revision_id,
        tool_identity=definition.identity,
        arguments={"customer_ref": "local-1", "subject": "same"},
        input_schema=definition.input_schema,
        proposal_ordinal=1,
        definition=definition,
    )
    original = approval_module.route_new_approval

    async def fail_before_commit(*args):
        await original(*args)
        raise RuntimeError("rollback proposal and audit together")

    with monkeypatch.context() as patch:
        patch.setattr(approval_module, "route_new_approval", fail_before_commit)
        with pytest.raises(RuntimeError, match="rollback proposal"):
            await approvals.create_or_get(ctx, **params)
    first, replay = await asyncio.gather(
        approvals.create_or_get(ctx, **params), approvals.create_or_get(ctx, **params)
    )
    assert first.id == replay.id and first.decision_status == "DENIED"
    async with db_factory() as session:
        assert list(
            await session.scalars(select(Approval.id).where(Approval.run_id == run.id))
        ) == [first.id]
        assert (
            await session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.workspace_id == run.workspace_id, AuditLog.action == "approval.decide"
                )
            )
            == 1
        )
    later = await approvals.create_or_get(ctx, **{**params, "proposal_ordinal": 2})
    assert later.decision_status == "PENDING" and later.routing_reason == "DENIAL_LIMIT_REACHED"
    async with db_factory() as session:
        event = await session.scalar(
            select(AuditLog).where(
                AuditLog.action == "approval.escalate", AuditLog.resource_id == str(later.id)
            )
        )
        assert (
            event.safe_metadata["prior_auto_denials"]
            == event.safe_metadata["max_auto_denials"]
            == 1
        )


async def test_revocation_after_delegated_decision_blocks_the_execution_claim(
    db_factory, monkeypatch
):
    base, ctx = await _fixture(db_factory)
    await _set(db_factory, ctx, "MEDIUM")
    approvals = ApprovalService(db_factory)
    create = approvals.create_or_get

    async def demote_immediately_after_decision(*args, **kwargs):
        row = await create(*args, **kwargs)
        await _demote(db_factory, base)
        return row

    monkeypatch.setattr(approvals, "create_or_get", demote_immediately_after_decision)
    _, result, row = await _propose(db_factory, base, ctx, approvals=approvals)
    assert (result.status, result.failure_code, row.execution_attempt_count) == (
        "NEEDS_ATTENTION",
        "RUN_ACTOR_ACCESS_REVOKED",
        0,
    )
    # Stale elevated context cannot delegate or approve, even after reaching a refusal limit.
    async with db_factory() as session:
        view = await read_preferences(session, ctx)
        assert (view.level, view.effective_level, view.identity_ceiling) == (
            "MEDIUM",
            "MANUAL",
            "MANUAL",
        )
    with pytest.raises(AgentHubError, match="authority"):
        await _set(db_factory, ctx, "LOW", 0)
    _, result, pending = await _propose(db_factory, base, ctx)
    assert (result.status, pending.routing_reason) == ("WAITING_APPROVAL", "IDENTITY_CEILING")
    with pytest.raises(AgentHubError, match="permission"):
        await ApprovalService(db_factory).decide(
            ctx, pending.id, decision=ApprovalDecisionStatus.APPROVED
        )


async def test_delegated_unknown_outcome_never_retries(db_factory):
    class Unconfirmed:
        calls = 0

        async def execute(self, *args, **kwargs):
            self.calls += 1
            return ActionExecutionResult.unknown_outcome()

    base, ctx = await _fixture(db_factory)
    await _set(db_factory, ctx, "MEDIUM")
    executor = Unconfirmed()
    runtime, result, row = await _propose(db_factory, base, ctx, executor=executor)
    assert (row.decision_status, row.execution_status, result.status, result.failure_code) == (
        "APPROVED",
        "UNKNOWN_OUTCOME",
        "NEEDS_ATTENTION",
        "ACTION_RECONCILIATION_REQUIRED",
    )
    assert (
        await runtime.resume(ctx, run_id=row.run_id, approval_id=row.id)
    ).status == "NEEDS_ATTENTION"
    assert executor.calls == 1


async def test_http_preferences_use_token_workspace_scope_and_validated_payload(db_factory):
    first, _ = await _fixture(db_factory)
    second, _ = await _fixture(db_factory)
    settings = declared_settings(testing=True, database_url=TEST_DATABASE_URL)
    application = create_app(settings)
    application.state.db_session_factory = db_factory
    headers = {"Authorization": "Bearer " + issue_access_token(first["user"].id, settings)}
    path = f"/api/v1/workspaces/{first['workspace_id']}/approval-preferences/me"
    payload = {"level": "LOW", "max_auto_denials": 1}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        assert (await client.get(path)).status_code == 401
        assert (await client.get(path, headers=headers)).json() == {
            "level": "MANUAL",
            "identity_ceiling": "MEDIUM",
            "effective_level": "MANUAL",
            "max_auto_denials": 2,
        }
        assert (await client.put(path, headers=headers, json=payload)).json()[
            "effective_level"
        ] == "LOW"
        assert (
            await client.put(
                path.replace(str(first["workspace_id"]), str(second["workspace_id"])),
                headers=headers,
                json=payload,
            )
        ).status_code == 404
        for invalid in (
            {**payload, "user_id": str(second["user"].id)},
            {**payload, "level": "HIGH"},
            {**payload, "max_auto_denials": True},
            {**payload, "max_auto_denials": 11},
        ):
            assert (await client.put(path, headers=headers, json=invalid)).status_code == 422
        await _demote(db_factory, first)
        forbidden = await client.put(path, headers=headers, json=payload)
        assert (
            forbidden.status_code == 403
            and forbidden.json()["error"]["code"] == "APPROVAL_DELEGATION_FORBIDDEN"
        )
        assert (await client.get(path, headers=headers)).json()["identity_ceiling"] == "MANUAL"
