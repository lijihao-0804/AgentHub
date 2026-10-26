"""Stable Turn/Run, memory-contribution and artifact-call identities.

The Turn FK is added after both tables exist so the existing Run -> Thread FK
does not combine with this new FK into a creation cycle.
"""

import hashlib
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection

revision: str = "0036_turn_memory_artifact"
down_revision: str | None = "0035_run_execution_lease"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _backfill_input_hashes(connection: Connection) -> None:
    """Hash the same normalized text the service has always persisted.

    ``user_input`` is stripped by the API/service before insertion. Legacy
    rows are normalized again so historical turns and new rows agree on the
    request-body identity. Python computes the same UTF-8 request identity as the service, without
    depending on database-side digest functions or encoding conversions.
    """

    rows = list(
        connection.execute(
            sa.text("SELECT id, user_input FROM thread_turns ORDER BY id")
        ).mappings()
    )
    statement = sa.text("UPDATE thread_turns SET input_hash = :input_hash WHERE id = :turn_id")
    for row in rows:
        normalized = (row["user_input"] or "").strip()
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        connection.execute(
            statement,
            {"input_hash": digest, "turn_id": row["id"]},
        )


def upgrade() -> None:
    op.execute(
        """
        UPDATE thread_turns
        SET client_token = 'legacy:' || id::text
        WHERE client_token IS NULL
        """
    )
    op.alter_column("thread_turns", "client_token", nullable=False)
    # Drop and recreate deliberately: the old constraint tolerated duplicate
    # NULL tokens, while the new request contract requires every row to carry
    # one. The unique key itself and its name remain unchanged.
    op.drop_constraint(
        "uq_thread_turns_client_token",
        "thread_turns",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_thread_turns_client_token",
        "thread_turns",
        ["thread_id", "client_token"],
    )
    op.add_column("thread_turns", sa.Column("input_hash", sa.String(64), nullable=True))
    _backfill_input_hashes(op.get_bind())
    op.alter_column("thread_turns", "input_hash", nullable=False)

    op.add_column("agent_runs", sa.Column("thread_turn_id", sa.Uuid(), nullable=True))
    # Every pre-migration turn starts marked as needing recovery. Only a
    # one-to-one reverse pointer whose Run agrees on workspace, thread and
    # input is strong enough to become the new stable identity. The GROUP BY
    # prevents PostgreSQL UPDATE ... FROM from choosing one of several Turns
    # that reference the same Run.
    op.add_column(
        "thread_turns",
        sa.Column(
            "legacy_recovery_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.execute(
        """
        WITH unique_legacy AS (
            SELECT agent_run_id, min(id::text)::uuid AS turn_id
            FROM thread_turns
            WHERE agent_run_id IS NOT NULL
            GROUP BY agent_run_id
            HAVING count(*) = 1
        )
        UPDATE agent_runs AS run
        SET thread_turn_id = unique_legacy.turn_id
        FROM unique_legacy
        JOIN thread_turns AS turn ON turn.id = unique_legacy.turn_id
        WHERE unique_legacy.agent_run_id = run.id
          AND turn.workspace_id = run.workspace_id
          AND turn.thread_id = run.thread_id
          AND turn.user_input = run.input_text
        """
    )
    op.execute(
        """
        UPDATE thread_turns AS turn
        SET legacy_recovery_required = false
        FROM agent_runs AS run
        WHERE run.thread_turn_id = turn.id
          AND turn.agent_run_id = run.id
          AND turn.workspace_id = run.workspace_id
          AND turn.thread_id = run.thread_id
          AND turn.user_input = run.input_text
        """
    )
    # Existing rows used TRUE during backfill to fail closed. New Turns can
    # safely prepare their own Run and must match the ORM default.
    op.alter_column(
        "thread_turns",
        "legacy_recovery_required",
        server_default=sa.false(),
    )
    op.create_foreign_key(
        "fk_agent_runs_thread_turn_workspace",
        "agent_runs",
        "thread_turns",
        ["workspace_id", "thread_turn_id"],
        ["workspace_id", "id"],
        # Thread deletion cascades to its Turns. Preserve completed Runs and
        # clear only their Turn pointer, matching the existing Run -> Thread
        # ``SET NULL (thread_id)`` evidence-retention contract.
        ondelete="SET NULL (thread_turn_id)",
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_unique_constraint(
        "uq_agent_runs_thread_turn",
        "agent_runs",
        ["workspace_id", "thread_turn_id"],
    )

    op.create_table(
        "memory_extraction_jobs",
        sa.Column("workspace_id", sa.Uuid(), primary_key=True),
        sa.Column("agent_run_id", sa.Uuid(), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("failure_code", sa.String(64)),
        sa.Column(
            "candidate_payload",
            postgresql.JSONB(astext_type=sa.Text()),
        ),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RUNNING', 'COMPLETED', 'SKIPPED', 'FAILED')",
            name="ck_memory_extraction_jobs_status",
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "agent_run_id"],
            ["agent_runs.workspace_id", "agent_runs.id"],
            name="fk_memory_extraction_jobs_run_workspace",
            ondelete="CASCADE",
        ),
    )

    op.create_table(
        "memory_contributions",
        sa.Column("workspace_id", sa.Uuid(), primary_key=True),
        sa.Column("agent_run_id", sa.Uuid(), primary_key=True),
        sa.Column("content_hash", sa.String(64), primary_key=True),
        sa.Column("memory_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "agent_run_id"],
            ["agent_runs.workspace_id", "agent_runs.id"],
            name="fk_memory_contributions_run_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "memory_id"],
            ["workspace_memories.workspace_id", "workspace_memories.id"],
            name="fk_memory_contributions_memory_workspace",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_memory_contributions_memory",
        "memory_contributions",
        ["workspace_id", "memory_id"],
    )

    op.add_column("artifacts", sa.Column("logical_call_id", sa.String(64)))
    op.create_unique_constraint(
        "uq_artifacts_run_logical_call",
        "artifacts",
        ["workspace_id", "run_id", "logical_call_id"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_artifacts_run_logical_call", "artifacts", type_="unique")
    op.drop_column("artifacts", "logical_call_id")
    op.drop_index("ix_memory_contributions_memory", table_name="memory_contributions")
    op.drop_table("memory_contributions")
    op.drop_table("memory_extraction_jobs")
    op.drop_constraint("uq_agent_runs_thread_turn", "agent_runs", type_="unique")
    op.drop_constraint("fk_agent_runs_thread_turn_workspace", "agent_runs", type_="foreignkey")
    op.drop_column("agent_runs", "thread_turn_id")
    op.drop_column("thread_turns", "legacy_recovery_required")
    op.drop_constraint(
        "uq_thread_turns_client_token",
        "thread_turns",
        type_="unique",
    )
    op.drop_column("thread_turns", "input_hash")
    op.create_unique_constraint(
        "uq_thread_turns_client_token", "thread_turns", ["thread_id", "client_token"]
    )
