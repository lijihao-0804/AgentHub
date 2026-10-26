from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from apps.api.app import create_app
from apps.api.dependencies import get_db_session
from apps.api.knowledge_dependencies import get_ingestion_queue, get_workspace_context
from packages.core.config.settings import Settings
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.knowledge.models import Document, DocumentRevision, IngestionJob, KnowledgeBase
from packages.knowledge.upload_security import validate_content_signature


class _UploadSession:
    def __init__(self, knowledge_base_id, workspace_id) -> None:
        self.knowledge_base = KnowledgeBase(
            id=knowledge_base_id,
            workspace_id=workspace_id,
            name="KB",
        )
        self.added: list[object] = []

    async def scalar(self, _statement):
        if not self.added:
            return self.knowledge_base
        if len(self.added) == 1:
            return None
        return None

    def add(self, value) -> None:
        if getattr(value, "id", None) is None:
            value.id = uuid4()
        now = datetime.now(UTC)
        for name, fallback in (
            ("created_at", now),
            ("updated_at", now),
            ("lifecycle_status", "ACTIVE"),
            ("ingestion_status", "PENDING"),
            ("status", "PENDING"),
            ("stage", "PARSING"),
            ("attempt_count", 0),
            ("last_error_code", None),
            ("safe_error_message", None),
        ):
            if getattr(value, name, None) is None:
                setattr(value, name, fallback)
        self.added.append(value)

    async def flush(self) -> None:
        for value in self.added:
            if getattr(value, "id", None) is None:
                value.id = uuid4()

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class _Queue:
    def __init__(self) -> None:
        self.calls = 0

    async def enqueue(self, _job_id) -> None:
        self.calls += 1


def _context(workspace_id) -> WorkspaceExecutionContext:
    principal = PrincipalContext(request_id="upload-h1", trace_id="upload-h1", user_id=str(uuid4()))
    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=principal,
            organization_id=str(uuid4()),
            org_role="OWNER",
        ),
        workspace_id=str(workspace_id),
        workspace_role="OWNER",
        permissions=frozenset({"knowledge_create"}),
    )


async def _post_upload(
    *,
    filename: str,
    content: bytes,
    media_type: str,
    oversized: bool = False,
) -> tuple[httpx.Response, _UploadSession, _Queue, Path]:
    workspace_id = uuid4()
    knowledge_base_id = uuid4()
    session = _UploadSession(knowledge_base_id, workspace_id)
    queue = _Queue()
    blob_root = Path("data") / f"upload-h1-{uuid4().hex}"
    app = create_app(
        Settings(
            testing=True,
            blob_root=str(blob_root),
            knowledge_max_upload_bytes=3 if oversized else 10 * 1024 * 1024,
        )
    )

    async def override_db():
        yield session

    async def override_context():
        return _context(workspace_id)

    async def override_queue():
        return queue

    app.dependency_overrides[get_db_session] = override_db
    app.dependency_overrides[get_workspace_context] = override_context
    app.dependency_overrides[get_ingestion_queue] = override_queue
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/v1/workspaces/{workspace_id}/knowledge-bases/{knowledge_base_id}/documents",
            files={"file": (filename, content, media_type)},
        )
    return response, session, queue, blob_root


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "content", "media_type", "code"),
    [
        ("../escape.txt", b"safe", "text/plain", "PATH_TRAVERSAL"),
        ("notes.txt", b"safe", "application/pdf", "INVALID_FILE"),
        ("broken.pdf", b"not a pdf", "application/pdf", "INVALID_FILE"),
    ],
)
async def test_hostile_uploads_fail_before_persistence(
    filename: str,
    content: bytes,
    media_type: str,
    code: str,
) -> None:
    response, session, queue, blob_root = await _post_upload(
        filename=filename,
        content=content,
        media_type=media_type,
    )
    try:
        assert response.status_code == 422
        assert response.json()["error"]["code"] == code
        assert session.added == []
        assert queue.calls == 0
        assert not any(blob_root.rglob("*"))
    finally:
        shutil.rmtree(blob_root, ignore_errors=True)


@pytest.mark.asyncio
async def test_oversized_upload_fails_before_revision_and_enqueue() -> None:
    response, session, queue, blob_root = await _post_upload(
        filename="large.txt",
        content=b"1234",
        media_type="text/plain",
        oversized=True,
    )
    try:
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "FILE_TOO_LARGE"
        assert session.added == []
        assert queue.calls == 0
        assert not any(blob_root.rglob("*"))
    finally:
        shutil.rmtree(blob_root, ignore_errors=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("character", ["é", "中", "🙂"])
async def test_complete_utf8_upload_allows_a_multibyte_character_across_prefix_boundary(
    character: str,
) -> None:
    response, session, queue, blob_root = await _post_upload(
        filename="boundary.txt",
        content=b"a" * 65_535 + character.encode("utf-8"),
        media_type="text/plain",
    )
    try:
        assert response.status_code == 202, response.text
        assert queue.calls == 1
        assert sum(isinstance(item, Document) for item in session.added) == 1
        assert sum(isinstance(item, DocumentRevision) for item in session.added) == 1
        assert sum(isinstance(item, IngestionJob) for item in session.added) == 1
    finally:
        shutil.rmtree(blob_root, ignore_errors=True)


@pytest.mark.asyncio
async def test_truly_invalid_utf8_upload_is_still_rejected() -> None:
    response, session, queue, blob_root = await _post_upload(
        filename="broken.txt",
        content=b"valid-prefix-\xff-invalid",
        media_type="text/plain",
    )
    try:
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "INVALID_FILE"
        assert session.added == []
        assert queue.calls == 0
        assert not any(blob_root.rglob("*"))
    finally:
        shutil.rmtree(blob_root, ignore_errors=True)


def test_text_prefix_accepts_an_incomplete_trailing_utf8_sequence() -> None:
    content = b"a" * 65_535 + "🙂".encode()

    validate_content_signature("boundary.txt", content[:65_536])
