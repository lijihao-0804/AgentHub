"""PostgreSQL cache isolation, conflicting writers, and worker replay."""

import asyncio
from datetime import UTC, datetime
from threading import Barrier
from unittest.mock import Mock

import pytest
from sqlalchemy import update

from apps.worker.tasks.knowledge import _process_knowledge_ingestion
from packages.knowledge.composition import RetrievalComponents
from packages.knowledge.contracts import KnowledgeProviderError, SparseEncoding
from packages.knowledge.models import (
    DocumentRevision,
    DocumentVectorCache,
    IngestionJob,
    IngestionStage,
)
from packages.knowledge.vector_cache import resolve_document_vectors
from tests.integration.test_m3c_indexing import TEST_DATABASE_URL, _seed_revision, _settings
from tests.integration.test_m3c_indexing import db_factory as db_factory
from tests.integration.test_m3c_indexing import migrated_database as migrated_database

pytestmark = [
    pytest.mark.integration,
    pytest.mark.asyncio,
    pytest.mark.skipif(not TEST_DATABASE_URL, reason="Set AGENTHUB_TEST_DATABASE_URL"),
]


async def _workspace(factory, path):
    job, _, _ = await _seed_revision(factory, blob_root=path, stage=IngestionStage.EMBEDDING)
    async with factory() as session:
        return (await session.get(IngestionJob, job)).workspace_id


async def test_exact_text_workspace_and_independent_encoder_namespaces(db_factory, tmp_path):
    first = await _workspace(db_factory, tmp_path)
    second = await _workspace(db_factory, tmp_path)
    inputs = ["A  B", "A B", "A  B", "é", "e\u0301"]
    dense = Mock(side_effect=lambda texts: [(float(len(t)),) for t in texts])
    sparse = Mock(side_effect=lambda texts: [SparseEncoding((1,), (1.0,)) for _ in texts])

    async def run(workspace, key, modality, encoder):
        return await resolve_document_vectors(
            db_factory,
            workspace,
            inputs,
            encoder,
            modality=modality,
            fingerprint=key,
            dimension=1 if modality == "dense" else None,
        )

    expected = await run(first, "a" * 64, "dense", dense)
    assert dense.call_args.args == (["A  B", "A B", "é", "e\u0301"],)
    assert expected == ((4.0,), (3.0,), (4.0,), (1.0,), (2.0,))
    await run(first, "a" * 64, "sparse", sparse)
    await run(first, "b" * 64, "dense", dense)
    await run(first, "a" * 64, "sparse", sparse)
    assert (dense.call_count, sparse.call_count) == (2, 1)
    await run(second, "a" * 64, "sparse", sparse)
    assert sparse.call_count == 2
    await run(first, None, "sparse", sparse)
    assert sparse.call_args.args == (inputs,)


async def test_full_index_batch_round_trips_and_reuses_every_vector(db_factory, tmp_path):
    workspace = await _workspace(db_factory, tmp_path)
    texts = [f"第 {index} 段正文" for index in range(62)]
    texts.extend([texts[0], texts[-1]])
    expected = {
        text: tuple((position + axis) / 2048 for axis in range(1024))
        for position, text in enumerate(texts[:62])
    }
    encode = Mock(side_effect=lambda batch: [expected[text] for text in batch])
    kwargs = dict(modality="dense", fingerprint="c" * 64, dimension=1024)
    first = await resolve_document_vectors(db_factory, workspace, texts, encode, **kwargs)
    assert first == tuple(expected[text] for text in texts)
    encode.assert_called_once_with(texts[:62])
    encode.reset_mock()
    second = await resolve_document_vectors(db_factory, workspace, texts, encode, **kwargs)
    assert second == first
    encode.assert_not_called()


@pytest.mark.parametrize("corrupt", [False, True])
async def test_concurrent_first_writes_and_repairs_return_one_winner(db_factory, tmp_path, corrupt):
    workspace = await _workspace(db_factory, tmp_path)
    if corrupt:
        await resolve_document_vectors(
            db_factory,
            workspace,
            ["x", "y"],
            lambda texts: [(3.0,)] * len(texts),
            modality="dense",
            fingerprint="a" * 64,
            dimension=1,
        )
        async with db_factory() as session, session.begin():
            await session.execute(
                update(DocumentVectorCache)
                .where(
                    DocumentVectorCache.workspace_id == workspace,
                )
                .values(vector=b"broken")
            )
    gate = Barrier(2, timeout=15)

    async def fill(texts, value):
        def encode(batch):
            gate.wait()
            return [(value,) for _ in batch]

        return await resolve_document_vectors(
            db_factory,
            workspace,
            texts,
            encode,
            modality="dense",
            fingerprint="a" * 64,
            dimension=1,
        )

    one, two = await asyncio.gather(fill(["x", "y"], 1.0), fill(["y", "x"], 2.0))
    assert one == two[::-1]
    assert one in (((1.0,), (1.0,)), ((2.0,), (2.0,)))


@pytest.mark.parametrize(("corrupt", "bad_repair"), [(False, False), (True, False), (True, True)])
async def test_cached_index_redelivery_still_requires_ack_and_valid_vectors(
    db_factory,
    tmp_path,
    corrupt,
    bad_repair,
):
    job_id, revision_id, _ = await _seed_revision(
        db_factory,
        blob_root=tmp_path,
        stage=IngestionStage.EMBEDDING,
        with_chunk=True,
    )
    settings = _settings("unused", tmp_path)
    dense = Mock(dimension=1)
    sparse = Mock()
    dense.document_cache_fingerprint.return_value = "a" * 64
    sparse.document_cache_fingerprint.return_value = "b" * 64
    dense.embed_documents.return_value = ((0.25,),)
    sparse.encode_documents.return_value = (SparseEncoding((1,), (1.0,)),)
    index = Mock()
    index.upsert.side_effect = [KnowledgeProviderError("QDRANT_UNAVAILABLE", "unavailable"), None]
    components = RetrievalComponents(dense, sparse, Mock(), index)

    async def ingest():
        await _process_knowledge_ingestion(job_id, settings, indexing_factory=lambda _: components)

    await ingest()
    async with db_factory() as session:
        job = await session.get(IngestionJob, job_id)
        revision = await session.get(DocumentRevision, revision_id)
        assert (job.status, job.stage, revision.ingestion_status) == (
            "PENDING",
            "INDEXING",
            "PROCESSING",
        )
        job.next_attempt_at = datetime.now(UTC)
        if corrupt:
            await session.execute(
                update(DocumentVectorCache)
                .where(
                    DocumentVectorCache.workspace_id == job.workspace_id,
                    DocumentVectorCache.modality == "dense",
                )
                .values(vector=b"broken")
            )
        await session.commit()
    if bad_repair:
        dense.embed_documents.return_value = ((float("nan"),),)
    await ingest()
    async with db_factory() as session:
        job = await session.get(IngestionJob, job_id)
        revision = await session.get(DocumentRevision, revision_id)
        assert dense.embed_documents.call_count == 1 + int(corrupt)
        assert sparse.encode_documents.call_count == 1
        if bad_repair:
            assert (job.status, revision.ingestion_status) == ("FAILED", "FAILED")
            assert job.last_error_code == "INVALID_PROVIDER_RESULT"
            assert index.upsert.call_count == 1
        else:
            assert (job.status, revision.ingestion_status) == ("SUCCEEDED", "READY")
            assert index.upsert.call_args_list[0] == index.upsert.call_args_list[1]
