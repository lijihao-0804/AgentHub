"""Persist M7-E ablation analysis and M7-F release gate artifacts."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021_m7ef_ablation_release_gate"
down_revision: str | None = "0020_m7d_review_closure"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True)
    json_type = postgresql.JSONB()
    now = sa.func.now()

    op.create_unique_constraint(
        "uq_evaluation_comparisons_workspace_id",
        "evaluation_experiment_comparisons",
        ["workspace_id", "id"],
    )

    op.create_table(
        "evaluation_ablation_results",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("comparison_id", uuid_type, nullable=False),
        sa.Column("experiment_run_id", uuid_type, nullable=False),
        sa.Column("baseline_variant_id", uuid_type, nullable=False),
        sa.Column("candidate_variant_id", uuid_type, nullable=False),
        sa.Column("factor", sa.String(length=32), nullable=False),
        sa.Column(
            "changed_paths", json_type, nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("baseline_factor_hash", sa.String(length=64), nullable=False),
        sa.Column("candidate_factor_hash", sa.String(length=64), nullable=False),
        sa.Column("analysis_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by", uuid_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(
            ["workspace_id", "comparison_id"],
            [
                "evaluation_experiment_comparisons.workspace_id",
                "evaluation_experiment_comparisons.id",
            ],
            name="fk_evaluation_ablation_comparison_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "experiment_run_id"],
            ["evaluation_experiment_runs.workspace_id", "evaluation_experiment_runs.id"],
            name="fk_evaluation_ablation_run_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "baseline_variant_id"],
            ["evaluation_experiment_variants.workspace_id", "evaluation_experiment_variants.id"],
            name="fk_evaluation_ablation_baseline_workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "candidate_variant_id"],
            ["evaluation_experiment_variants.workspace_id", "evaluation_experiment_variants.id"],
            name="fk_evaluation_ablation_candidate_workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_evaluation_ablation_created_by",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "factor IN ('RETRIEVAL', 'PROMPT', 'MODEL', 'MULTI_FACTOR_CHANGE', 'NO_CHANGE')",
            name="ck_evaluation_ablation_factor",
        ),
        sa.UniqueConstraint(
            "workspace_id", "comparison_id", name="uq_evaluation_ablation_comparison"
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_evaluation_ablation_workspace_id"),
    )
    op.create_index(
        "ix_evaluation_ablation_run",
        "evaluation_ablation_results",
        ["workspace_id", "experiment_run_id"],
    )

    op.create_table(
        "evaluation_release_gate_policies",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("policy_json", json_type, nullable=False),
        sa.Column("policy_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by", uuid_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_evaluation_gate_policies_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_evaluation_gate_policies_created_by",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_evaluation_gate_policies_workspace_id"),
        sa.UniqueConstraint("workspace_id", "name", name="uq_evaluation_gate_policies_name"),
    )
    op.create_index(
        "ix_evaluation_gate_policies_workspace_created",
        "evaluation_release_gate_policies",
        ["workspace_id", "created_at"],
    )

    op.create_table(
        "evaluation_release_gate_decisions",
        sa.Column("id", uuid_type, primary_key=True),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("comparison_id", uuid_type, nullable=False),
        sa.Column("policy_id", uuid_type, nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("rule_results", json_type, nullable=False),
        sa.Column("reasons", json_type, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("comparison_hash", sa.String(length=64), nullable=False),
        sa.Column("policy_hash", sa.String(length=64), nullable=False),
        sa.Column("decision_hash", sa.String(length=64), nullable=False),
        sa.Column("created_by", uuid_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=now),
        sa.ForeignKeyConstraint(
            ["workspace_id", "comparison_id"],
            [
                "evaluation_experiment_comparisons.workspace_id",
                "evaluation_experiment_comparisons.id",
            ],
            name="fk_evaluation_gate_decisions_comparison_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "policy_id"],
            [
                "evaluation_release_gate_policies.workspace_id",
                "evaluation_release_gate_policies.id",
            ],
            name="fk_evaluation_gate_decisions_policy_workspace",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_evaluation_gate_decisions_created_by",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('PASS', 'FAIL', 'INCONCLUSIVE')", name="ck_evaluation_gate_decisions_status"
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "comparison_id",
            "policy_id",
            name="uq_evaluation_gate_decisions_identity",
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_evaluation_gate_decisions_workspace_id"),
        sa.Index("ix_evaluation_gate_decisions_comparison", "workspace_id", "comparison_id"),
    )


def downgrade() -> None:
    op.drop_table("evaluation_release_gate_decisions")
    op.drop_index(
        "ix_evaluation_gate_policies_workspace_created",
        table_name="evaluation_release_gate_policies",
    )
    op.drop_table("evaluation_release_gate_policies")
    op.drop_index("ix_evaluation_ablation_run", table_name="evaluation_ablation_results")
    op.drop_table("evaluation_ablation_results")
    op.drop_constraint(
        "uq_evaluation_comparisons_workspace_id",
        "evaluation_experiment_comparisons",
        type_="unique",
    )
