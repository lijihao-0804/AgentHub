import asyncio
import os
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from apps.api.app import create_app
from packages.agent_runtime.models import AgentRun
from packages.artifacts.models import Artifact
from packages.artifacts.schemas import SUPPORT_HANDOFF
from packages.artifacts.service import ArtifactService
from packages.control_plane.models import (
    AuditLog,
    OrganizationMembership,
    User,
    WorkspaceMembership,
)
from packages.core.auth.security import issue_access_token
from packages.core.config.settings import Settings
from packages.core.errors.exceptions import AgentHubError
from packages.handoffs.service import HandoffService, source_hash
from packages.threads.models import AgentThread
from packages.threads.service import ThreadService
from tests.integration.test_m4c_agent_runtime import _seed
from tests.integration.test_m5a_approval_runtime import db_factory as db_factory
from tests.integration.test_m5a_approval_runtime import migrated_database as migrated_database

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("AGENTHUB_TEST_DATABASE_URL"), reason="isolated DB required"
    ),
]


async def seed(factory):
    async with factory() as s:
        base = await _seed(s, label=uuid4().hex)
        thread = AgentThread(
            workspace_id=base["workspace_id"],
            agent_id=base["version"].agent_id,
            title="synthetic handoff",
            created_by=base["user"].id,
        )
        s.add(thread)
        await s.flush()
        source = Artifact(
            workspace_id=base["workspace_id"],
            thread_id=thread.id,
            type=SUPPORT_HANDOFF,
            title="unresolved synthetic case",
            content={
                "problem": "pending synthetic task",
                "checked": [],
                "findings": [],
                "recommended_action": "inspect claim",
                "reason": "UNKNOWN_OUTCOME",
                "customer_ref": None,
                "case_ref": None,
            },
            created_by=base["user"].id,
        )
        s.add(source)
        await s.commit()
        return base, thread, source


async def member(factory, base, role="DEVELOPER"):
    async with factory() as s:
        user = User(
            email=f"handoff-{uuid4().hex}@example.test",
            normalized_email=uuid4().hex,
            password_hash="unused",
        )
        s.add(user)
        await s.flush()
        s.add(
            OrganizationMembership(
                organization_id=base["organization_id"], user_id=user.id, role="MEMBER"
            )
        )
        s.add(WorkspaceMembership(workspace_id=base["workspace_id"], user_id=user.id, role=role))
        await s.commit()
        org = base["context"].organization.model_copy(
            update={
                "org_role": "MEMBER",
                "principal": base["context"].organization.principal.model_copy(
                    update={"user_id": str(user.id)}
                ),
            }
        )
        # Deliberately stale permissions: the service must resolve real membership.
        return user, base["context"].model_copy(
            update={"organization": org, "workspace_role": role}
        )


@pytest.mark.asyncio
async def test_handoff_lifecycle_retains_source_and_idempotent_close(db_factory):
    base, thread, source = await seed(db_factory)
    ctx = base["context"]
    service = HandoffService(db_factory)
    a, b = await asyncio.gather(service.open(ctx, source.id), service.open(ctx, source.id))
    assert a.id == b.id and a.status == "OPEN"
    worker, worker_ctx = await member(db_factory, base)
    a = await service.assign(ctx, a.id, expected_version=1, assignee_id=worker.id)
    assert a.status == "ASSIGNED" and a.version == 2
    a = await service.claim(worker_ctx, a.id, expected_version=2)
    assert a.status == "IN_PROGRESS"
    payload = dict(
        expected_version=3,
        reason="Reviewed safely; provider outcome remains unconfirmed.",
        unresolved_items=["external outcome unknown"],
    )
    a = await service.close(worker_ctx, a.id, **payload)
    again = await service.close(worker_ctx, a.id, **payload)
    assert a.version == again.version == 4 and a.id == again.id
    assert a.unresolved_items == ["external outcome unknown"]
    with pytest.raises(AgentHubError):
        await service.close(worker_ctx, a.id, **{**payload, "reason": "different"})
    async with db_factory() as s:
        original = await s.get(Artifact, source.id)
        assert source_hash(original) == a.source_hash
        logs = list(await s.scalars(select(AuditLog).where(AuditLog.resource_id == str(a.id))))
        assert len(logs) == 4
        assert "external outcome unknown" not in str([entry.safe_metadata for entry in logs])
    for method, args in [
        (ArtifactService(db_factory).update, {"title": "changed"}),
        (ArtifactService(db_factory).delete, {}),
    ]:
        with pytest.raises(AgentHubError) as error:
            await method(ctx, source.id, **args)
        assert error.value.code == "HANDOFF_EVIDENCE_RETAINED"
    with pytest.raises(AgentHubError) as error:
        await ThreadService(db_factory).delete_thread(ctx, thread.id)
    assert error.value.code == "HANDOFF_EVIDENCE_RETAINED"


