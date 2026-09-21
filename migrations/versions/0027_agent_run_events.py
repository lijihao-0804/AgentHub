"""Persist the run event stream so a run outlives its HTTP connection.

Until now ``POST /agent-versions/{id}/runs/stream`` *was* the run: the async
generator that produced the SSE body also drove execution, so a client
disconnect cancelled the generator and ``abort_if_active()`` killed the run.
Closing a browser tab could kill a run that was mid-WRITE.

The events themselves were never stored. They carry ``event_id`` and
``sequence`` -- ``events.py`` even says out loud that sequence is "ordering
only; it is not an SSE replay guarantee" -- and then they were dropped on the
floor once written to the socket. Only ``run_steps`` survived, and that is a
coarser projection (11 kinds against 19 event types, no usage, no context
budget), so it cannot reconstruct the stream a consumer was reading.

Storing the stream is what makes every other part of detaching a run from its
connection cheap:

  * a consumer that reconnects asks for ``sequence > N`` and misses nothing;
  * several consumers can watch one run, because none of them owns it;
  * a consumer in a *different process* can follow a run it did not start by
    tailing this table, which is why moving execution into the worker needs no
    pub/sub -- a durable ordered log already delivers, in order, exactly once.

``message.delta`` is deliberately excluded by the writer. Keeping it would make
the log O(tokens) to buy back a typing animation. Sequence gaps are therefore
normal and this column is a cursor, not a count.

``ON DELETE CASCADE`` matches ``run_steps``: the events are part of the run,
not an independent audit record. The audit trail that must outlive a run lives
in the audit log, not here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Kept within 32 characters: alembic_version.version_num is varchar(32).
revision: str = "0027_agent_run_events"
down_revision: str | None = "0026_document_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_run_events",
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("workspace_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("agent_run_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=48), nullable=False),
        sa.Column("request_id", sa.String(length=128), nullable=False),
        sa.Column("step_id", sa.String(length=64), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "agent_run_id"],
            ["agent_runs.workspace_id", "agent_runs.id"],
            name="fk_agent_run_events_agent_run_workspace",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "workspace_id", "agent_run_id", "sequence", name="uq_agent_run_events_sequence"
        ),
    )
    # Every read of this table is "give me this run's events after cursor N",
    # in workspace scope. The unique constraint already covers that prefix on
    # PostgreSQL, but the explicit index keeps the tail query independent of
    # that implementation detail and is what the planner is aimed at.
    op.create_index(
        "ix_agent_run_events_workspace_run",
        "agent_run_events",
        ["workspace_id", "agent_run_id", "sequence"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_run_events_workspace_run", table_name="agent_run_events")
    op.drop_table("agent_run_events")
