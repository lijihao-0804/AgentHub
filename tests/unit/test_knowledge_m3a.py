from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import get_type_hints

import pytest

from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.knowledge.blob_store import BlobStoreError, LocalBlobStore
from packages.knowledge.contracts import KnowledgeRetriever, RetrievalQuery, RetrievedEvidence
from packages.knowledge.upload_security import (
    UploadSecurityError,
    UploadSecurityPolicy,
    enforce_stream_size,
    validate_content_signature,
    validate_declared_media_type,
    validate_original_filename,
)


async def _chunks(*values: bytes) -> AsyncIterator[bytes]:
    for value in values:
        yield value


def test_retrieval_contract_freezes_workspace_context_and_limits() -> None:
    query = RetrievalQuery(
        text="password reset", knowledge_base_id="kb-1", knowledge_snapshot_id="s-1"
    )

    assert query.dense_top_k == 30
    assert query.sparse_top_k == 30
    assert query.candidate_top_k == 20
    assert query.final_top_k == 6
    assert "workspace_id" not in RetrievalQuery.__dataclass_fields__
    assert get_type_hints(KnowledgeRetriever.retrieve)["context"] is WorkspaceExecutionContext
    assert set(RetrievedEvidence.__dataclass_fields__) == {
        "document_id",
        "document_revision_id",
        "chunk_id",
        "source",
        "locator",
        "text",
        "retrieval_score",
        "rerank_score",
        "metadata",
    }


@pytest.mark.parametrize("filename", ["../notes.txt", "folder/notes.txt", r"C:notes.txt"])
def test_upload_filename_cannot_escape_generated_storage_root(filename: str) -> None:
    with pytest.raises(UploadSecurityError) as error:
        validate_original_filename(filename)

    assert error.value.code == "PATH_TRAVERSAL"


def test_upload_security_checks_extension_media_signature_and_size() -> None:
    assert validate_original_filename("guide.PDF") == "guide.PDF"
    assert validate_declared_media_type("guide.PDF", "application/pdf") == "application/pdf"
    validate_content_signature("guide.PDF", b"%PDF-1.7")
    enforce_stream_size(10, UploadSecurityPolicy(max_file_size_bytes=10))

    with pytest.raises(UploadSecurityError, match="not supported"):
        validate_original_filename("archive.zip")
    with pytest.raises(UploadSecurityError, match="media type"):
        validate_declared_media_type("guide.PDF", "text/plain")
    with pytest.raises(UploadSecurityError, match="content"):
        validate_content_signature("guide.PDF", b"not a pdf")
    with pytest.raises(UploadSecurityError) as error:
        enforce_stream_size(11, UploadSecurityPolicy(max_file_size_bytes=10))
    assert error.value.code == "FILE_TOO_LARGE"


@pytest.mark.asyncio
async def test_local_blob_store_is_root_confined_and_reads_atomic_upload(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path / "blobs", read_chunk_size=3)
    stored = await store.put("workspace/revision.bin", _chunks(b"abc", b"def"))

    assert stored.size_bytes == 6
    assert (tmp_path / "blobs" / "workspace" / "revision.bin").read_bytes() == b"abcdef"
    assert b"".join([chunk async for chunk in store.read(stored.blob_key)]) == b"abcdef"

    with pytest.raises(BlobStoreError, match="BLOB_KEY_INVALID"):
        await store.put("../outside.bin", _chunks(b"nope"))
    with pytest.raises(BlobStoreError, match="BLOB_NOT_FOUND"):
        _ = b"".join([chunk async for chunk in store.read("missing.bin")])
