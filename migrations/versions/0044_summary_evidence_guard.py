"""Keep summary job identity and persisted evidence pointers immutable.

Revision ID: 0044_summary_evidence_guard
Revises: 0043_summary_dispatch_boundary
"""

from alembic import op

revision = "0044_summary_evidence_guard"
down_revision = "0043_summary_dispatch_boundary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION thread_summary_guard_evidence_identity() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF (NEW.id, NEW.parent_artifact_id, NEW.parent_revision_hash, NEW.created_at)
            IS DISTINCT FROM
            (OLD.id, OLD.parent_artifact_id, OLD.parent_revision_hash, OLD.created_at)
          THEN
            RAISE EXCEPTION 'Thread summary job evidence identity is immutable';
          END IF;
          IF OLD.dispatch_id IS NOT NULL AND NEW.dispatch_id IS DISTINCT FROM OLD.dispatch_id THEN
            RAISE EXCEPTION 'Thread summary dispatch pointer cannot be cleared or replaced';
          END IF;
          IF OLD.response_id IS NOT NULL AND NEW.response_id IS DISTINCT FROM OLD.response_id THEN
            RAISE EXCEPTION 'Thread summary response pointer cannot be cleared or replaced';
          END IF;
          IF OLD.artifact_id IS NOT NULL AND NEW.artifact_id IS DISTINCT FROM OLD.artifact_id THEN
            RAISE EXCEPTION 'Thread summary artifact pointer cannot be cleared or replaced';
          END IF;
          RETURN NEW;
        END
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER thread_summary_evidence_identity_guard
        BEFORE UPDATE ON thread_summary_jobs
        FOR EACH ROW EXECUTE FUNCTION thread_summary_guard_evidence_identity();
        """
    )


def downgrade() -> None:
    # Dropping this guard would weaken the promised immutability of persisted
    # evidence, so rollback is allowed only before any summary jobs exist.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM thread_summary_jobs) THEN
            RAISE EXCEPTION 'Cannot remove retained summary evidence protection';
          END IF;
        END
        $$;
        """
    )
    op.execute("DROP TRIGGER thread_summary_evidence_identity_guard ON thread_summary_jobs")
    op.execute("DROP FUNCTION thread_summary_guard_evidence_identity()")