@pytest.mark.asyncio
async def test_two_members_claim_only_once_and_reassignment_revokes_handler(db_factory):
    base, _, source = await seed(db_factory)
    service = HandoffService(db_factory)
    a = await service.open(base["context"], source.id)
    first, first_ctx = await member(db_factory, base)
    second, second_ctx = await member(db_factory, base)
    results = await asyncio.gather(
        service.claim(first_ctx, a.id, expected_version=1),
        service.claim(second_ctx, a.id, expected_version=1),
        return_exceptions=True,
    )
    winners = [r for r in results if not isinstance(r, Exception)]
    losers = [r for r in results if isinstance(r, Exception)]
    assert len(winners) == len(losers) == 1 and losers[0].code == "HANDOFF_VERSION_CONFLICT"
    winner = winners[0]
    old_ctx = first_ctx if winner.claimed_by == first.id else second_ctx
    target = second if winner.claimed_by == first.id else first
    a = await service.assign(base["context"], a.id, expected_version=2, assignee_id=target.id)
    with pytest.raises(AgentHubError) as error:
        await service.close(
            old_ctx, a.id, expected_version=3, reason="wrong handler", unresolved_items=[]
        )
    assert error.value.status_code == 403
    with pytest.raises(AgentHubError):
        await service.claim(old_ctx, a.id, expected_version=3)


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["viewer", "removed", "inactive"])
async def test_real_membership_checked_even_with_stale_context(db_factory, revocation):
    base, _, source = await seed(db_factory)
    service = HandoffService(db_factory)
    a = await service.open(base["context"], source.id)
    user, ctx = await member(db_factory, base)
    a = await service.claim(ctx, a.id, expected_version=1)
    async with db_factory() as s:
        membership = await s.get(WorkspaceMembership, (base["workspace_id"], user.id))
        if revocation == "viewer":
            membership.role = "VIEWER"
        elif revocation == "removed":
            await s.delete(membership)
        else:
            (await s.get(User, user.id)).is_active = False
        await s.commit()
    with pytest.raises(AgentHubError) as error:
        await service.close(
            ctx, a.id, expected_version=2, reason="not permitted", unresolved_items=[]
        )
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_read_only_assignment_permission_and_cross_workspace(db_factory):
    base, thread, source = await seed(db_factory)
    service = HandoffService(db_factory)
    a = await service.open(base["context"], source.id)
    viewer, viewer_ctx = await member(db_factory, base, role="VIEWER")
    assert len(await service.list_for_thread(viewer_ctx, thread.id)) == 1
    for call in [
        service.claim(viewer_ctx, a.id, expected_version=1),
        service.assign(viewer_ctx, a.id, expected_version=1, assignee_id=viewer.id),
        service.assign(base["context"], a.id, expected_version=1, assignee_id=viewer.id),
    ]:
        with pytest.raises(AgentHubError) as error:
            await call
        assert error.value.status_code == 403
    other, _, _ = await seed(db_factory)
    with pytest.raises(AgentHubError) as error:
        await service.claim(other["context"], a.id, expected_version=1)
    assert error.value.code == "HANDOFF_NOT_FOUND"


