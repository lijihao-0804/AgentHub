"""PostgreSQL replay coverage for logical Artifact identities."""

from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from packages.agent_runtime.models import AgentRun
from packages.agent_runtime.work_layer import RecordedToolCall
from packages.artifacts.models import Artifact
from packages.artifacts.recorder import ToolResultArtifactRecorder
from packages.threads.models import AgentThread
from tests.integration.test_m4c_agent_runtime import _seed
from tests.integration.test_m5a_approval_runtime import db_factory as db_factory
from tests.integration.test_m5a_approval_runtime import migrated_database as migrated_database

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("AGENTHUB_TEST_DATABASE_URL"), reason="PostgreSQL required"
    ),
]

_BASE_RUN_ID = UUID("00000000-0000-0000-0000-000000000301")


async def _run(factory):
    async with factory() as session:
        base = await _seed(session, label=uuid4().hex)
        thread = AgentThread(
            workspace_id=base["workspace_id"],
            agent_id=base["version"].agent_id,
            title="artifact identity",
            created_by=base["user"].id,
        )
        session.add(thread)
        await session.flush()
        run = AgentRun(
            workspace_id=base["workspace_id"],
            agent_version_id=base["version"].id,
            thread_id=thread.id,
            input_text="collect incident evidence",
            created_by=base["user"].id,
            status="RUNNING",
        )
        session.add(run)
        await session.commit()
        return base, thread.id, run.id


def _call(*, logical_call_id: str, arguments: dict[str, str]) -> RecordedToolCall:
    return RecordedToolCall(
        tool_identity="ops.get_deployments",
        tool_call_id=f"provider-{uuid4().hex}",
        step_sequence=7,
        arguments=arguments,
        data={
            "structured_content": {
                "service": arguments["service"],
                "total": 1,
                "deployments": [
                    {
                        "deployment_id": f"dep-{arguments['service']}",
                        "version": "5.4.2",
                        "commit_sha": "abc123def4567890",
                        "deployed_at": "2026-10-26T12:00:00Z",
                        "deployed_by": "ci",
                        "status": "SUCCEEDED",
                        "environment": "production",
                    }
                ],
            }
        },
        logical_call_id=logical_call_id,
    )


async def _artifact_calls(factory, workspace_id, run_id):
    async with factory() as session:
        return list(
            await session.scalars(
                select(Artifact)
                .where(
                    Artifact.workspace_id == workspace_id,
                    Artifact.run_id == run_id,
                )
                .order_by(Artifact.logical_call_id)
            )
        )


@pytest.mark.asyncio
async def test_checkpoint_replay_records_one_artifact_and_distinct_calls_stay_separate(
    db_factory,
) -> None:
    base, thread_id, run_id = await _run(db_factory)
    recorder = ToolResultArtifactRecorder(db_factory)
    first = _call(logical_call_id="round-2-call-1", arguments={"service": "api"})
    replay = RecordedToolCall(
        tool_identity=first.tool_identity,
        tool_call_id=f"provider-{uuid4().hex}",
        step_sequence=99,
        arguments=first.arguments,
        data=first.data,
        logical_call_id=first.logical_call_id,
    )
    second = _call(logical_call_id="round-2-call-2", arguments={"service": "web"})

    for call in (first, replay):
        await recorder.record(
            workspace_id=base["workspace_id"],
            thread_id=thread_id,
            run_id=run_id,
            created_by=base["user"].id,
            calls=(call,),
        )
    rows = await _artifact_calls(db_factory, base["workspace_id"], run_id)
    assert len(rows) == 1
    assert rows[0].logical_call_id == "round-2-call-1"
    assert rows[0].content["entries"][0]["provenance"]["tool_call_id"] == first.tool_call_id

    await recorder.record(
        workspace_id=base["workspace_id"],
        thread_id=thread_id,
        run_id=run_id,
        created_by=base["user"].id,
        calls=(second,),
    )
    rows = await _artifact_calls(db_factory, base["workspace_id"], run_id)
    assert {row.logical_call_id for row in rows} == {
        "round-2-call-1",
        "round-2-call-2",
    }


def test_fallback_identity_is_not_the_audit_sequence_alone() -> None:
    first = _call(logical_call_id="ignored", arguments={"service": "api"})
    second = RecordedToolCall(
        tool_identity=first.tool_identity,
        tool_call_id=first.tool_call_id,
        step_sequence=8,
        arguments=first.arguments,
        data=first.data,
    )
    later = RecordedToolCall(
        tool_identity=first.tool_identity,
        tool_call_id=first.tool_call_id,
        step_sequence=8,
        arguments=first.arguments,
        data=first.data,
    )

    assert ToolResultArtifactRecorder.logical_call_id(second, "incident.timeline") == (
        ToolResultArtifactRecorder.logical_call_id(later, "incident.timeline")
    )
