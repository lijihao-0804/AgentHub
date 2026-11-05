"""Add the parent Interactive case budget ledger.

Revision ID: 0053_interactive_case_budget
Revises: 0052_memory_recovery_index
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0053_interactive_case_budget"
down_revision = "0052_memory_recovery_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True)
    now = sa.func.now()

    op.create_table(
        "case_budget_accounts",
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("case_id", uuid_type, nullable=False),
        sa.Column("experiment_variant_id", uuid_type, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("limit_amount", sa.Numeric(20, 8), nullable=False),
        sa.Column(
            "reserved_amount", sa.Numeric(20, 8), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "settled_amount", sa.Numeric(20, 8), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("pricing_snapshot_id", uuid_type, nullable=False),
        sa.Column("pricing_snapshot_hash", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("provider_egress_policy_version", sa.String(64), nullable=False),
        sa.Column("provider_base_url", sa.Text()),
        sa.Column(
            "budget_policy_version",
            sa.String(64),
            nullable=False,
            server_default="interactive_case_budget_v1",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(
            ["workspace_id", "case_id"],
            [
                "evaluation_interactive_episodes.workspace_id",
                "evaluation_interactive_episodes.case_id",
            ],
            name="fk_case_budget_accounts_episode_workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "experiment_variant_id"],
            [
                "evaluation_experiment_variants.workspace_id",
                "evaluation_experiment_variants.id",
            ],
            name="fk_case_budget_accounts_variant_workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "pricing_snapshot_id"],
            ["pricing_snapshots.workspace_id", "pricing_snapshots.id"],
            name="fk_case_budget_accounts_pricing_workspace",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "case_id", name="pk_case_budget_accounts"),
        sa.CheckConstraint("currency = 'CNY'", name="ck_case_budget_accounts_cny"),
        sa.CheckConstraint(
            "limit_amount > 0 AND "
            "limit_amount NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)",
            name="ck_case_budget_accounts_limit_positive_finite",
        ),
        sa.CheckConstraint(
            "reserved_amount >= 0 AND reserved_amount <= limit_amount",
            name="ck_case_budget_accounts_reserved_bounds",
        ),
        sa.CheckConstraint(
            "settled_amount >= 0 AND "
            "settled_amount NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)",
            name="ck_case_budget_accounts_settled_bounds",
        ),
        sa.CheckConstraint(
            "pricing_snapshot_hash ~ '^[0-9a-f]{64}$'",
            name="ck_case_budget_accounts_pricing_hash",
        ),
        sa.CheckConstraint(
            "provider = btrim(provider) AND length(provider) BETWEEN 1 AND 100 AND "
            "model = btrim(model) AND length(model) BETWEEN 1 AND 200",
            name="ck_case_budget_accounts_model_identity",
        ),
        sa.CheckConstraint(
            "provider_egress_policy_version = btrim(provider_egress_policy_version) "
            "AND length(provider_egress_policy_version) BETWEEN 1 AND 64",
            name="ck_case_budget_accounts_egress_policy",
        ),
        sa.CheckConstraint(
            "provider_base_url IS NULL OR ("
            "provider_base_url = btrim(provider_base_url) AND "
            "length(provider_base_url) BETWEEN 1 AND 2048)",
            name="ck_case_budget_accounts_provider_base_url",
        ),
        sa.CheckConstraint(
            "budget_policy_version = 'interactive_case_budget_v1'",
            name="ck_case_budget_accounts_policy_version",
        ),
    )
    op.create_index(
        "ix_case_budget_accounts_workspace_variant",
        "case_budget_accounts",
        ["workspace_id", "experiment_variant_id"],
    )

    op.create_table(
        "case_budget_run_bindings",
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("case_id", uuid_type, nullable=False),
        sa.Column("turn_index", sa.Integer(), nullable=False),
        sa.Column("evaluation_run_id", uuid_type, nullable=False),
        sa.Column("agent_run_id", uuid_type, nullable=False),
        sa.Column("dispatch_owner", sa.String(128), nullable=False),
        sa.Column("dispatch_generation", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(
            ["workspace_id", "case_id"],
            ["case_budget_accounts.workspace_id", "case_budget_accounts.case_id"],
            name="fk_case_budget_run_bindings_account_workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "case_id", "turn_index"],
            [
                "evaluation_interactive_episode_turns.workspace_id",
                "evaluation_interactive_episode_turns.case_id",
                "evaluation_interactive_episode_turns.turn_index",
            ],
            name="fk_case_budget_run_bindings_turn_workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "evaluation_run_id"],
            ["evaluation_experiment_runs.workspace_id", "evaluation_experiment_runs.id"],
            name="fk_case_budget_run_bindings_evaluation_run_workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "agent_run_id"],
            ["agent_runs.workspace_id", "agent_runs.id"],
            name="fk_case_budget_run_bindings_agent_run_workspace",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "workspace_id",
            "case_id",
            "turn_index",
            name="pk_case_budget_run_bindings",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "agent_run_id",
            name="uq_case_budget_run_bindings_agent_run",
        ),
        sa.CheckConstraint(
            "turn_index >= 0", name="ck_case_budget_run_bindings_turn_index"
        ),
        sa.CheckConstraint(
            "dispatch_owner = btrim(dispatch_owner) AND "
            "length(dispatch_owner) BETWEEN 1 AND 128",
            name="ck_case_budget_run_bindings_owner",
        ),
        sa.CheckConstraint(
            "dispatch_generation >= 0",
            name="ck_case_budget_run_bindings_generation",
        ),
    )
    op.create_index(
        "ix_case_budget_run_bindings_evaluation_run",
        "case_budget_run_bindings",
        ["workspace_id", "evaluation_run_id"],
    )

    op.add_column("run_budget_accounts", sa.Column("case_id", uuid_type))
    op.create_foreign_key(
        "fk_run_budget_accounts_case_workspace",
        "run_budget_accounts",
        "case_budget_accounts",
        ["workspace_id", "case_id"],
        ["workspace_id", "case_id"],
        ondelete="RESTRICT",
    )

    op.add_column("run_budget_attempts", sa.Column("case_id", uuid_type))
    op.add_column(
        "run_budget_attempts", sa.Column("case_attempt_identity", sa.String(128))
    )
    op.add_column("run_budget_attempts", sa.Column("basis_identity", sa.String(256)))
    op.create_foreign_key(
        "fk_run_budget_attempts_case_workspace",
        "run_budget_attempts",
        "case_budget_accounts",
        ["workspace_id", "case_id"],
        ["workspace_id", "case_id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_run_budget_attempts_case_identity",
        "run_budget_attempts",
        ["workspace_id", "case_id", "case_attempt_identity"],
    )
    op.create_check_constraint(
        "ck_run_budget_attempts_case_fields_shape",
        "run_budget_attempts",
        "(case_id IS NULL AND case_attempt_identity IS NULL AND basis_identity IS NULL) OR "
        "(case_id IS NOT NULL AND case_attempt_identity IS NOT NULL "
        "AND basis_identity IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_run_budget_attempts_case_identity",
        "run_budget_attempts",
        "case_attempt_identity IS NULL OR ("
        "case_attempt_identity = btrim(case_attempt_identity) AND "
        "length(case_attempt_identity) BETWEEN 1 AND 128)",
    )
    op.create_check_constraint(
        "ck_run_budget_attempts_basis_identity",
        "run_budget_attempts",
        "basis_identity IS NULL OR ("
        "basis_identity = btrim(basis_identity) AND "
        "length(basis_identity) BETWEEN 1 AND 256)",
    )

    op.create_table(
        "case_budget_response_observations",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("case_id", uuid_type, nullable=False),
        sa.Column("run_id", uuid_type, nullable=False),
        sa.Column("attempt_identity", sa.String(128), nullable=False),
        sa.Column("observation_identity", sa.String(256), nullable=False),
        sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("provider_egress_policy_version", sa.String(64), nullable=False),
        sa.Column("provider_base_url", sa.Text()),
        sa.Column("input_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("total_tokens", sa.Integer()),
        sa.Column("cached_tokens", sa.Integer()),
        sa.Column("cost_amount", sa.Numeric()),
        sa.Column("cost_currency", sa.String(3)),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(
            ["workspace_id", "case_id"],
            ["case_budget_accounts.workspace_id", "case_budget_accounts.case_id"],
            name="fk_case_budget_response_observations_case_workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id", "attempt_identity"],
            [
                "run_budget_attempts.workspace_id",
                "run_budget_attempts.run_id",
                "run_budget_attempts.attempt_identity",
            ],
            name="fk_case_budget_response_observations_attempt_workspace",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_case_budget_response_observations"),
        sa.UniqueConstraint(
            "workspace_id",
            "run_id",
            "attempt_identity",
            name="uq_case_budget_response_observations_attempt",
        ),
        sa.CheckConstraint(
            "observation_identity = btrim(observation_identity) AND "
            "length(observation_identity) BETWEEN 1 AND 256",
            name="ck_case_budget_response_observations_identity",
        ),
        sa.CheckConstraint(
            "provider = btrim(provider) AND length(provider) BETWEEN 1 AND 100 AND "
            "model = btrim(model) AND length(model) BETWEEN 1 AND 200",
            name="ck_case_budget_response_observations_model_identity",
        ),
        sa.CheckConstraint(
            "provider_egress_policy_version = btrim(provider_egress_policy_version) "
            "AND length(provider_egress_policy_version) BETWEEN 1 AND 64",
            name="ck_case_budget_response_observations_egress_policy",
        ),
        sa.CheckConstraint(
            "provider_base_url IS NULL OR ("
            "provider_base_url = btrim(provider_base_url) AND "
            "length(provider_base_url) BETWEEN 1 AND 2048)",
            name="ck_case_budget_response_observations_base_url",
        ),
        sa.CheckConstraint(
            "(input_tokens IS NULL OR input_tokens >= 0) AND "
            "(output_tokens IS NULL OR output_tokens >= 0) AND "
            "(total_tokens IS NULL OR total_tokens >= 0) AND "
            "(cached_tokens IS NULL OR cached_tokens >= 0)",
            name="ck_case_budget_response_observations_usage",
        ),
        sa.CheckConstraint(
            "cached_tokens IS NULL OR input_tokens IS NULL OR cached_tokens <= input_tokens",
            name="ck_case_budget_response_observations_cached_bound",
        ),
        sa.CheckConstraint(
            "total_tokens IS NULL OR input_tokens IS NULL OR output_tokens IS NULL OR "
            "total_tokens = input_tokens + output_tokens",
            name="ck_case_budget_response_observations_total",
        ),
        sa.CheckConstraint(
            "(cost_amount IS NULL AND cost_currency IS NULL) OR "
            "(cost_amount IS NOT NULL AND cost_amount >= 0 AND "
            "cost_amount NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric) "
            "AND cost_currency ~ '^[A-Z]{3}$')",
            name="ck_case_budget_response_observations_cost",
        ),
    )

    op.execute(
        """
        CREATE FUNCTION case_budget_guard_account_identity() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
          expected_reserved numeric;
          expected_settled numeric;
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'Case budget accounts are retained';
          END IF;
          IF TG_OP = 'INSERT' THEN
            IF NEW.reserved_amount <> 0 OR NEW.settled_amount <> 0 THEN
              RAISE EXCEPTION 'Case budget accounts must start with zero totals';
            END IF;
            RETURN NEW;
          END IF;
          IF (
            NEW.workspace_id, NEW.case_id, NEW.experiment_variant_id,
            NEW.currency, NEW.limit_amount, NEW.pricing_snapshot_id,
            NEW.pricing_snapshot_hash, NEW.provider, NEW.model,
            NEW.provider_egress_policy_version, NEW.provider_base_url,
            NEW.budget_policy_version, NEW.created_at
          ) IS DISTINCT FROM (
            OLD.workspace_id, OLD.case_id, OLD.experiment_variant_id,
            OLD.currency, OLD.limit_amount, OLD.pricing_snapshot_id,
            OLD.pricing_snapshot_hash, OLD.provider, OLD.model,
            OLD.provider_egress_policy_version, OLD.provider_base_url,
            OLD.budget_policy_version, OLD.created_at
          ) THEN
            RAISE EXCEPTION 'Case budget account identity is immutable';
          END IF;
          IF NEW.reserved_amount > OLD.reserved_amount
             AND NEW.reserved_amount + NEW.settled_amount > NEW.limit_amount
          THEN
            RAISE EXCEPTION 'Case budget cannot add an over-limit reservation';
          END IF;
          IF (NEW.reserved_amount, NEW.settled_amount)
             IS DISTINCT FROM (OLD.reserved_amount, OLD.settled_amount)
          THEN
            SELECT
              COALESCE(SUM(reserved_amount) FILTER (
                WHERE status IN ('RESERVED', 'UNKNOWN_OUTCOME')
              ), 0),
              COALESCE(SUM(settlement_amount) FILTER (
                WHERE status = 'SETTLED'
              ), 0)
            INTO expected_reserved, expected_settled
            FROM run_budget_attempts
            WHERE workspace_id = NEW.workspace_id
              AND case_id = NEW.case_id;
            IF (NEW.reserved_amount, NEW.settled_amount)
               IS DISTINCT FROM (expected_reserved, expected_settled)
            THEN
              RAISE EXCEPTION 'Case budget totals do not match attempts';
            END IF;
          END IF;
          RETURN NEW;
        END
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER case_budget_account_identity_guard
        BEFORE INSERT OR UPDATE OR DELETE ON case_budget_accounts
        FOR EACH ROW EXECUTE FUNCTION case_budget_guard_account_identity();
        """
    )
    op.execute(
        """
        CREATE FUNCTION case_budget_guard_run_case_binding() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
          parent_currency text;
        BEGIN
          IF TG_OP = 'UPDATE' AND NEW.case_id IS DISTINCT FROM OLD.case_id THEN
            RAISE EXCEPTION 'Run budget case binding is immutable';
          END IF;
          IF NEW.case_id IS NULL THEN
            RETURN NEW;
          END IF;
          SELECT currency INTO parent_currency
          FROM case_budget_accounts
          WHERE workspace_id = NEW.workspace_id AND case_id = NEW.case_id;
          IF parent_currency IS DISTINCT FROM NEW.currency THEN
            RAISE EXCEPTION 'Run and case budget currencies must match';
          END IF;
          RETURN NEW;
        END
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER run_budget_case_binding_guard
        BEFORE INSERT OR UPDATE ON run_budget_accounts
        FOR EACH ROW EXECUTE FUNCTION case_budget_guard_run_case_binding();
        """
    )
    op.execute(
        """
        CREATE FUNCTION case_budget_guard_attempt_binding() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
          run_case_id uuid;
        BEGIN
          SELECT case_id INTO run_case_id
          FROM run_budget_accounts
          WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id;
          IF NEW.case_id IS DISTINCT FROM run_case_id THEN
            RAISE EXCEPTION 'Attempt case must match its Run account';
          END IF;
          IF TG_OP = 'UPDATE' AND (
            NEW.case_id, NEW.case_attempt_identity, NEW.basis_identity
          ) IS DISTINCT FROM (
            OLD.case_id, OLD.case_attempt_identity, OLD.basis_identity
          ) THEN
            RAISE EXCEPTION 'Attempt case evidence is immutable';
          END IF;
          RETURN NEW;
        END
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER run_budget_case_attempt_guard
        BEFORE INSERT OR UPDATE ON run_budget_attempts
        FOR EACH ROW EXECUTE FUNCTION case_budget_guard_attempt_binding();
        """
    )
    op.execute(
        """
        CREATE FUNCTION case_budget_verify_account_totals() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
          account_reserved numeric;
          account_settled numeric;
          expected_reserved numeric;
          expected_settled numeric;
        BEGIN
          IF NEW.case_id IS NULL THEN
            RETURN NEW;
          END IF;
          SELECT reserved_amount, settled_amount
          INTO account_reserved, account_settled
          FROM case_budget_accounts
          WHERE workspace_id = NEW.workspace_id AND case_id = NEW.case_id;
          SELECT
            COALESCE(SUM(reserved_amount) FILTER (
              WHERE status IN ('RESERVED', 'UNKNOWN_OUTCOME')
            ), 0),
            COALESCE(SUM(settlement_amount) FILTER (
              WHERE status = 'SETTLED'
            ), 0)
          INTO expected_reserved, expected_settled
          FROM run_budget_attempts
          WHERE workspace_id = NEW.workspace_id AND case_id = NEW.case_id;
          IF (account_reserved, account_settled)
             IS DISTINCT FROM (expected_reserved, expected_settled)
          THEN
            RAISE EXCEPTION 'Case budget totals do not match attempts';
          END IF;
          RETURN NEW;
        END
        $$;
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER case_budget_attempt_totals_guard
        AFTER INSERT OR UPDATE ON run_budget_attempts
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION case_budget_verify_account_totals();
        """
    )
    op.execute(
        """
        CREATE FUNCTION case_budget_guard_run_binding_evidence() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
          bound_run_id uuid;
          case_evaluation_run_id uuid;
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'Case Run bindings are retained';
          END IF;
          IF TG_OP = 'UPDATE' THEN
            RAISE EXCEPTION 'Case Run bindings are immutable';
          END IF;
          SELECT agent_run_id INTO bound_run_id
          FROM evaluation_interactive_episode_turns
          WHERE workspace_id = NEW.workspace_id
            AND case_id = NEW.case_id
            AND turn_index = NEW.turn_index;
          SELECT experiment_run_id INTO case_evaluation_run_id
          FROM evaluation_experiment_case_results
          WHERE workspace_id = NEW.workspace_id AND id = NEW.case_id;
          IF bound_run_id IS DISTINCT FROM NEW.agent_run_id
             OR case_evaluation_run_id IS DISTINCT FROM NEW.evaluation_run_id
          THEN
            RAISE EXCEPTION 'Case Run binding does not match its turn';
          END IF;
          RETURN NEW;
        END
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER case_budget_run_binding_evidence_guard
        BEFORE INSERT OR UPDATE OR DELETE ON case_budget_run_bindings
        FOR EACH ROW EXECUTE FUNCTION case_budget_guard_run_binding_evidence();
        """
    )
    op.execute(
        """
        CREATE FUNCTION case_budget_guard_response_observation() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
          attempt_case_id uuid;
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'Case response observations are retained';
          END IF;
          IF TG_OP = 'UPDATE' THEN
            RAISE EXCEPTION 'Case response observations are immutable';
          END IF;
          SELECT case_id INTO attempt_case_id
          FROM run_budget_attempts
          WHERE workspace_id = NEW.workspace_id
            AND run_id = NEW.run_id
            AND attempt_identity = NEW.attempt_identity;
          IF attempt_case_id IS DISTINCT FROM NEW.case_id THEN
            RAISE EXCEPTION 'Response observation case does not match its attempt';
          END IF;
          RETURN NEW;
        END
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER case_budget_response_observation_guard
        BEFORE INSERT OR UPDATE OR DELETE ON case_budget_response_observations
        FOR EACH ROW EXECUTE FUNCTION case_budget_guard_response_observation();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM case_budget_accounts)
             OR EXISTS (SELECT 1 FROM case_budget_run_bindings)
             OR EXISTS (SELECT 1 FROM case_budget_response_observations)
             OR EXISTS (SELECT 1 FROM run_budget_attempts WHERE case_id IS NOT NULL)
             OR EXISTS (SELECT 1 FROM run_budget_accounts WHERE case_id IS NOT NULL)
          THEN
            RAISE EXCEPTION 'Cannot remove retained Interactive case budget evidence';
          END IF;
        END
        $$;
        """
    )
    op.execute(
        "DROP TRIGGER case_budget_response_observation_guard "
        "ON case_budget_response_observations"
    )
    op.execute("DROP FUNCTION case_budget_guard_response_observation()")
    op.execute(
        "DROP TRIGGER case_budget_run_binding_evidence_guard ON case_budget_run_bindings"
    )
    op.execute("DROP FUNCTION case_budget_guard_run_binding_evidence()")
    op.execute("DROP TRIGGER case_budget_attempt_totals_guard ON run_budget_attempts")
    op.execute("DROP FUNCTION case_budget_verify_account_totals()")
    op.execute("DROP TRIGGER run_budget_case_attempt_guard ON run_budget_attempts")
    op.execute("DROP FUNCTION case_budget_guard_attempt_binding()")
    op.execute("DROP TRIGGER run_budget_case_binding_guard ON run_budget_accounts")
    op.execute("DROP FUNCTION case_budget_guard_run_case_binding()")
    op.execute("DROP TRIGGER case_budget_account_identity_guard ON case_budget_accounts")
    op.execute("DROP FUNCTION case_budget_guard_account_identity()")

    op.drop_table("case_budget_response_observations")
    op.drop_constraint(
        "fk_run_budget_attempts_case_workspace",
        "run_budget_attempts",
        type_="foreignkey",
    )
    op.drop_constraint(
        "uq_run_budget_attempts_case_identity", "run_budget_attempts", type_="unique"
    )
    op.drop_constraint(
        "ck_run_budget_attempts_case_fields_shape", "run_budget_attempts", type_="check"
    )
    op.drop_constraint(
        "ck_run_budget_attempts_case_identity", "run_budget_attempts", type_="check"
    )
    op.drop_constraint(
        "ck_run_budget_attempts_basis_identity", "run_budget_attempts", type_="check"
    )
    op.drop_column("run_budget_attempts", "basis_identity")
    op.drop_column("run_budget_attempts", "case_attempt_identity")
    op.drop_column("run_budget_attempts", "case_id")
    op.drop_constraint(
        "fk_run_budget_accounts_case_workspace",
        "run_budget_accounts",
        type_="foreignkey",
    )
    op.drop_column("run_budget_accounts", "case_id")
    op.drop_index(
        "ix_case_budget_run_bindings_evaluation_run",
        table_name="case_budget_run_bindings",
    )
    op.drop_table("case_budget_run_bindings")
    op.drop_index(
        "ix_case_budget_accounts_workspace_variant", table_name="case_budget_accounts"
    )
    op.drop_table("case_budget_accounts")
