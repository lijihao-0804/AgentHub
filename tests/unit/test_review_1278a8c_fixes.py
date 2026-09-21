"""Boundaries closed after the comprehensive review of 1278a8c.

Two of the findings were boundaries a later refactor can quietly undo, so they
are asserted here rather than left to the code reading correctly:

* the run response withholds raw content from a reader who only holds
  ``workspace_read``, and still gives that reader everything else;
* the ORM metadata declares what the migrations actually build -- a constraint
  or index that lives only in a migration reads to a drift check as one to
  drop.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from apps.api.routes.agent_runs import _run_response
from apps.api.schemas.agent_runs import AgentRunResponse
from packages.agent_runtime.models import AgentRun
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.knowledge.models import Document

WORKSPACE_ID = uuid4()


def make_run() -> AgentRun:
    return AgentRun(
        id=uuid4(),
        workspace_id=WORKSPACE_ID,
        agent_version_id=uuid4(),
        status="SUCCEEDED",
        input_text="what is the refund policy",
        final_output="the refund policy is ...",
        failure_code=None,
        resolved_spec_hash="hash",
        effective_knowledge_snapshots=[],
        model_step_count=1,
        tool_call_count=0,
        total_input_tokens=10,
        total_output_tokens=20,
        total_tokens=30,
        total_cached_tokens=0,
        total_cost_amount=None,
        cost_currency=None,
        cost_is_estimate=None,
        created_by=uuid4(),
        created_at=datetime.now(UTC),
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
    )


def make_context(*permissions: str) -> WorkspaceExecutionContext:
    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(
                request_id="req-1", trace_id="trace-1", user_id=str(uuid4())
            ),
            organization_id=str(uuid4()),
            org_role="MEMBER",
        ),
        workspace_id=str(WORKSPACE_ID),
        workspace_role="VIEWER",
        permissions=frozenset(permissions),
    )


def test_a_reader_without_agent_run_gets_the_run_but_not_its_content() -> None:
    projected = _run_response(make_context("workspace_read"), make_run())

    assert projected.input_text is None
    assert projected.final_output is None
    # Everything a VIEWER is entitled to is still there: this withholds
    # content, it does not withhold the run.
    assert projected.status == "SUCCEEDED"
    assert projected.total_tokens == 30
    # The fields stay present in the contract; only their content is gated.
    assert "input_text" in AgentRunResponse.model_fields


def test_whoever_may_run_the_agent_may_read_what_was_sent_to_it() -> None:
    run = make_run()
    projected = _run_response(make_context("workspace_read", "agent_run"), run)

    assert projected.input_text == run.input_text
    assert projected.final_output == run.final_output


def test_the_thread_foreign_key_migration_0023_builds_is_in_the_metadata() -> None:
    names = {constraint.name for constraint in AgentRun.__table__.constraints}
    assert "fk_agent_runs_thread_workspace" in names


def test_the_partial_index_migration_0026_builds_is_in_the_metadata() -> None:
    index = next(
        (ix for ix in Document.__table__.indexes if ix.name == "ix_documents_superseded_by"),
        None,
    )
    assert index is not None
    # The predicate is part of the index's identity: nearly every row is NULL,
    # so an unconditional index would be a different object.
    assert index.dialect_options["postgresql"]["where"] is not None
