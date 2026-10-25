"""Versioned derived Wiki; raw snapshot and runtime contracts unchanged."""

from alembic import op


revision = "0032_wiki"
down_revision = "0031_handoff_cases"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "\nCREATE TABLE wiki_compilations (\n\tid UUID NOT NULL, \n\tworkspace_id UUID NOT NULL, \n\tknowledge_base_id UUID NOT NULL, \n\tsource_snapshot_id UUID NOT NULL, \n\tsource_snapshot_hash VARCHAR(64) NOT NULL, \n\tidentity_hash VARCHAR(64) NOT NULL, \n\tcompiler_spec JSONB NOT NULL, \n\trequested_by VARCHAR(64) NOT NULL, \n\tstatus VARCHAR(16) NOT NULL, \n\tlease_token VARCHAR(64), \n\tlease_expires_at TIMESTAMP WITH TIME ZONE, \n\tattempt_count INTEGER NOT NULL, \n\terror_code VARCHAR(64), \n\tcontent_hash VARCHAR(64), \n\ttelemetry JSONB NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n\tpublished_at TIMESTAMP WITH TIME ZONE, \n\tpublished_by VARCHAR(64), \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(workspace_id, knowledge_base_id, source_snapshot_id) REFERENCES knowledge_snapshots (workspace_id, knowledge_base_id, id) ON DELETE RESTRICT, \n\tUNIQUE (workspace_id, knowledge_base_id, id), \n\tUNIQUE (workspace_id, knowledge_base_id, identity_hash), \n\tCHECK (status IN ('PENDING','PROCESSING','READY','PUBLISHED','FAILED'))\n)\n\n"
    )
    op.execute(
        "CREATE INDEX ix_wiki_jobs_reconcile ON wiki_compilations (status, lease_expires_at)"
    )
    op.execute(
        "\nCREATE TABLE wiki_extractions (\n\tid UUID NOT NULL, \n\tworkspace_id UUID NOT NULL, \n\tknowledge_base_id UUID NOT NULL, \n\tdocument_revision_id UUID NOT NULL, \n\tcompiler_hash VARCHAR(64) NOT NULL, \n\tinput_hash VARCHAR(64) NOT NULL, \n\tpayload JSONB NOT NULL, \n\ttelemetry JSONB NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(workspace_id, knowledge_base_id, document_revision_id) REFERENCES document_revisions (workspace_id, knowledge_base_id, id) ON DELETE RESTRICT, \n\tUNIQUE (workspace_id, document_revision_id, compiler_hash)\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE wiki_pages (\n\tid UUID NOT NULL, \n\tworkspace_id UUID NOT NULL, \n\tknowledge_base_id UUID NOT NULL, \n\tcompilation_id UUID NOT NULL, \n\tpage_key VARCHAR(64) NOT NULL, \n\ttitle VARCHAR(120) NOT NULL, \n\tkind VARCHAR(16) NOT NULL, \n\tmarkdown TEXT NOT NULL, \n\tclaims JSONB NOT NULL, \n\trelations JSONB NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(workspace_id, knowledge_base_id, compilation_id) REFERENCES wiki_compilations (workspace_id, knowledge_base_id, id) ON DELETE RESTRICT, \n\tUNIQUE (compilation_id, page_key), \n\tCHECK (kind IN ('concept','entity','topic'))\n)\n\n"
    )
    op.execute("""
    CREATE FUNCTION wiki_guard_compilation() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Wiki history is retained';
      END IF;
      IF (NEW.workspace_id, NEW.knowledge_base_id, NEW.source_snapshot_id,
          NEW.source_snapshot_hash, NEW.identity_hash, NEW.compiler_spec, NEW.requested_by)
        IS DISTINCT FROM
         (OLD.workspace_id, OLD.knowledge_base_id, OLD.source_snapshot_id,
          OLD.source_snapshot_hash, OLD.identity_hash, OLD.compiler_spec, OLD.requested_by) THEN
        RAISE EXCEPTION 'Wiki compilation identity is immutable';
      END IF;
      IF OLD.status = 'PUBLISHED' AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION 'Wiki publication is immutable';
      END IF;
      IF OLD.status = 'READY' AND (NEW.status NOT IN ('READY','PUBLISHED') OR
          (NEW.content_hash, NEW.telemetry) IS DISTINCT FROM (OLD.content_hash, OLD.telemetry)) THEN
        RAISE EXCEPTION 'Ready Wiki content is immutable';
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER wiki_compilation_guard BEFORE UPDATE OR DELETE ON wiki_compilations
      FOR EACH ROW EXECUTE FUNCTION wiki_guard_compilation();
    CREATE FUNCTION wiki_guard_page() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE parent_status text;
    BEGIN
      IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'Wiki page revisions are immutable';
      END IF;
      SELECT status INTO parent_status FROM wiki_compilations
        WHERE id = NEW.compilation_id FOR SHARE;
      IF parent_status <> 'PROCESSING' THEN
        RAISE EXCEPTION 'Pages may only be installed during compilation';
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER wiki_page_guard BEFORE INSERT OR UPDATE OR DELETE ON wiki_pages
      FOR EACH ROW EXECUTE FUNCTION wiki_guard_page();
    CREATE FUNCTION wiki_guard_extraction() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'Wiki extraction cache is immutable'; END $$;
    CREATE TRIGGER wiki_extraction_guard BEFORE UPDATE OR DELETE ON wiki_extractions
      FOR EACH ROW EXECUTE FUNCTION wiki_guard_extraction();
    """)


def downgrade():
    op.drop_table("wiki_pages")
    op.drop_table("wiki_extractions")
    op.drop_table("wiki_compilations")
    for name in ("wiki_guard_compilation", "wiki_guard_page", "wiki_guard_extraction"):
        op.execute(f"DROP FUNCTION IF EXISTS {name}()")
