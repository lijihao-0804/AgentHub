"""Retain fixed Run accounts and conservative provider-attempt reservations.

The reservation-basis check admits only ``CONSERVATIVE_MAXIMUM``.  That label
is an assertion supplied by trusted application code; this migration cannot
prove that pricing or request inputs are complete upper bounds.

Revision ID: 0045_run_budget_ledger
Revises: 0044_summary_evidence_guard
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0045_run_budget_ledger"
down_revision = "0044_summary_evidence_guard"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True)
    now = sa.func.now()

    op.create_table(
        "run_budget_accounts",
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("run_id", uuid_type, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("limit_amount", sa.Numeric(20, 8), nullable=False),
        sa.Column(
            "reserved_amount",
            sa.Numeric(20, 8),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "settled_amount",
            sa.Numeric(20, 8),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["agent_runs.workspace_id", "agent_runs.id"],
            name="fk_run_budget_accounts_run_workspace",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "workspace_id",
            "run_id",
            name="pk_run_budget_accounts",
        ),
        sa.CheckConstraint(
            "currency ~ '^[A-Z]{3}$'",
            name="ck_run_budget_accounts_currency",
        ),
        sa.CheckConstraint(
            "limit_amount > 0 AND "
            "limit_amount NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)",
            name="ck_run_budget_accounts_limit_positive_finite",
        ),
        sa.CheckConstraint(
            "reserved_amount >= 0 AND reserved_amount <= limit_amount",
            name="ck_run_budget_accounts_reserved_bounds",
        ),
        sa.CheckConstraint(
            "settled_amount >= 0 AND "
            "settled_amount NOT IN "
            "('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)",
            name="ck_run_budget_accounts_settled_bounds",
        ),
    )
    op.create_table(
        "run_budget_attempts",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("run_id", uuid_type, nullable=False),
        sa.Column("attempt_identity", sa.String(128), nullable=False),
        sa.Column("attempt_kind", sa.String(32), nullable=False),
        sa.Column("reserved_amount", sa.Numeric(20, 8), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("reservation_basis", sa.String(32), nullable=False),
        sa.Column(
            "status",
            sa.String(24),
            nullable=False,
            server_default="RESERVED",
        ),
        sa.Column("settlement_amount", sa.Numeric(20, 8)),
        sa.Column("settlement_currency", sa.String(3)),
        sa.Column("settlement_evidence_kind", sa.String(32)),
        sa.Column("settlement_evidence_identity", sa.String(256)),
        sa.Column("settlement_is_estimate", sa.Boolean()),
        sa.Column("settled_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["run_budget_accounts.workspace_id", "run_budget_accounts.run_id"],
            name="fk_run_budget_attempts_account_scope",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["agent_runs.workspace_id", "agent_runs.id"],
            name="fk_run_budget_attempts_run_workspace",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_run_budget_attempts"),
        sa.UniqueConstraint(
            "workspace_id",
            "run_id",
            "attempt_identity",
            name="uq_run_budget_attempts_identity",
        ),
        sa.CheckConstraint(
            "attempt_identity = btrim(attempt_identity) AND "
            "length(attempt_identity) BETWEEN 1 AND 128",
            name="ck_run_budget_attempts_identity",
        ),
        sa.CheckConstraint(
            "attempt_kind IN ('MAIN_MODEL', 'RETRY', 'FALLBACK', 'SUMMARY', 'AUXILIARY')",
            name="ck_run_budget_attempts_kind",
        ),
        sa.CheckConstraint(
            "reserved_amount > 0 AND "
            "reserved_amount NOT IN "
            "('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)",
            name="ck_run_budget_attempts_reserved_positive_finite",
        ),
        sa.CheckConstraint(
            "currency ~ '^[A-Z]{3}$'",
            name="ck_run_budget_attempts_currency",
        ),
        sa.CheckConstraint(
            "reservation_basis = 'CONSERVATIVE_MAXIMUM'",
            name="ck_run_budget_attempts_reservation_basis",
        ),
        sa.CheckConstraint(
            "status IN ('RESERVED', 'UNKNOWN_OUTCOME', 'SETTLED')",
            name="ck_run_budget_attempts_status",
        ),
        sa.CheckConstraint(
            "settlement_currency IS NULL OR settlement_currency ~ '^[A-Z]{3}$'",
            name="ck_run_budget_attempts_settlement_currency",
        ),
        sa.CheckConstraint(
            "settlement_currency IS NULL OR settlement_currency = currency",
            name="ck_run_budget_attempts_settlement_currency_matches",
        ),
        sa.CheckConstraint(
            "settlement_evidence_kind IS NULL OR settlement_evidence_kind IN "
            "('RESPONSE_ESTIMATE', 'CONSERVATIVE_CHARGE', 'PROVIDER_BILL')",
            name="ck_run_budget_attempts_settlement_evidence_kind",
        ),
        sa.CheckConstraint(
            "settlement_amount IS NULL OR "
            "(settlement_amount >= 0 AND settlement_amount NOT IN "
            "('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric))",
            name="ck_run_budget_attempts_settlement_amount",
        ),
        sa.CheckConstraint(
            "settlement_evidence_identity IS NULL OR "
            "(settlement_evidence_identity = btrim(settlement_evidence_identity) "
            "AND length(settlement_evidence_identity) BETWEEN 1 AND 256)",
            name="ck_run_budget_attempts_settlement_identity",
        ),
        sa.CheckConstraint(
            "("
            "status = 'SETTLED' AND settlement_amount IS NOT NULL "
            "AND settlement_currency IS NOT NULL "
            "AND settlement_evidence_kind IS NOT NULL "
            "AND settlement_evidence_identity IS NOT NULL "
            "AND settlement_is_estimate IS NOT NULL "
            "AND settled_at IS NOT NULL"
            ") OR ("
            "status IN ('RESERVED', 'UNKNOWN_OUTCOME') "
            "AND settlement_amount IS NULL "
            "AND settlement_currency IS NULL "
            "AND settlement_evidence_kind IS NULL "
            "AND settlement_evidence_identity IS NULL "
            "AND settlement_is_estimate IS NULL "
            "AND settled_at IS NULL"
            ")",
            name="ck_run_budget_attempts_settlement_shape",
        ),
        sa.CheckConstraint(
            "settlement_evidence_kind IS NULL OR "
            "settlement_is_estimate = (settlement_evidence_kind <> 'PROVIDER_BILL')",
            name="ck_run_budget_attempts_settlement_estimate_flag",
        ),
        sa.CheckConstraint(
            "status <> 'SETTLED' OR settlement_evidence_kind = 'PROVIDER_BILL'",
            name="ck_run_budget_attempts_final_settlement_provider_bill",
        ),
    )
    op.create_index(
        "ix_run_budget_attempts_workspace_run_status",
        "run_budget_attempts",
        ["workspace_id", "run_id", "status", "created_at"],
    )

    op.execute(
        """
        CREATE FUNCTION run_budget_guard_account_identity() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
          expected_reserved numeric;
          expected_settled numeric;
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'Run budget accounts are retained';
          END IF;
          IF TG_OP = 'INSERT' THEN
            IF NEW.reserved_amount <> 0 OR NEW.settled_amount <> 0 THEN
              RAISE EXCEPTION 'Run budget accounts must start with zero totals';
            END IF;
            RETURN NEW;
          END IF;
          IF (
            NEW.workspace_id, NEW.run_id, NEW.currency,
            NEW.limit_amount, NEW.created_at
          ) IS DISTINCT FROM (
            OLD.workspace_id, OLD.run_id, OLD.currency,
            OLD.limit_amount, OLD.created_at
          ) THEN
            RAISE EXCEPTION 'Run budget account identity is immutable';
          END IF;
          -- Settlement must be allowed to record an actual overrun, but an
          -- already over-limit account must not accept a new reservation,
          -- even if a writer bypasses the application service.
          IF NEW.reserved_amount > OLD.reserved_amount
             AND NEW.reserved_amount + NEW.settled_amount > NEW.limit_amount
          THEN
            RAISE EXCEPTION 'Run budget cannot add an over-limit reservation';
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
              AND run_id = NEW.run_id;
            IF (NEW.reserved_amount, NEW.settled_amount)
               IS DISTINCT FROM (expected_reserved, expected_settled)
            THEN
              RAISE EXCEPTION
                'Run budget account totals do not match retained attempts';
            END IF;
          END IF;
          RETURN NEW;
        END
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER run_budget_account_identity_guard
        BEFORE INSERT OR UPDATE OR DELETE ON run_budget_accounts
        FOR EACH ROW EXECUTE FUNCTION run_budget_guard_account_identity();
        """
    )
    op.execute(
        """
        CREATE FUNCTION run_budget_guard_attempt_evidence() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
          account_currency text;
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'Run budget attempts are retained';
          END IF;
          SELECT currency
          INTO account_currency
          FROM run_budget_accounts
          WHERE workspace_id = NEW.workspace_id
            AND run_id = NEW.run_id;
          IF account_currency IS DISTINCT FROM NEW.currency THEN
            RAISE EXCEPTION
              'Run budget attempt currency must match its account currency';
          END IF;
          IF TG_OP = 'INSERT' THEN
            IF NEW.status <> 'RESERVED'
               OR NEW.settlement_amount IS NOT NULL
               OR NEW.settlement_currency IS NOT NULL
               OR NEW.settlement_evidence_kind IS NOT NULL
               OR NEW.settlement_evidence_identity IS NOT NULL
               OR NEW.settlement_is_estimate IS NOT NULL
               OR NEW.settled_at IS NOT NULL
            THEN
              RAISE EXCEPTION 'Run budget attempts must start reserved';
            END IF;
            RETURN NEW;
          END IF;
          IF (
            NEW.id, NEW.workspace_id, NEW.run_id, NEW.attempt_identity,
            NEW.attempt_kind, NEW.reserved_amount, NEW.currency,
            NEW.reservation_basis, NEW.created_at
          ) IS DISTINCT FROM (
            OLD.id, OLD.workspace_id, OLD.run_id, OLD.attempt_identity,
            OLD.attempt_kind, OLD.reserved_amount, OLD.currency,
            OLD.reservation_basis, OLD.created_at
          ) THEN
            RAISE EXCEPTION 'Run budget attempt identity is immutable';
          END IF;
          IF NEW.settlement_currency IS NOT NULL
             AND NEW.settlement_currency IS DISTINCT FROM NEW.currency
          THEN
            RAISE EXCEPTION
              'Run budget settlement currency must match the attempt currency';
          END IF;
          IF OLD.status = 'SETTLED' AND NEW.status <> 'SETTLED' THEN
            RAISE EXCEPTION 'Settled Run budget attempts are terminal';
          END IF;
          IF NOT (
            OLD.status = NEW.status OR
            (OLD.status = 'RESERVED' AND NEW.status IN ('UNKNOWN_OUTCOME', 'SETTLED')) OR
            (OLD.status = 'UNKNOWN_OUTCOME' AND NEW.status = 'SETTLED')
          ) THEN
            RAISE EXCEPTION 'Run budget attempt transition is invalid';
          END IF;
          IF OLD.settlement_amount IS NOT NULL AND (
            NEW.settlement_amount, NEW.settlement_currency,
            NEW.settlement_evidence_kind, NEW.settlement_evidence_identity,
            NEW.settlement_is_estimate, NEW.settled_at
          ) IS DISTINCT FROM (
            OLD.settlement_amount, OLD.settlement_currency,
            OLD.settlement_evidence_kind, OLD.settlement_evidence_identity,
            OLD.settlement_is_estimate, OLD.settled_at
          ) THEN
            RAISE EXCEPTION 'Run budget settlement evidence is immutable';
          END IF;
          RETURN NEW;
        END
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER run_budget_attempt_evidence_guard
        BEFORE INSERT OR UPDATE OR DELETE ON run_budget_attempts
        FOR EACH ROW EXECUTE FUNCTION run_budget_guard_attempt_evidence();
        """
    )
    op.execute(
        """
        CREATE FUNCTION run_budget_verify_account_totals() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
          account_reserved numeric;
          account_settled numeric;
          expected_reserved numeric;
          expected_settled numeric;
        BEGIN
          SELECT reserved_amount, settled_amount
          INTO account_reserved, account_settled
          FROM run_budget_accounts
          WHERE workspace_id = NEW.workspace_id
            AND run_id = NEW.run_id;

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
            AND run_id = NEW.run_id;

          IF (account_reserved, account_settled)
             IS DISTINCT FROM (expected_reserved, expected_settled)
          THEN
            RAISE EXCEPTION
              'Run budget account totals do not match retained attempts';
          END IF;
          RETURN NEW;
        END
        $$;
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER run_budget_attempt_totals_guard
        AFTER INSERT OR UPDATE ON run_budget_attempts
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION run_budget_verify_account_totals();
        """
    )


def downgrade() -> None:
    # A rollback must not erase the only accounting evidence for reserved or
    # settled provider attempts. Empty installations may still roll back.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM run_budget_accounts)
             OR EXISTS (SELECT 1 FROM run_budget_attempts)
          THEN
            RAISE EXCEPTION 'Cannot remove retained Run budget ledger evidence';
          END IF;
        END
        $$;
        """
    )
    op.execute("DROP TRIGGER run_budget_attempt_evidence_guard ON run_budget_attempts")
    op.execute("DROP TRIGGER run_budget_attempt_totals_guard ON run_budget_attempts")
    op.execute("DROP FUNCTION run_budget_verify_account_totals()")
    op.execute("DROP FUNCTION run_budget_guard_attempt_evidence()")
    op.execute("DROP TRIGGER run_budget_account_identity_guard ON run_budget_accounts")
    op.execute("DROP FUNCTION run_budget_guard_account_identity()")
    op.drop_index(
        "ix_run_budget_attempts_workspace_run_status",
        table_name="run_budget_attempts",
    )
    op.drop_table("run_budget_attempts")
    op.drop_table("run_budget_accounts")
