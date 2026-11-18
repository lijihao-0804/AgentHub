"""Check chunk provenance constraints and transactional migration on a private database."""

import asyncio
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, insert, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from packages.core.database import create_database
from packages.knowledge.models import DocumentChunk, DocumentRevision
from tests.integration.test_auxiliary_run_step_migration import _migrate
from tests.integration.test_document_lifecycle import _seed

ADMIN = os.environ.get("AGENTHUB_TEST_ADMIN_DATABASE_URL", "")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not ADMIN, reason="Local admin URL required"),
]


def test_chunk_revision_pair_and_migration():
    admin = make_url(ADMIN).set(drivername="postgresql+psycopg", database="postgres")
    assert admin.host in {"localhost", "127.0.0.1", "::1"}
    name = "agenthub_chunk_pair_" + uuid4().hex[:12]
    server = create_engine(admin, isolation_level="AUTOCOMMIT")
    with server.connect() as conn:
        conn.exec_driver_sql(f'CREATE DATABASE "{name}"')
    server.dispose()
    url = admin.set(database=name)
    old = "0060_private_memory"
    new = "0061_chunk_document_revision"
    _migrate(url, old)

    async def setup():
        db, factory = create_database(
            url.set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)
        )
        try:
            return await _seed(factory)
        finally:
            await db.dispose()

    base = asyncio.run(setup())
    db = create_engine(url)
    revision_id, invalid_id = uuid4(), uuid4()
    scope = dict(workspace_id=base.workspace_id, knowledge_base_id=base.knowledge_base_id)
    common = dict(
        **scope,
        ordinal=0,
        normalized_content_hash="0" * 64,
        text="fixture",
        document_revision_id=revision_id,
    )
    try:
        with db.begin() as conn:
            conn.execute(
                insert(DocumentRevision).values(
                    **scope,
                    id=revision_id,
                    document_id=base.v1_document_id,
                    revision_number=1,
                    original_filename="file.txt",
                    blob_key="fixture",
                    media_type="text/plain",
                    file_size=7,
                )
            )
            conn.execute(
                insert(DocumentChunk).values(
                    **common, id=invalid_id, chunk_id="invalid", document_id=base.v2_document_id
                )
            )
            conn.execute(
                insert(DocumentChunk).values(
                    **common, chunk_id="valid", document_id=base.v1_document_id
                )
            )
        with pytest.raises(RuntimeError, match=str(invalid_id)):
            _migrate(url, new)
        with db.begin() as conn:
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == old
            assert (
                conn.scalar(
                    text("SELECT document_id FROM document_chunks WHERE id=:id"), {"id": invalid_id}
                )
                == base.v2_document_id
            )
            # This explicit fixture cleanup represents the operator decision.
            conn.execute(text("DELETE FROM document_chunks WHERE id=:id"), {"id": invalid_id})
        _migrate(url, new)
        with pytest.raises(IntegrityError) as exc, db.begin() as conn:
            conn.execute(
                insert(DocumentChunk).values(
                    **common, chunk_id="cross-document", document_id=base.v2_document_id
                )
            )
        assert exc.value.orig.diag.constraint_name == "fk_document_chunks_document_revision"
        with db.begin() as conn:
            conn.execute(
                insert(DocumentChunk).values(
                    **common, chunk_id="valid-new", document_id=base.v1_document_id
                )
            )
        _migrate(url, old, downgrade=True)
        with db.begin() as conn:
            assert conn.scalar(text("SELECT count(*) FROM document_chunks")) == 2
            conn.execute(
                insert(DocumentChunk).values(
                    **common, chunk_id="downgraded-cross", document_id=base.v2_document_id
                )
            )
            conn.execute(text("DELETE FROM document_chunks WHERE chunk_id='downgraded-cross'"))
        _migrate(url, new)
        with db.begin() as conn:
            conn.execute(text("DELETE FROM document_revisions WHERE id=:id"), {"id": revision_id})
            assert conn.scalar(text("SELECT count(*) FROM document_chunks")) == 0
    finally:
        db.dispose()
