"""Add durable interactive execution episodes and turns.

Revision ID: 0051_interactive_lifecycle
Revises: 0050_interactive_category
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0051_interactive_lifecycle"
down_revision = "0050_interactive_category"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evaluation_interactive_episodes",
        sa.Column("case_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("workspace_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("instance_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("reserved_thread_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("thread_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("seed", sa.Integer(), nullable=False),
        sa.Column("fixture_ref", sa.String(length=64), nullable=False),
        sa.Column("fixture_hash", sa.String(length=64), nullable=False),
        sa.Column("env_kind", sa.String(length=32), nullable=False),
        sa.Column("env_version", sa.String(length=64), nullable=False),
        sa.Column("simulator_version", sa.String(length=64), nullable=False),
        sa.Column("driver_version", sa.String(length=64), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("policy_hash", sa.String(length=64), nullable=False),
        sa.Column("customer_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("customer_ref", sa.String(length=128), nullable=True),
        sa.Column("fixture_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("prepare_stage", sa.String(length=32), nullable=False),
        sa.Column(
            "execution_status",
            sa.String(length=24),
            nullable=False,
            server_default="PREPARING",
        ),
        sa.Column("termination_reason", sa.String(length=96), nullable=True),
        sa.Column("assertion", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("assertion_hash", sa.String(length=64), nullable=True),
        sa.Column("assertion_source", sa.String(length=128), nullable=True),
        sa.Column("assertion_recorded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "observation_envelope",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("observation_hash", sa.String(length=64), nullable=True),
        sa.Column("observation_recorded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "cleanup_status",
            sa.String(length=32),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("cleanup_error", sa.String(length=96), nullable=True),
        sa.Column(
            "cleanup_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("cleanup_next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleanup_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cleanup_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "case_id"],
            [
                "evaluation_experiment_case_results.workspace_id",
                "evaluation_experiment_case_results.id",
            ],
            name="fk_interactive_episodes_case_workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "thread_id"],
            ["agent_threads.workspace_id", "agent_threads.id"],
            name="fk_interactive_episodes_thread_workspace",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("case_id"),
        sa.UniqueConstraint(
            "workspace_id",
            "case_id",
            name="uq_interactive_episodes_workspace_case",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "instance_id",
            name="uq_interactive_episodes_workspace_instance",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "reserved_thread_id",
            name="uq_interactive_episodes_workspace_reserved_thread",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "thread_id",
            name="uq_interactive_episodes_workspace_thread",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "customer_id",
            name="uq_interactive_episodes_workspace_customer_id",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "customer_ref",
            name="uq_interactive_episodes_workspace_customer_ref",
        ),
        sa.CheckConstraint(
            "fixture_ref = 'support_customer'",
            name="ck_interactive_episodes_fixture_ref",
        ),
        sa.CheckConstraint(
            "seed >= 0",
            name="ck_interactive_episodes_seed",
        ),
        sa.CheckConstraint(
            "thread_id IS NULL OR thread_id = reserved_thread_id",
            name="ck_interactive_episodes_reserved_thread",
        ),
        sa.CheckConstraint(
            "instance_id = case_id",
            name="ck_interactive_episodes_instance_case",
        ),
        sa.CheckConstraint(
            "env_kind = 'support_fixture'",
            name="ck_interactive_episodes_environment_kind",
        ),
        sa.CheckConstraint(
            "env_version = 'environment-v2' AND "
            "simulator_version = 'scripted-disclosure-v1' AND "
            "driver_version = 'interactive-driver-v2'",
            name="ck_interactive_episodes_protocol_versions",
        ),
        sa.CheckConstraint(
            "execution_status IN ('PREPARING', 'RUNNING', 'COMPLETED', "
            "'RECOVERY_REQUIRED', 'TERMINATED')",
            name="ck_interactive_episodes_execution_status",
        ),
        sa.CheckConstraint(
            "cleanup_status IN ('PENDING', 'TEARDOWN_COMPLETE', "
            "'TEARDOWN_FAILED', 'CLEANUP_BLOCKED')",
            name="ck_interactive_episodes_cleanup_status",
        ),
        sa.CheckConstraint(
            "cleanup_attempts >= 0",
            name="ck_interactive_episodes_cleanup_attempts",
        ),
        sa.CheckConstraint(
            "input_hash ~ '^[0-9a-f]{64}$' AND "
            "policy_hash ~ '^[0-9a-f]{64}$' AND "
            "fixture_hash ~ '^[0-9a-f]{64}$'",
            name="ck_interactive_episodes_hash_formats",
        ),
        sa.CheckConstraint(
            "(customer_id IS NULL AND customer_ref IS NULL AND "
            "fixture_created_at IS NULL) OR "
            "(customer_id IS NOT NULL AND customer_ref IS NOT NULL AND "
            "fixture_created_at IS NOT NULL)",
            name="ck_interactive_episodes_customer_binding",
        ),
        sa.CheckConstraint(
            "(assertion IS NULL AND assertion_hash IS NULL AND "
            "assertion_source IS NULL AND assertion_recorded_at IS NULL) OR "
            "(assertion IS NOT NULL AND assertion_hash IS NOT NULL AND "
            "assertion_source IS NOT NULL AND assertion_recorded_at IS NOT NULL)",
            name="ck_interactive_episodes_assertion_binding",
        ),
        sa.CheckConstraint(
            "(observation_envelope IS NULL AND observation_hash IS NULL AND "
            "observation_recorded_at IS NULL) OR "
            "(observation_envelope IS NOT NULL AND observation_hash IS NOT NULL AND "
            "observation_recorded_at IS NOT NULL)",
            name="ck_interactive_episodes_observation_binding",
        ),
        sa.CheckConstraint(
            "assertion_hash IS NULL OR assertion_hash ~ '^[0-9a-f]{64}$'",
            name="ck_interactive_episodes_assertion_hash",
        ),
        sa.CheckConstraint(
            "observation_hash IS NULL OR observation_hash ~ '^[0-9a-f]{64}$'",
            name="ck_interactive_episodes_observation_hash",
        ),
    )
    op.create_index(
        "ix_interactive_episodes_cleanup_retry",
        "evaluation_interactive_episodes",
        ["cleanup_status", "cleanup_next_attempt_at", "workspace_id", "case_id"],
    )
    op.create_index(
        "ix_interactive_episodes_execution_status",
        "evaluation_interactive_episodes",
        ["workspace_id", "execution_status"],
    )

    op.create_table(
        "evaluation_interactive_episode_turns",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("workspace_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("case_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("turn_index", sa.Integer(), nullable=False),
        sa.Column("client_token", sa.String(length=64), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("thread_turn_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("agent_run_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column(
            "dispatch_status",
            sa.String(length=24),
            nullable=False,
            server_default="RESERVED",
        ),
        sa.Column("dispatch_intent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("terminal_status", sa.String(length=32), nullable=True),
        sa.Column("failure_code", sa.String(length=96), nullable=True),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("evidence_hash", sa.String(length=64), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "case_id"],
            [
                "evaluation_interactive_episodes.workspace_id",
                "evaluation_interactive_episodes.case_id",
            ],
            name="fk_interactive_episode_turns_episode_workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "thread_turn_id"],
            ["thread_turns.workspace_id", "thread_turns.id"],
            name="fk_interactive_episode_turns_thread_turn_workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "agent_run_id"],
            ["agent_runs.workspace_id", "agent_runs.id"],
            name="fk_interactive_episode_turns_agent_run_workspace",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "case_id",
            "turn_index",
            name="uq_interactive_episode_turns_case_index",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "case_id",
            "client_token",
            name="uq_interactive_episode_turns_case_token",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "thread_turn_id",
            name="uq_interactive_episode_turns_workspace_thread_turn",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "agent_run_id",
            name="uq_interactive_episode_turns_workspace_agent_run",
        ),
        sa.CheckConstraint(
            "turn_index >= 0",
            name="ck_interactive_episode_turns_index",
        ),
        sa.CheckConstraint(
            "dispatch_status IN ('RESERVED', 'BOUND', 'DISPATCH_INTENT', "
            "'TERMINAL', 'UNKNOWN_OUTCOME', 'RECOVERY_REQUIRED')",
            name="ck_interactive_episode_turns_dispatch_status",
        ),
        sa.CheckConstraint(
            "input_hash ~ '^[0-9a-f]{64}$'",
            name="ck_interactive_episode_turns_input_hash",
        ),
        sa.CheckConstraint(
            "evidence_hash IS NULL OR evidence_hash ~ '^[0-9a-f]{64}$'",
            name="ck_interactive_episode_turns_evidence_hash",
        ),
        sa.CheckConstraint(
            "(thread_turn_id IS NULL AND agent_run_id IS NULL) OR "
            "(thread_turn_id IS NOT NULL AND agent_run_id IS NOT NULL)",
            name="ck_interactive_episode_turns_binding_pair",
        ),
        sa.CheckConstraint(
            "(evidence IS NULL AND evidence_hash IS NULL) OR "
            "(evidence IS NOT NULL AND evidence_hash IS NOT NULL)",
            name="ck_interactive_episode_turns_evidence_binding",
        ),
        sa.CheckConstraint(
            "dispatch_status NOT IN "
            "('DISPATCH_INTENT', 'TERMINAL', 'UNKNOWN_OUTCOME', "
            "'RECOVERY_REQUIRED') OR "
            "(thread_turn_id IS NOT NULL AND agent_run_id IS NOT NULL AND "
            "dispatch_intent_at IS NOT NULL)",
            name="ck_interactive_episode_turns_dispatch_binding",
        ),
    )
    op.create_index(
        "ix_interactive_episode_turns_case_index",
        "evaluation_interactive_episode_turns",
        ["workspace_id", "case_id", "turn_index"],
    )
    op.create_index(
        "ix_interactive_episode_turns_agent_run",
        "evaluation_interactive_episode_turns",
        ["workspace_id", "agent_run_id"],
    )

    op.execute(
        """
        CREATE FUNCTION interactive_episode_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION
              'Cannot remove retained interactive execution evidence';
          END IF;

          IF (
            NEW.workspace_id, NEW.case_id, NEW.instance_id,
            NEW.reserved_thread_id, NEW.seed, NEW.fixture_ref,
            NEW.fixture_hash, NEW.env_kind, NEW.env_version,
            NEW.simulator_version, NEW.driver_version, NEW.input_hash,
            NEW.policy_hash
          ) IS DISTINCT FROM (
            OLD.workspace_id, OLD.case_id, OLD.instance_id,
            OLD.reserved_thread_id, OLD.seed, OLD.fixture_ref,
            OLD.fixture_hash, OLD.env_kind, OLD.env_version,
            OLD.simulator_version, OLD.driver_version, OLD.input_hash,
            OLD.policy_hash
          ) THEN
            RAISE EXCEPTION 'Interactive episode identity is immutable';
          END IF;

          IF OLD.thread_id IS NOT NULL
             AND NEW.thread_id IS DISTINCT FROM OLD.thread_id THEN
            RAISE EXCEPTION 'Interactive episode thread binding is immutable';
          END IF;

          IF OLD.customer_id IS NOT NULL
             AND (NEW.customer_id, NEW.customer_ref, NEW.fixture_created_at)
                 IS DISTINCT FROM
                 (OLD.customer_id, OLD.customer_ref, OLD.fixture_created_at) THEN
            RAISE EXCEPTION 'Interactive episode fixture binding is immutable';
          END IF;

          IF OLD.assertion_hash IS NOT NULL
             AND (
               NEW.assertion, NEW.assertion_hash, NEW.assertion_source,
               NEW.assertion_recorded_at
             ) IS DISTINCT FROM (
               OLD.assertion, OLD.assertion_hash, OLD.assertion_source,
               OLD.assertion_recorded_at
             ) THEN
            RAISE EXCEPTION 'Interactive assertion evidence is immutable';
          END IF;

          IF OLD.observation_hash IS NOT NULL
             AND (
               NEW.observation_envelope, NEW.observation_hash,
               NEW.observation_recorded_at
             ) IS DISTINCT FROM (
               OLD.observation_envelope, OLD.observation_hash,
               OLD.observation_recorded_at
             ) THEN
            RAISE EXCEPTION 'Interactive observation evidence is immutable';
          END IF;

          IF OLD.execution_status IS DISTINCT FROM NEW.execution_status
             AND NOT (
               (OLD.execution_status = 'PREPARING'
                AND NEW.execution_status IN
                  ('RUNNING', 'COMPLETED', 'RECOVERY_REQUIRED', 'TERMINATED'))
               OR
               (OLD.execution_status = 'RUNNING'
                AND NEW.execution_status IN
                  ('COMPLETED', 'RECOVERY_REQUIRED', 'TERMINATED'))
             ) THEN
            RAISE EXCEPTION 'Illegal interactive execution transition';
          END IF;

          IF OLD.cleanup_status = 'TEARDOWN_COMPLETE'
             AND NEW.cleanup_status IS DISTINCT FROM OLD.cleanup_status THEN
            RAISE EXCEPTION 'Completed interactive cleanup is terminal';
          END IF;

          IF NEW.cleanup_attempts < OLD.cleanup_attempts THEN
            RAISE EXCEPTION 'Interactive cleanup attempts cannot decrease';
          END IF;

          RETURN NEW;
        END
        $$;

        CREATE TRIGGER interactive_episode_evidence_guard
          BEFORE UPDATE OR DELETE ON evaluation_interactive_episodes
          FOR EACH ROW EXECUTE FUNCTION interactive_episode_guard();
        """
    )

    op.execute(
        """
        CREATE FUNCTION interactive_episode_turn_guard() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION
              'Cannot remove retained interactive execution evidence';
          END IF;

          IF (
            NEW.id, NEW.workspace_id, NEW.case_id, NEW.turn_index,
            NEW.client_token, NEW.input_hash
          ) IS DISTINCT FROM (
            OLD.id, OLD.workspace_id, OLD.case_id, OLD.turn_index,
            OLD.client_token, OLD.input_hash
          ) THEN
            RAISE EXCEPTION 'Interactive turn identity is immutable';
          END IF;

          IF OLD.thread_turn_id IS NOT NULL
             AND NEW.thread_turn_id IS DISTINCT FROM OLD.thread_turn_id THEN
            RAISE EXCEPTION 'Interactive turn ThreadTurn binding is immutable';
          END IF;

          IF OLD.agent_run_id IS NOT NULL
             AND NEW.agent_run_id IS DISTINCT FROM OLD.agent_run_id THEN
            RAISE EXCEPTION 'Interactive turn AgentRun binding is immutable';
          END IF;

          IF OLD.dispatch_intent_at IS NOT NULL
             AND NEW.dispatch_intent_at IS DISTINCT FROM OLD.dispatch_intent_at THEN
            RAISE EXCEPTION 'Interactive turn dispatch intent is immutable';
          END IF;

          IF OLD.dispatch_status IS DISTINCT FROM NEW.dispatch_status
             AND NOT (
               (OLD.dispatch_status = 'RESERVED'
                AND NEW.dispatch_status = 'BOUND')
               OR
               (OLD.dispatch_status = 'BOUND'
                AND NEW.dispatch_status = 'DISPATCH_INTENT')
               OR
               (OLD.dispatch_status = 'DISPATCH_INTENT'
                AND NEW.dispatch_status IN
                  ('TERMINAL', 'UNKNOWN_OUTCOME', 'RECOVERY_REQUIRED'))
             ) THEN
            RAISE EXCEPTION 'Illegal interactive turn dispatch transition';
          END IF;

          IF OLD.evidence_hash IS NOT NULL
             AND (
               NEW.evidence, NEW.evidence_hash, NEW.terminal_status,
               NEW.failure_code, NEW.completed_at
             ) IS DISTINCT FROM (
               OLD.evidence, OLD.evidence_hash, OLD.terminal_status,
               OLD.failure_code, OLD.completed_at
             ) THEN
            RAISE EXCEPTION 'Interactive turn evidence is immutable';
          END IF;

          RETURN NEW;
        END
        $$;

        CREATE TRIGGER interactive_episode_turn_evidence_guard
          BEFORE UPDATE OR DELETE ON evaluation_interactive_episode_turns
          FOR EACH ROW EXECUTE FUNCTION interactive_episode_turn_guard();
        """
    )


def downgrade() -> None:
    op.execute(
        "LOCK TABLE evaluation_interactive_episodes, "
        "evaluation_interactive_episode_turns IN ACCESS EXCLUSIVE MODE"
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM evaluation_interactive_episodes)
             OR EXISTS (SELECT 1 FROM evaluation_interactive_episode_turns) THEN
            RAISE EXCEPTION
              'Cannot remove retained interactive execution evidence';
          END IF;
        END
        $$;
        """
    )
    op.execute(
        "DROP TRIGGER interactive_episode_turn_evidence_guard "
        "ON evaluation_interactive_episode_turns"
    )
    op.execute("DROP TRIGGER interactive_episode_evidence_guard ON evaluation_interactive_episodes")
    op.execute("DROP FUNCTION interactive_episode_turn_guard()")
    op.execute("DROP FUNCTION interactive_episode_guard()")
    op.drop_index(
        "ix_interactive_episode_turns_agent_run",
        table_name="evaluation_interactive_episode_turns",
    )
    op.drop_index(
        "ix_interactive_episode_turns_case_index",
        table_name="evaluation_interactive_episode_turns",
    )
    op.drop_table("evaluation_interactive_episode_turns")
    op.drop_index(
        "ix_interactive_episodes_execution_status",
        table_name="evaluation_interactive_episodes",
    )
    op.drop_index(
        "ix_interactive_episodes_cleanup_retry",
        table_name="evaluation_interactive_episodes",
    )
    op.drop_table("evaluation_interactive_episodes")
