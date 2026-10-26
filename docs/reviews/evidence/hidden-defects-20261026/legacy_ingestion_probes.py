"""Offline upload/metadata probes, using existing ASGI test dependencies."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from packages.knowledge.models import DocumentRevision
from packages.knowledge.services import KnowledgeService
from tests.unit.test_upload_security_api import _context, _post_upload


@pytest.mark.asyncio
@pytest.mark.parametrize("character", ["é", "中", "🙂"])
async def test_valid_utf8_rejected_at_signature_prefix_boundary(character):
    content = b"a" * 65535 + character.encode("utf-8")
    assert content.decode("utf-8").endswith(character)
    response, _session, queue, _root = await _post_upload(
        filename="boundary.txt", content=content, media_type="text/plain"
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_FILE"
    assert queue.calls == 0


@pytest.mark.asyncio
async def test_upload_commit_ack_lost_deletes_referenced_blob():
    workspace_id, kb_id = uuid4(), uuid4()

    class UploadSession:
        reads = 0
        rows = []
        durable = []

        async def scalar(self, _statement):
            self.reads += 1
            return SimpleNamespace(id=kb_id) if self.reads == 1 else None

        def add(self, row):
            self.rows.append(row)

        async def flush(self):
            for row in self.rows:
                if getattr(row, "id", None) is None:
                    row.id = uuid4()

        async def commit(self):
            # Simulate server-side COMMIT success with a lost client acknowledgement.
            self.durable.extend(self.rows)
            raise ConnectionError("simulated commit acknowledgement lost")

        async def rollback(self):
            pass

    class Blobs:
        deleted = []

        async def put(self, key, _chunks):
            self.key = key
            return SimpleNamespace(blob_key=key, size_bytes=5)

        async def delete(self, key):
            self.deleted.append(key)

    async def chunks():
        yield b"valid"

    session, blobs = UploadSession(), Blobs()
    with pytest.raises(ConnectionError, match="acknowledgement"):
        await KnowledgeService().upload_document(
            session,
            context=_context(workspace_id),
            knowledge_base_id=kb_id,
            filename="valid.txt",
            media_type="text/plain",
            chunks=chunks(),
            blob_store=blobs,
            queue=SimpleNamespace(),
        )
    revision = next(row for row in session.durable if isinstance(row, DocumentRevision))
    assert revision.blob_key in blobs.deleted


@pytest.mark.asyncio
async def test_document_supersession_allows_two_node_cycle():
    workspace_id, kb_id, a_id, b_id = (uuid4() for _ in range(4))
    a = SimpleNamespace(id=a_id, effective_date=None, superseded_by_document_id=None)
    b = SimpleNamespace(id=b_id, effective_date=None, superseded_by_document_id=None)

    class LifecycleSession:
        def __init__(self):
            self.values = iter([a, b_id, b, a_id])

        async def scalar(self, _statement):
            return next(self.values)

        def add(self, _row):
            pass

        async def commit(self):
            pass

        async def refresh(self, _row):
            pass

    context = _context(workspace_id)
    context = context.model_copy(update={"permissions": frozenset({"knowledge_edit"})})
    service, session = KnowledgeService(), LifecycleSession()
    for document_id, successor_id in [(a_id, b_id), (b_id, a_id)]:
        await service.update_document_lifecycle(
            session,
            context=context,
            knowledge_base_id=kb_id,
            document_id=document_id,
            effective_date=None,
            superseded_by_document_id=successor_id,
            set_effective_date=False,
            set_superseded_by=True,
        )
    assert a.superseded_by_document_id == b.id
    assert b.superseded_by_document_id == a.id
