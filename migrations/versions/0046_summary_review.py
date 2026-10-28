"""Persist immutable protected-source reviews and their fixed job bindings.

Revision ID: 0046_summary_review
Revises: 0045_run_budget_ledger
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0046_summary_review"
down_revision = "0045_run_budget_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid_type = postgresql.UUID(as_uuid=True)
    json_type = postgresql.JSONB(astext_type=sa.Text())
    now = sa.func.now()

    op.create_table(
        "summary_review_records",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("thread_id", uuid_type, nullable=False),
        sa.Column("trigger_run_id", uuid_type, nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("before_sequence", sa.Integer(), nullable=False),
        sa.Column("source_count", sa.Integer(), nullable=False),
        sa.Column("excerpt_count", sa.Integer(), nullable=False),
        sa.Column("source_manifest_sha256", sa.String(64), nullable=False),
        sa.Column("review_hash", sa.String(64), nullable=False),
        sa.Column("review_basis", sa.String(64), nullable=False),
        sa.Column("completeness_status", sa.String(32), nullable=False),
        sa.Column("contract_version", sa.String(64), nullable=False),
        sa.Column("protected_manifest_attested", sa.Boolean(), nullable=False),
        sa.Column("reviewer_admin_verified", sa.Boolean(), nullable=False),
        sa.Column("manifest_json", json_type, nullable=False),
        sa.Column("audit_json", json_type, nullable=False),
        sa.Column("reviewed_by", uuid_type, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=now,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "thread_id"],
            ["agent_threads.workspace_id", "agent_threads.id"],
            name="fk_summary_review_records_thread",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "trigger_run_id"],
            ["agent_runs.workspace_id", "agent_runs.id"],
            name="fk_summary_review_records_trigger_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by"],
            ["users.id"],
            name="fk_summary_review_records_reviewer",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_summary_review_records"),
        sa.UniqueConstraint(
            "workspace_id",
            "thread_id",
            "revision",
            name="uq_summary_review_records_revision",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "thread_id",
            "id",
            name="uq_summary_review_records_scope_id",
        ),
        sa.CheckConstraint(
            "revision > 0",
            name="ck_summary_review_records_revision",
        ),
        sa.CheckConstraint(
            "before_sequence > 0",
            name="ck_summary_review_records_before_sequence",
        ),
        sa.CheckConstraint(
            "source_count > 0 AND source_count <= 200",
            name="ck_summary_review_records_source_count",
        ),
        sa.CheckConstraint(
            "excerpt_count >= 0 AND excerpt_count <= 20000",
            name="ck_summary_review_records_excerpt_count",
        ),
        sa.CheckConstraint(
            "source_manifest_sha256 ~ '^[0-9a-f]{64}$' "
            "AND review_hash ~ '^[0-9a-f]{64}$'",
            name="ck_summary_review_records_hash_formats",
        ),
        sa.CheckConstraint(
            "review_basis = 'ADMIN_EXACT_EXCERPTS_V1'",
            name="ck_summary_review_records_basis",
        ),
        sa.CheckConstraint(
            "contract_version = 'summary-review-store-v1'",
            name="ck_summary_review_records_contract_version",
        ),
        sa.CheckConstraint(
            "completeness_status = 'ATTESTED_NOT_PROVEN'",
            name="ck_summary_review_records_completeness",
        ),
        sa.CheckConstraint(
            "protected_manifest_attested = true AND reviewer_admin_verified = true",
            name="ck_summary_review_records_attestations",
        ),
        sa.CheckConstraint(
            "octet_length(manifest_json::text) <= 4194304 "
            "AND octet_length(audit_json::text) <= 4194304",
            name="ck_summary_review_records_json_size",
        ),
    )
    op.create_index(
        "ix_summary_review_records_thread_revision",
        "summary_review_records",
        ["workspace_id", "thread_id", "revision"],
    )
    op.create_index(
        "ix_summary_review_records_reviewer",
        "summary_review_records",
        ["reviewed_by", "created_at"],
    )

    op.create_table(
        "summary_review_sources",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("thread_id", uuid_type, nullable=False),
        sa.Column("review_id", uuid_type, nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.String(256), nullable=False),
        sa.Column("source_identity", sa.String(64), nullable=False),
        sa.Column("turn_id", uuid_type, nullable=False),
        sa.Column("run_id", uuid_type, nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("source_watermark", sa.Integer(), nullable=False),
        sa.Column("original_ref", sa.String(256), nullable=False),
        sa.Column("revision_sha256", sa.String(64), nullable=False),
        sa.Column("user_input_sha256", sa.String(64), nullable=False),
        sa.Column("final_output_sha256", sa.String(64), nullable=False),
        sa.Column(
            "artifact_identities",
            json_type,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("review_decision", sa.String(16), nullable=False),
        sa.Column("review_reason", sa.String(128), nullable=False),
        sa.Column("eligibility_review_id", uuid_type, nullable=False),
        sa.Column("eligibility_revision", sa.Integer(), nullable=False),
        sa.Column("reviewed_by", uuid_type, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=now,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "thread_id", "review_id"],
            [
                "summary_review_records.workspace_id",
                "summary_review_records.thread_id",
                "summary_review_records.id",
            ],
            name="fk_summary_review_sources_record",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "turn_id"],
            ["thread_turns.workspace_id", "thread_turns.id"],
            name="fk_summary_review_sources_turn",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["agent_runs.workspace_id", "agent_runs.id"],
            name="fk_summary_review_sources_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["eligibility_review_id"],
            ["thread_summary_eligibility_records.id"],
            name="fk_summary_review_sources_eligibility",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reviewed_by"],
            ["users.id"],
            name="fk_summary_review_sources_reviewer",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_summary_review_sources"),
        sa.UniqueConstraint(
            "workspace_id",
            "thread_id",
            "review_id",
            "ordinal",
            name="uq_summary_review_sources_ordinal",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "thread_id",
            "review_id",
            "source_id",
            name="uq_summary_review_sources_source_id",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "thread_id",
            "review_id",
            "source_identity",
            name="uq_summary_review_sources_identity",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "thread_id",
            "review_id",
            "sequence",
            name="uq_summary_review_sources_sequence",
        ),
        sa.CheckConstraint(
            "ordinal > 0 AND sequence > 0 AND source_watermark > 0",
            name="ck_summary_review_sources_positive_order",
        ),
        sa.CheckConstraint(
            "source_identity ~ '^[0-9a-f]{64}$' "
            "AND revision_sha256 ~ '^[0-9a-f]{64}$' "
            "AND user_input_sha256 ~ '^[0-9a-f]{64}$' "
            "AND final_output_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_summary_review_sources_hash_formats",
        ),
        sa.CheckConstraint(
            "review_decision = 'ELIGIBLE' AND review_reason = 'ADMIN_REVIEWED'",
            name="ck_summary_review_sources_decision",
        ),
        sa.CheckConstraint(
            "eligibility_revision > 0",
            name="ck_summary_review_sources_eligibility_revision",
        ),
        sa.CheckConstraint(
            "artifact_identities = '[]'::jsonb",
            name="ck_summary_review_sources_artifact_free",
        ),
    )
    op.create_index(
        "ix_summary_review_sources_turn",
        "summary_review_sources",
        ["workspace_id", "thread_id", "turn_id"],
    )

    op.create_table(
        "summary_review_excerpts",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("thread_id", uuid_type, nullable=False),
        sa.Column("review_id", uuid_type, nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("fact_ref", sa.String(256), nullable=False),
        sa.Column("source_id", sa.String(256), nullable=False),
        sa.Column("source_identity", sa.String(64), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("excerpt_sha256", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=now,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "thread_id", "review_id"],
            [
                "summary_review_records.workspace_id",
                "summary_review_records.thread_id",
                "summary_review_records.id",
            ],
            name="fk_summary_review_excerpts_record",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "thread_id", "review_id", "source_id"],
            [
                "summary_review_sources.workspace_id",
                "summary_review_sources.thread_id",
                "summary_review_sources.review_id",
                "summary_review_sources.source_id",
            ],
            name="fk_summary_review_excerpts_source",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_summary_review_excerpts"),
        sa.UniqueConstraint(
            "workspace_id",
            "thread_id",
            "review_id",
            "ordinal",
            name="uq_summary_review_excerpts_ordinal",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "thread_id",
            "review_id",
            "fact_ref",
            name="uq_summary_review_excerpts_fact",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "thread_id",
            "review_id",
            "source_id",
            "role",
            "start_offset",
            "end_offset",
            name="uq_summary_review_excerpts_coordinate",
        ),
        sa.CheckConstraint(
            "role IN ('user', 'assistant')",
            name="ck_summary_review_excerpts_role",
        ),
        sa.CheckConstraint(
            "start_offset >= 0 AND end_offset > start_offset",
            name="ck_summary_review_excerpts_offsets",
        ),
        sa.CheckConstraint(
            "excerpt_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_summary_review_excerpts_hash",
        ),
    )
    op.create_index(
        "ix_summary_review_excerpts_source",
        "summary_review_excerpts",
        ["workspace_id", "thread_id", "review_id", "source_id"],
    )

    op.create_table(
        "summary_review_job_bindings",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column("workspace_id", uuid_type, nullable=False),
        sa.Column("thread_id", uuid_type, nullable=False),
        sa.Column("job_id", uuid_type, nullable=False),
        sa.Column("review_id", uuid_type, nullable=False),
        sa.Column("review_revision", sa.Integer(), nullable=False),
        sa.Column(
            "bound_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=now,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "thread_id", "job_id"],
            [
                "thread_summary_jobs.workspace_id",
                "thread_summary_jobs.thread_id",
                "thread_summary_jobs.id",
            ],
            name="fk_summary_review_job_bindings_job",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "thread_id", "review_id"],
            [
                "summary_review_records.workspace_id",
                "summary_review_records.thread_id",
                "summary_review_records.id",
            ],
            name="fk_summary_review_job_bindings_review",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_summary_review_job_bindings"),
        sa.UniqueConstraint(
            "workspace_id",
            "thread_id",
            "job_id",
            name="uq_summary_review_job_bindings_job",
        ),
        sa.CheckConstraint(
            "review_revision > 0",
            name="ck_summary_review_job_bindings_revision",
        ),
    )
    op.create_index(
        "ix_summary_review_job_bindings_review",
        "summary_review_job_bindings",
        ["workspace_id", "thread_id", "review_id", "review_revision"],
    )

    op.execute(
        """
        CREATE FUNCTION summary_review_reject_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION '% rows are append-only', TG_TABLE_NAME;
        END
        $$;
        """
    )
    for table_name in (
        "summary_review_records",
        "summary_review_sources",
        "summary_review_excerpts",
        "summary_review_job_bindings",
    ):
        op.execute(
            f"""
            CREATE TRIGGER {table_name}_append_only
            BEFORE UPDATE OR DELETE ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION summary_review_reject_change();
            """
        )
        op.execute(
            f"""
            CREATE TRIGGER {table_name}_reject_truncate
            BEFORE TRUNCATE ON {table_name}
            FOR EACH STATEMENT EXECUTE FUNCTION summary_review_reject_change();
            """
        )

    op.execute(
        """
        CREATE FUNCTION summary_review_validate_record_insert() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
          frozen_manifest jsonb;
          organization_id_value uuid;
          organization_role_value text;
          trigger_thread_id uuid;
        BEGIN
          SELECT agent_threads.workspace_id, agent_threads.id
          INTO organization_id_value, trigger_thread_id
          FROM agent_threads
          WHERE agent_threads.workspace_id = NEW.workspace_id
            AND agent_threads.id = NEW.thread_id;
          IF organization_id_value IS NULL THEN
            RAISE EXCEPTION 'Review Thread is outside the workspace';
          END IF;

          SELECT workspaces.organization_id
          INTO organization_id_value
          FROM workspaces
          WHERE workspaces.id = NEW.workspace_id;

          SELECT organization_memberships.role
          INTO organization_role_value
          FROM organization_memberships
          WHERE organization_memberships.organization_id = organization_id_value
            AND organization_memberships.user_id = NEW.reviewed_by;
          IF organization_role_value IS NULL
             OR organization_role_value NOT IN ('OWNER', 'ADMIN')
          THEN
            RAISE EXCEPTION 'Review author is not a current organization administrator';
          END IF;

          SELECT agent_runs.thread_id, agent_runs.effective_thread_history_snapshot->'source'
          INTO trigger_thread_id, frozen_manifest
          FROM agent_runs
          WHERE agent_runs.workspace_id = NEW.workspace_id
            AND agent_runs.id = NEW.trigger_run_id;
          IF trigger_thread_id IS DISTINCT FROM NEW.thread_id OR frozen_manifest IS NULL THEN
            RAISE EXCEPTION 'Review trigger Run has no frozen Thread source';
          END IF;
          IF NEW.manifest_json IS DISTINCT FROM frozen_manifest THEN
            RAISE EXCEPTION 'Review manifest differs from the frozen Run source';
          END IF;

          IF NEW.manifest_json->>'status' <> 'COMPLETE'
             OR (NEW.manifest_json->>'provenance_complete')::boolean IS NOT TRUE
             OR (NEW.manifest_json->>'work_limit_reached')::boolean IS NOT FALSE
             OR (NEW.manifest_json->>'selected_artifact_count')::integer <> 0
             OR (NEW.manifest_json->>'available_artifact_count')::integer <> 0
             OR (NEW.manifest_json->>'selected_source_count')::integer
                <> (NEW.manifest_json->>'available_eligible_source_count')::integer
             OR (NEW.manifest_json->>'selected_source_count')::integer
                <> (NEW.manifest_json->>'available_source_count')::integer
          THEN
            RAISE EXCEPTION 'Review manifest is not a complete artifact-free source set';
          END IF;
          IF NEW.manifest_json->'scope'->>'workspace_id' IS DISTINCT FROM NEW.workspace_id::text
             OR NEW.manifest_json->'scope'->>'thread_id' IS DISTINCT FROM NEW.thread_id::text
             OR NEW.manifest_json->'scope'->>'before_run_id'
                IS DISTINCT FROM NEW.trigger_run_id::text
             OR (NEW.manifest_json->'scope'->>'before_sequence')::integer
                IS DISTINCT FROM NEW.before_sequence
             OR (NEW.manifest_json->>'selected_source_count')::integer
                IS DISTINCT FROM NEW.source_count
             OR NEW.manifest_json->>'manifest_sha256'
                IS DISTINCT FROM NEW.source_manifest_sha256
          THEN
            RAISE EXCEPTION 'Review header differs from its frozen source scope';
          END IF;
          IF jsonb_array_length(NEW.manifest_json->'selected_sources') <> NEW.source_count
             OR NEW.audit_json->>'contract_version' <> 'summary-protected-review-v1'
             OR NEW.audit_json->>'workspace_id' IS DISTINCT FROM NEW.workspace_id::text
             OR NEW.audit_json->>'thread_id' IS DISTINCT FROM NEW.thread_id::text
             OR NEW.audit_json->>'before_run_id' IS DISTINCT FROM NEW.trigger_run_id::text
             OR (NEW.audit_json->>'before_sequence')::integer
                IS DISTINCT FROM NEW.before_sequence
             OR (NEW.audit_json->>'protected_manifest_complete_asserted')::boolean
                IS NOT TRUE
             OR (NEW.audit_json->>'server_revalidation_required')::boolean
                IS NOT TRUE
             OR jsonb_array_length(NEW.audit_json->'sources') <> NEW.source_count
             OR jsonb_array_length(NEW.audit_json->'excerpts') <> NEW.excerpt_count
          THEN
            RAISE EXCEPTION 'Review audit metadata differs from its persisted scope';
          END IF;
          RETURN NEW;
        END
        $$;

        CREATE TRIGGER summary_review_records_validate_insert
        BEFORE INSERT ON summary_review_records
        FOR EACH ROW EXECUTE FUNCTION summary_review_validate_record_insert();
        """
    )

    op.execute(
        """
        CREATE FUNCTION summary_review_validate_source_insert() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
          record_trigger_run_id uuid;
          record_before_sequence integer;
          record_reviewer uuid;
          eligibility_row thread_summary_eligibility_records%ROWTYPE;
        BEGIN
          SELECT summary_review_records.trigger_run_id,
                 summary_review_records.before_sequence,
                 summary_review_records.reviewed_by
          INTO record_trigger_run_id, record_before_sequence, record_reviewer
          FROM summary_review_records
          WHERE summary_review_records.workspace_id = NEW.workspace_id
            AND summary_review_records.thread_id = NEW.thread_id
            AND summary_review_records.id = NEW.review_id;
          IF record_trigger_run_id IS NULL THEN
            RAISE EXCEPTION 'Review source has no persisted review';
          END IF;

          SELECT *
          INTO eligibility_row
          FROM thread_summary_eligibility_records
          WHERE thread_summary_eligibility_records.id = NEW.eligibility_review_id
            AND thread_summary_eligibility_records.workspace_id = NEW.workspace_id
            AND thread_summary_eligibility_records.thread_id = NEW.thread_id
            AND thread_summary_eligibility_records.turn_id = NEW.turn_id
            AND thread_summary_eligibility_records.run_id = NEW.run_id
            AND thread_summary_eligibility_records.source_identity = NEW.source_identity
            AND thread_summary_eligibility_records.revision = NEW.eligibility_revision
            AND thread_summary_eligibility_records.reviewed_by = NEW.reviewed_by;
          IF eligibility_row.id IS NULL
             OR eligibility_row.decision <> 'ELIGIBLE'
             OR eligibility_row.reason_code <> 'ADMIN_REVIEWED'
             OR eligibility_row.reviewed_from_run_id <> record_trigger_run_id
             OR eligibility_row.reviewed_by <> record_reviewer
          THEN
            RAISE EXCEPTION 'Review source eligibility row is not authoritative';
          END IF;
          IF EXISTS (
            SELECT 1
            FROM thread_summary_eligibility_records AS newer
            WHERE newer.workspace_id = NEW.workspace_id
              AND newer.thread_id = NEW.thread_id
              AND newer.source_identity = NEW.source_identity
              AND newer.revision > NEW.eligibility_revision
          ) THEN
            RAISE EXCEPTION 'Review source eligibility revision is not latest';
          END IF;
          IF NEW.source_watermark <> record_before_sequence
             OR NOT EXISTS (
               SELECT 1
               FROM agent_threads
               JOIN thread_turns
                 ON thread_turns.workspace_id = agent_threads.workspace_id
                AND thread_turns.thread_id = agent_threads.id
               JOIN agent_runs
                 ON agent_runs.workspace_id = thread_turns.workspace_id
                AND agent_runs.thread_id = thread_turns.thread_id
                AND agent_runs.id = thread_turns.agent_run_id
               WHERE agent_threads.workspace_id = NEW.workspace_id
                 AND agent_threads.id = NEW.thread_id
                 AND thread_turns.id = NEW.turn_id
                 AND thread_turns.sequence = NEW.sequence
                 AND thread_turns.agent_run_id = NEW.run_id
                 AND agent_runs.id = NEW.run_id
                 AND agent_runs.thread_turn_id = NEW.turn_id
             )
          THEN
            RAISE EXCEPTION 'Review source does not match its frozen Thread exchange';
          END IF;
          RETURN NEW;
        END
        $$;

        CREATE TRIGGER summary_review_sources_validate_insert
        BEFORE INSERT ON summary_review_sources
        FOR EACH ROW EXECUTE FUNCTION summary_review_validate_source_insert();
        """
    )

    op.execute(
        """
        CREATE FUNCTION summary_review_validate_excerpt_insert() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
          source_turn_id uuid;
          source_run_id uuid;
          source_identity_value text;
          source_text text;
          excerpt_text text;
        BEGIN
          SELECT summary_review_sources.turn_id,
                 summary_review_sources.run_id,
                 summary_review_sources.source_identity
          INTO source_turn_id, source_run_id, source_identity_value
          FROM summary_review_sources
          WHERE summary_review_sources.workspace_id = NEW.workspace_id
            AND summary_review_sources.thread_id = NEW.thread_id
            AND summary_review_sources.review_id = NEW.review_id
            AND summary_review_sources.source_id = NEW.source_id;
          IF source_turn_id IS NULL
             OR source_identity_value IS DISTINCT FROM NEW.source_identity
          THEN
            RAISE EXCEPTION 'Review excerpt source identity differs';
          END IF;

          IF NEW.role = 'user' THEN
            SELECT thread_turns.user_input
            INTO source_text
            FROM thread_turns
            WHERE thread_turns.workspace_id = NEW.workspace_id
              AND thread_turns.thread_id = NEW.thread_id
              AND thread_turns.id = source_turn_id;
          ELSE
            SELECT agent_runs.final_output
            INTO source_text
            FROM agent_runs
            WHERE agent_runs.workspace_id = NEW.workspace_id
              AND agent_runs.thread_id = NEW.thread_id
              AND agent_runs.id = source_run_id;
          END IF;
          IF source_text IS NULL
             OR NEW.end_offset > char_length(source_text)
          THEN
            RAISE EXCEPTION 'Review excerpt coordinate escapes its source';
          END IF;
          excerpt_text := substring(
            source_text
            FROM NEW.start_offset + 1
            FOR NEW.end_offset - NEW.start_offset
          );
          IF encode(sha256(convert_to(excerpt_text, 'UTF8')), 'hex')
             IS DISTINCT FROM NEW.excerpt_sha256
          THEN
            RAISE EXCEPTION 'Review excerpt digest differs from the authoritative source';
          END IF;
          RETURN NEW;
        END
        $$;

        CREATE TRIGGER summary_review_excerpts_validate_insert
        BEFORE INSERT ON summary_review_excerpts
        FOR EACH ROW EXECUTE FUNCTION summary_review_validate_excerpt_insert();
        """
    )

    op.execute(
        """
        CREATE FUNCTION summary_review_verify_children() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
          actual_source_count integer;
          actual_excerpt_count integer;
        BEGIN
          SELECT count(*)
          INTO actual_source_count
          FROM summary_review_sources
          WHERE summary_review_sources.workspace_id = NEW.workspace_id
            AND summary_review_sources.thread_id = NEW.thread_id
            AND summary_review_sources.review_id = NEW.id;
          SELECT count(*)
          INTO actual_excerpt_count
          FROM summary_review_excerpts
          WHERE summary_review_excerpts.workspace_id = NEW.workspace_id
            AND summary_review_excerpts.thread_id = NEW.thread_id
            AND summary_review_excerpts.review_id = NEW.id;
          IF actual_source_count <> NEW.source_count
             OR actual_excerpt_count <> NEW.excerpt_count
             OR jsonb_array_length(NEW.manifest_json->'selected_sources')
                <> actual_source_count
             OR jsonb_array_length(NEW.audit_json->'sources') <> actual_source_count
             OR jsonb_array_length(NEW.audit_json->'excerpts') <> actual_excerpt_count
          THEN
            RAISE EXCEPTION 'Review row counts do not match persisted children';
          END IF;

          IF EXISTS (
            SELECT 1
            FROM jsonb_array_elements(NEW.manifest_json->'selected_sources') AS item
            WHERE NOT EXISTS (
              SELECT 1
              FROM summary_review_sources AS source
              WHERE source.workspace_id = NEW.workspace_id
                AND source.thread_id = NEW.thread_id
                AND source.review_id = NEW.id
                AND source.source_id = item->>'source_id'
                AND source.turn_id = (item->>'turn_id')::uuid
                AND source.run_id = (item->>'run_id')::uuid
                AND source.sequence = (item->>'sequence')::integer
                AND source.source_watermark = (item->>'watermark')::integer
                AND source.original_ref = item->>'original_ref'
                AND source.revision_sha256 = item->>'revision_sha256'
                AND source.user_input_sha256 = item->>'user_input_sha256'
                AND source.final_output_sha256 = item->>'final_output_sha256'
                AND source.artifact_identities = item->'artifact_identities'
            )
          ) THEN
            RAISE EXCEPTION 'Review source rows differ from the frozen manifest';
          END IF;

          IF EXISTS (
            SELECT 1
            FROM jsonb_array_elements(NEW.audit_json->'sources') AS item
            WHERE NOT EXISTS (
              SELECT 1
              FROM summary_review_sources AS source
              WHERE source.workspace_id = NEW.workspace_id
                AND source.thread_id = NEW.thread_id
                AND source.review_id = NEW.id
                AND source.source_id = item->>'source_id'
                AND source.source_identity = item->>'source_identity'
                AND source.revision_sha256 = item->>'source_revision_sha256'
                AND source.turn_id = (item->>'turn_id')::uuid
                AND source.run_id = (item->>'run_id')::uuid
                AND source.sequence = (item->>'sequence')::integer
                AND source.source_watermark = (item->>'source_watermark')::integer
                AND source.eligibility_review_id = (item->>'review_id')::uuid
                AND source.reviewed_by = (item->>'reviewed_by')::uuid
                AND source.eligibility_revision = (item->>'review_revision')::integer
                AND item->>'reviewer_is_workspace_admin' = 'true'
                AND item->>'review_decision' = 'ELIGIBLE'
            )
          ) THEN
            RAISE EXCEPTION 'Review audit sources differ from persisted rows';
          END IF;

          IF EXISTS (
            SELECT 1
            FROM jsonb_array_elements(NEW.audit_json->'excerpts') AS item
            WHERE NOT EXISTS (
              SELECT 1
              FROM summary_review_excerpts AS excerpt
              WHERE excerpt.workspace_id = NEW.workspace_id
                AND excerpt.thread_id = NEW.thread_id
                AND excerpt.review_id = NEW.id
                AND excerpt.fact_ref = item->>'fact_ref'
                AND excerpt.source_id = item->>'source_id'
                AND excerpt.source_identity = item->>'source_identity'
                AND excerpt.role = item->>'role'
                AND excerpt.start_offset = (item->>'start_offset')::integer
                AND excerpt.end_offset = (item->>'end_offset')::integer
            )
          ) THEN
            RAISE EXCEPTION 'Review audit excerpts differ from persisted rows';
          END IF;
          RETURN NEW;
        END
        $$;

        CREATE CONSTRAINT TRIGGER summary_review_records_verify_children
        AFTER INSERT ON summary_review_records
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION summary_review_verify_children();
        """
    )

    op.execute(
        """
        CREATE FUNCTION summary_review_validate_job_binding_insert() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
          review_trigger_run_id uuid;
          review_before_sequence integer;
          review_hash_value text;
          review_source_count integer;
          job_trigger_run_id uuid;
          job_source_watermark integer;
          job_scope_review_id text;
          job_scope_review_revision text;
          job_scope_review_hash text;
          job_source_count integer;
        BEGIN
          SELECT summary_review_records.trigger_run_id,
                 summary_review_records.before_sequence,
                 summary_review_records.review_hash,
                 summary_review_records.source_count
          INTO review_trigger_run_id,
               review_before_sequence,
               review_hash_value,
               review_source_count
          FROM summary_review_records
          WHERE summary_review_records.workspace_id = NEW.workspace_id
            AND summary_review_records.thread_id = NEW.thread_id
            AND summary_review_records.id = NEW.review_id
            AND summary_review_records.revision = NEW.review_revision;
          IF review_trigger_run_id IS NULL THEN
            RAISE EXCEPTION 'Review binding does not name one exact review revision';
          END IF;

          SELECT thread_summary_jobs.trigger_run_id,
                 thread_summary_jobs.source_watermark,
                 thread_summary_jobs.source_scope->>'summary_review_id',
                 thread_summary_jobs.source_scope->>'summary_review_revision',
                 thread_summary_jobs.source_scope->>'summary_review_hash'
          INTO job_trigger_run_id,
               job_source_watermark,
               job_scope_review_id,
               job_scope_review_revision,
               job_scope_review_hash
          FROM thread_summary_jobs
          WHERE thread_summary_jobs.workspace_id = NEW.workspace_id
            AND thread_summary_jobs.thread_id = NEW.thread_id
            AND thread_summary_jobs.id = NEW.job_id;
          IF job_trigger_run_id IS DISTINCT FROM review_trigger_run_id
             OR job_source_watermark IS DISTINCT FROM review_before_sequence
          THEN
            RAISE EXCEPTION 'Summary job scope differs from the fixed review';
          END IF;

          IF job_scope_review_id IS DISTINCT FROM NEW.review_id::text
             OR job_scope_review_revision IS DISTINCT FROM NEW.review_revision::text
             OR job_scope_review_hash IS DISTINCT FROM review_hash_value
          THEN
            RAISE EXCEPTION 'Summary job scope does not name the fixed review';
          END IF;

          SELECT count(*)
          INTO job_source_count
          FROM thread_summary_job_sources
          WHERE thread_summary_job_sources.workspace_id = NEW.workspace_id
            AND thread_summary_job_sources.thread_id = NEW.thread_id
            AND thread_summary_job_sources.job_id = NEW.job_id;
          IF job_source_count <> review_source_count
             OR EXISTS (
               SELECT 1
               FROM summary_review_sources AS reviewed
               FULL OUTER JOIN thread_summary_job_sources AS job_source
                 ON job_source.workspace_id = reviewed.workspace_id
                AND job_source.thread_id = reviewed.thread_id
                AND job_source.ordinal = reviewed.ordinal
               WHERE (
                   (reviewed.workspace_id = NEW.workspace_id
                    AND reviewed.thread_id = NEW.thread_id
                    AND reviewed.review_id = NEW.review_id)
                   OR
                   (job_source.workspace_id = NEW.workspace_id
                    AND job_source.thread_id = NEW.thread_id
                    AND job_source.job_id = NEW.job_id)
                 )
                 AND (
                   reviewed.id IS NULL
                   OR job_source.id IS NULL
                   OR job_source.source_id IS DISTINCT FROM reviewed.source_id
                   OR job_source.turn_id IS DISTINCT FROM reviewed.turn_id
                   OR job_source.run_id IS DISTINCT FROM reviewed.run_id
                   OR job_source.sequence IS DISTINCT FROM reviewed.sequence
                   OR job_source.original_ref IS DISTINCT FROM reviewed.original_ref
                   OR job_source.source_identity_hash
                      IS DISTINCT FROM reviewed.source_identity
                   OR job_source.revision_sha256 IS DISTINCT FROM reviewed.revision_sha256
                   OR job_source.user_input_sha256 IS DISTINCT FROM reviewed.user_input_sha256
                   OR job_source.final_output_sha256
                      IS DISTINCT FROM reviewed.final_output_sha256
                   OR job_source.artifact_identities
                      IS DISTINCT FROM reviewed.artifact_identities
                   OR job_source.summary_eligibility
                      IS DISTINCT FROM reviewed.review_decision
                   OR job_source.eligibility_reason
                      IS DISTINCT FROM reviewed.review_reason
                 )
             )
          THEN
            RAISE EXCEPTION 'Summary job source manifest differs from the fixed review';
          END IF;
          RETURN NEW;
        END
        $$;
        """
    )

    op.execute(
        """
        CREATE TRIGGER summary_review_job_bindings_validate_insert
        BEFORE INSERT ON summary_review_job_bindings
        FOR EACH ROW EXECUTE FUNCTION summary_review_validate_job_binding_insert();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM summary_review_records)
             OR EXISTS (SELECT 1 FROM summary_review_sources)
             OR EXISTS (SELECT 1 FROM summary_review_excerpts)
             OR EXISTS (SELECT 1 FROM summary_review_job_bindings)
          THEN
            RAISE EXCEPTION 'Cannot remove retained protected review evidence';
          END IF;
        END
        $$;
        """
    )
    op.execute(
        "DROP TRIGGER summary_review_job_bindings_validate_insert "
        "ON summary_review_job_bindings"
    )
    op.execute("DROP FUNCTION summary_review_validate_job_binding_insert()")
    op.execute("DROP TRIGGER summary_review_records_verify_children ON summary_review_records")
    op.execute("DROP FUNCTION summary_review_verify_children()")
    op.execute("DROP TRIGGER summary_review_excerpts_validate_insert ON summary_review_excerpts")
    op.execute("DROP FUNCTION summary_review_validate_excerpt_insert()")
    op.execute("DROP TRIGGER summary_review_sources_validate_insert ON summary_review_sources")
    op.execute("DROP FUNCTION summary_review_validate_source_insert()")
    op.execute("DROP TRIGGER summary_review_records_validate_insert ON summary_review_records")
    op.execute("DROP FUNCTION summary_review_validate_record_insert()")
    for table_name in (
        "summary_review_job_bindings",
        "summary_review_excerpts",
        "summary_review_sources",
        "summary_review_records",
    ):
        op.execute(
            f"DROP TRIGGER IF EXISTS {table_name}_reject_truncate ON {table_name}"
        )
        op.execute(f"DROP TRIGGER {table_name}_append_only ON {table_name}")
    op.execute("DROP FUNCTION summary_review_reject_change()")
    op.drop_index(
        "ix_summary_review_job_bindings_review",
        table_name="summary_review_job_bindings",
    )
    op.drop_table("summary_review_job_bindings")
    op.drop_index(
        "ix_summary_review_excerpts_source",
        table_name="summary_review_excerpts",
    )
    op.drop_table("summary_review_excerpts")
    op.drop_index(
        "ix_summary_review_sources_turn",
        table_name="summary_review_sources",
    )
    op.drop_table("summary_review_sources")
    op.drop_index(
        "ix_summary_review_records_reviewer",
        table_name="summary_review_records",
    )
    op.drop_index(
        "ix_summary_review_records_thread_revision",
        table_name="summary_review_records",
    )
    op.drop_table("summary_review_records")