@pytest.mark.asyncio
async def test_close_audit_failure_rolls_back_state_atomically(db_factory, monkeypatch):
    base, _, source = await seed(db_factory)
    service = HandoffService(db_factory)
    row = await service.open(base["context"], source.id)
    row = await service.claim(base["context"], row.id, expected_version=1)

    def unavailable(*args):
        raise RuntimeError("controlled audit failure")

    monkeypatch.setattr(service, "audit", unavailable)
    with pytest.raises(RuntimeError, match="controlled audit failure"):
        await service.close(
            base["context"],
            row.id,
            expected_version=2,
            reason="not committed",
            unresolved_items=["remains unknown"],
        )
    persisted = await service.get_for_artifact(base["context"], source.id)
    assert persisted.status == "IN_PROGRESS" and persisted.version == 2
    assert persisted.closure_reason is None
    async with db_factory() as session:
        logs = list(
            await session.scalars(select(AuditLog).where(AuditLog.resource_id == str(row.id)))
        )
        assert len(logs) == 2


@pytest.mark.asyncio
async def test_failed_agent_run_is_not_resumed_by_handoff_close(db_factory):
    base, _, source = await seed(db_factory)
    async with db_factory() as session:
        run = AgentRun(
            workspace_id=base["workspace_id"],
            agent_version_id=base["version"].id,
            thread_id=source.thread_id,
            created_by=base["user"].id,
            input_text="synthetic failure",
            status="NEEDS_ATTENTION",
            failure_code="UNKNOWN_OUTCOME",
        )
        session.add(run)
        await session.flush()
        stored_source = await session.get(Artifact, source.id)
        stored_source.run_id = run.id
        await session.commit()
    service = HandoffService(db_factory)
    row = await service.open(base["context"], source.id)
    row = await service.claim(base["context"], row.id, expected_version=1)
    await service.close(
        base["context"],
        row.id,
        expected_version=2,
        reason="Escalated to external reconciliation",
        unresolved_items=["External effect unknown"],
    )
    async with db_factory() as session:
        actual = await session.get(AgentRun, run.id)
        assert actual.status == "NEEDS_ATTENTION" and actual.failure_code == "UNKNOWN_OUTCOME"


@pytest.mark.asyncio
async def test_handoff_routes_real_auth_and_strict_envelope(db_factory):
    base, thread, source = await seed(db_factory)
    settings = Settings(testing=True)
    app = create_app(settings)
    app.state.db_session_factory = db_factory
    token = issue_access_token(base["user"].id, settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        prefix = f"/api/v1/workspaces/{base['workspace_id']}"
        path = f"{prefix}/artifacts/{source.id}/handoff"
        assert (await client.get(path)).json() is None
        opened = await client.post(path)
        assert opened.status_code == 200, opened.text
        case_id = opened.json()["id"]
        cases = (await client.get(f"{prefix}/threads/{thread.id}/handoffs")).json()
        assert len(cases) == 1
        assert base["user"].email in str((await client.get(f"{prefix}/handoffs/assignees")).json())
        bad = await client.post(
            f"{prefix}/handoffs/{case_id}/claim", json={"expected_version": True}
        )
        assert bad.status_code == 422 and "error" in bad.json()
        claimed = await client.post(
            f"{prefix}/handoffs/{case_id}/claim", json={"expected_version": 1}
        )
        assert claimed.status_code == 200 and claimed.json()["status"] == "IN_PROGRESS"
        closed = await client.post(
            f"{prefix}/handoffs/{case_id}/close",
            json={
                "expected_version": 2,
                "reason": "controlled verification",
                "unresolved_items": ["follow up"],
            },
        )
        assert closed.status_code == 200 and closed.json()["status"] == "CLOSED"
