"""Durable evidence for cross-Run Interactive evaluation budgets."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy import Uuid as SQLUuid
from sqlalchemy.orm import Mapped, mapped_column

from packages.core.database import Base

CASE_BUDGET_POLICY_VERSION = "interactive_case_budget_v1"
MAX_PROVIDER_BASE_URL_LENGTH = 2_048


class CaseBudgetAccount(Base):
    """One immutable CNY budget envelope for an Interactive episode case."""

    __tablename__ = "case_budget_accounts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "case_id"],
            [
                "evaluation_interactive_episodes.workspace_id",
                "evaluation_interactive_episodes.case_id",
            ],
            name="fk_case_budget_accounts_episode_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "experiment_variant_id"],
            [
                "evaluation_experiment_variants.workspace_id",
                "evaluation_experiment_variants.id",
            ],
            name="fk_case_budget_accounts_variant_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "pricing_snapshot_id"],
            ["pricing_snapshots.workspace_id", "pricing_snapshots.id"],
            name="fk_case_budget_accounts_pricing_workspace",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "currency = 'CNY'",
            name="ck_case_budget_accounts_cny",
        ),
        CheckConstraint(
            "limit_amount > 0 AND "
            "limit_amount NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)",
            name="ck_case_budget_accounts_limit_positive_finite",
        ),
        CheckConstraint(
            "reserved_amount >= 0 AND reserved_amount <= limit_amount",
            name="ck_case_budget_accounts_reserved_bounds",
        ),
        CheckConstraint(
            "settled_amount >= 0 AND "
            "settled_amount NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric)",
            name="ck_case_budget_accounts_settled_bounds",
        ),
        CheckConstraint(
            "pricing_snapshot_hash ~ '^[0-9a-f]{64}$'",
            name="ck_case_budget_accounts_pricing_hash",
        ),
        CheckConstraint(
            "provider = btrim(provider) AND length(provider) BETWEEN 1 AND 100 AND "
            "model = btrim(model) AND length(model) BETWEEN 1 AND 200",
            name="ck_case_budget_accounts_model_identity",
        ),
        CheckConstraint(
            "provider_egress_policy_version = btrim(provider_egress_policy_version) "
            "AND length(provider_egress_policy_version) BETWEEN 1 AND 64",
            name="ck_case_budget_accounts_egress_policy",
        ),
        CheckConstraint(
            "provider_base_url IS NULL OR ("
            "provider_base_url = btrim(provider_base_url) AND "
            "length(provider_base_url) BETWEEN 1 AND 2048)",
            name="ck_case_budget_accounts_provider_base_url",
        ),
        CheckConstraint(
            f"budget_policy_version = '{CASE_BUDGET_POLICY_VERSION}'",
            name="ck_case_budget_accounts_policy_version",
        ),
        PrimaryKeyConstraint(
            "workspace_id",
            "case_id",
            name="pk_case_budget_accounts",
        ),
        Index(
            "ix_case_budget_accounts_workspace_variant",
            "workspace_id",
            "experiment_variant_id",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    case_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    experiment_variant_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    limit_amount: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    reserved_amount: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), nullable=False, default=Decimal("0"), server_default=text("0")
    )
    settled_amount: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), nullable=False, default=Decimal("0"), server_default=text("0")
    )
    pricing_snapshot_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    pricing_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    provider_egress_policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_base_url: Mapped[str | None] = mapped_column(Text)
    budget_policy_version: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=CASE_BUDGET_POLICY_VERSION
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class CaseBudgetRunBinding(Base):
    """Immutable mapping from one interactive turn/Run to its case account."""

    __tablename__ = "case_budget_run_bindings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "case_id"],
            ["case_budget_accounts.workspace_id", "case_budget_accounts.case_id"],
            name="fk_case_budget_run_bindings_account_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "case_id", "turn_index"],
            [
                "evaluation_interactive_episode_turns.workspace_id",
                "evaluation_interactive_episode_turns.case_id",
                "evaluation_interactive_episode_turns.turn_index",
            ],
            name="fk_case_budget_run_bindings_turn_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "evaluation_run_id"],
            ["evaluation_experiment_runs.workspace_id", "evaluation_experiment_runs.id"],
            name="fk_case_budget_run_bindings_evaluation_run_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "agent_run_id"],
            ["agent_runs.workspace_id", "agent_runs.id"],
            name="fk_case_budget_run_bindings_agent_run_workspace",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id",
            "agent_run_id",
            name="uq_case_budget_run_bindings_agent_run",
        ),
        CheckConstraint("turn_index >= 0", name="ck_case_budget_run_bindings_turn_index"),
        CheckConstraint(
            "dispatch_owner = btrim(dispatch_owner) AND "
            "length(dispatch_owner) BETWEEN 1 AND 128",
            name="ck_case_budget_run_bindings_owner",
        ),
        CheckConstraint(
            "dispatch_generation >= 0",
            name="ck_case_budget_run_bindings_generation",
        ),
        PrimaryKeyConstraint(
            "workspace_id",
            "case_id",
            "turn_index",
            name="pk_case_budget_run_bindings",
        ),
        Index(
            "ix_case_budget_run_bindings_evaluation_run",
            "workspace_id",
            "evaluation_run_id",
        ),
    )

    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    case_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    turn_index: Mapped[int] = mapped_column(Integer, nullable=False)
    evaluation_run_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    agent_run_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    dispatch_owner: Mapped[str] = mapped_column(String(128), nullable=False)
    dispatch_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CaseBudgetResponseObservation(Base):
    """Append-only response evidence that never releases a reservation."""

    __tablename__ = "case_budget_response_observations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "case_id"],
            ["case_budget_accounts.workspace_id", "case_budget_accounts.case_id"],
            name="fk_case_budget_response_observations_case_workspace",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "run_id", "attempt_identity"],
            [
                "run_budget_attempts.workspace_id",
                "run_budget_attempts.run_id",
                "run_budget_attempts.attempt_identity",
            ],
            name="fk_case_budget_response_observations_attempt_workspace",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "workspace_id",
            "run_id",
            "attempt_identity",
            name="uq_case_budget_response_observations_attempt",
        ),
        CheckConstraint(
            "observation_identity = btrim(observation_identity) AND "
            "length(observation_identity) BETWEEN 1 AND 256",
            name="ck_case_budget_response_observations_identity",
        ),
        CheckConstraint(
            "provider = btrim(provider) AND length(provider) BETWEEN 1 AND 100 AND "
            "model = btrim(model) AND length(model) BETWEEN 1 AND 200",
            name="ck_case_budget_response_observations_model_identity",
        ),
        CheckConstraint(
            "provider_egress_policy_version = btrim(provider_egress_policy_version) "
            "AND length(provider_egress_policy_version) BETWEEN 1 AND 64",
            name="ck_case_budget_response_observations_egress_policy",
        ),
        CheckConstraint(
            "provider_base_url IS NULL OR ("
            "provider_base_url = btrim(provider_base_url) AND "
            "length(provider_base_url) BETWEEN 1 AND 2048)",
            name="ck_case_budget_response_observations_base_url",
        ),
        CheckConstraint(
            "(input_tokens IS NULL OR input_tokens >= 0) AND "
            "(output_tokens IS NULL OR output_tokens >= 0) AND "
            "(total_tokens IS NULL OR total_tokens >= 0) AND "
            "(cached_tokens IS NULL OR cached_tokens >= 0)",
            name="ck_case_budget_response_observations_usage",
        ),
        CheckConstraint(
            "cached_tokens IS NULL OR input_tokens IS NULL OR cached_tokens <= input_tokens",
            name="ck_case_budget_response_observations_cached_bound",
        ),
        CheckConstraint(
            "total_tokens IS NULL OR input_tokens IS NULL OR output_tokens IS NULL OR "
            "total_tokens = input_tokens + output_tokens",
            name="ck_case_budget_response_observations_total",
        ),
        CheckConstraint(
            "(cost_amount IS NULL AND cost_currency IS NULL) OR "
            "(cost_amount IS NOT NULL AND cost_amount >= 0 AND "
            "cost_amount NOT IN ('NaN'::numeric, 'Infinity'::numeric, '-Infinity'::numeric) "
            "AND cost_currency ~ '^[A-Z]{3}$')",
            name="ck_case_budget_response_observations_cost",
        ),
        PrimaryKeyConstraint("id", name="pk_case_budget_response_observations"),
    )

    id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    case_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(SQLUuid(as_uuid=True), nullable=False)
    attempt_identity: Mapped[str] = mapped_column(String(128), nullable=False)
    observation_identity: Mapped[str] = mapped_column(String(256), nullable=False)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    provider_egress_policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_base_url: Mapped[str | None] = mapped_column(Text)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    cached_tokens: Mapped[int | None] = mapped_column(Integer)
    # Response estimates are side evidence. Preserve their original precision;
    # the CNY account and reservation amounts remain NUMERIC(20, 8).
    cost_amount: Mapped[Decimal | None] = mapped_column(Numeric())
    cost_currency: Mapped[str | None] = mapped_column(String(3))
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = [
    "CASE_BUDGET_POLICY_VERSION",
    "CaseBudgetAccount",
    "CaseBudgetResponseObservation",
    "CaseBudgetRunBinding",
    "MAX_PROVIDER_BASE_URL_LENGTH",
]
