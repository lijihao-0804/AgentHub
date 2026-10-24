"""Isolated, resumable FiQA retrieval baseline through production AgentHub code.

This is a retrieval benchmark, not an answer-quality evaluation or a formal
platform Experiment. Run explicit migrations before ``prepare``. Preparation
reads only ``corpus.jsonl``; test queries and labels are opened by ``baseline``
only after an exact protocol has bound FiQA to this baseline implementation.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import subprocess
import time
import zipfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4, uuid5

from qdrant_client import QdrantClient
from sqlalchemy import func, insert, select

from apps.worker.tasks.knowledge import _vector_records
from benchmarks.public_rag.data import SourceDocument, _load_corpus, load_beir
from benchmarks.public_rag.scifact_runner import offline_models
from benchmarks.public_rag.scifact_runner import settings as scifact_settings
from packages.control_plane.models import Organization, OrganizationMembership, User, Workspace
from packages.control_plane.services import TenantService
from packages.core.database import create_database
from packages.core.execution_context.models import PrincipalContext
from packages.knowledge.chunking import build_deterministic_chunks
from packages.knowledge.composition import production_retrieval_components
from packages.knowledge.contracts import RetrievalQuery, RetrievalStrategy
from packages.knowledge.models import (
    Document,
    DocumentChunk,
    DocumentRevision,
    KnowledgeBase,
    KnowledgeSnapshotItem,
    RevisionIngestionStatus,
    RevisionLifecycleStatus,
)
from packages.knowledge.parser import ParsedBlock, ParsedDocument
from packages.knowledge.retrieval import HybridKnowledgeRetriever
from packages.knowledge.snapshots import KnowledgeSnapshotService

ROOT = Path(__file__).resolve().parents[2]
DATABASE = "agenthub_rag_scifact_20261021"
COLLECTION = "rag_fiqa_20261022"
SEED = 20261027
TEST_QUERY_COUNT = 648
INSERT_BATCH_SIZE = 200
INDEX_BATCH_SIZE = 128
CHUNK_SIZE_CHARS = 1200
CHUNK_OVERLAP_CHARS = 200
STATE_SCHEMA_VERSION = 1
FROZEN_DEV_SELECTION = (
    "benchmarks/public_rag/results/onyx-selection-off-20261022/frozen-selection.json"
)
PREPARE_SENTINEL_SUFFIX = ".prepare-in-progress.json"


@dataclass(frozen=True)
class SeededCorpus:
    user_id: UUID
    organization_id: UUID
    workspace_id: UUID
    knowledge_base_id: UUID
    snapshot_id: UUID | None
    snapshot_hash: str | None
    documents: int
    empty_documents: int
    chunks: int
    chunk_sources: dict[str, str]


def configuration():
    """Use the SciFact controls but replace only the Qdrant collection."""

    return scifact_settings().model_copy(
        update={"knowledge_qdrant_collection": "rag_fiqa_20261022"}
    )


def retrieval_identity(config) -> dict[str, object]:
    return {
        "dense_model": config.knowledge_embedding_model,
        "dense_dimension": config.knowledge_dense_vector_size,
        "dense_normalized": True,
        "sparse": "BGE-M3 tokenizer log-TF with Qdrant IDF",
        "reranker_model": config.knowledge_reranker_model,
        "reranker_precision": "fp16",
        "embedding_batch_size": config.knowledge_embedding_batch_size,
        "reranker_batch_size": config.knowledge_reranker_batch_size,
        "chunk_size_chars": CHUNK_SIZE_CHARS,
        "chunk_overlap_chars": CHUNK_OVERLAP_CHARS,
        "text_policy": "stripped FiQA title + two newlines + stripped text",
        "dense_top_k": 30,
        "sparse_top_k": 30,
        "candidate_top_k": 20,
        "rerank_top_k": 20,
        "final_top_k": 20,
        "rrf_k": 60,
        "max_chunks_per_document": None,
        "min_rerank_score": None,
        "superseded_rank_penalty": 0.0,
        "qdrant_url": config.qdrant_url,
        "collection": config.knowledge_qdrant_collection,
    }


def atomic_dump(path: Path, value: object) -> None:
    """Durably replace one JSON file without exposing a partial document."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _seed_sentinel_state() -> dict[str, object]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "dataset": "fiqa",
        "database": DATABASE,
        "collection": COLLECTION,
        "seed": SEED,
        "document_id_namespace": str(uuid4()),
        "revision_id_namespace": str(uuid4()),
        "operation_id": str(uuid4()),
        "started_at": datetime.now(UTC).isoformat(),
    }


def _prepare_sentinel_path(state: Path) -> Path:
    return state.with_name(state.name + PREPARE_SENTINEL_SUFFIX)


def ensure_fresh_collection(config) -> None:
    """Refuse a fresh seed unless its isolated vector collection is absent."""

    client = QdrantClient(
        url=config.qdrant_url,
        timeout=int(config.knowledge_qdrant_timeout_seconds),
    )
    try:
        if client.collection_exists(config.knowledge_qdrant_collection):
            raise ValueError(
                f"fresh FiQA prepare requires absent collection {COLLECTION}"
            )
    except ValueError:
        raise
    except Exception as exc:
        raise RuntimeError("could not verify FiQA collection absence") from exc
    finally:
        close = getattr(client, "close", None)
        if close is not None:
            close()


def build_parsed_document(doc: SourceDocument) -> ParsedDocument:
    """Apply the frozen title/text policy before production chunking."""

    return ParsedDocument((ParsedBlock(doc.retrieval_text, {"type": "text_range"}),))


def chunk_document(
    revision_id: UUID,
    doc: SourceDocument,
) -> tuple[Any, ...]:
    return build_deterministic_chunks(
        revision_id,
        build_parsed_document(doc),
        chunk_size_chars=CHUNK_SIZE_CHARS,
        chunk_overlap_chars=CHUNK_OVERLAP_CHARS,
    )


def _document_metadata(doc: SourceDocument, revision_id: UUID) -> dict[str, object]:
    return {
        "document_name": f"fiqa:{doc.document_id}",
        "original_filename": f"{doc.document_id}.txt",
        "blob_key": f"benchmark/fiqa/{revision_id}",
        "file_size": len(doc.retrieval_text.encode("utf-8")),
    }


def _seed_document_ids(
    upstream_document_id: str,
    namespaces: tuple[UUID, UUID],
) -> tuple[UUID, UUID]:
    document_namespace, revision_namespace = namespaces
    if document_namespace == revision_namespace:
        raise ValueError("FiQA seed ID namespaces must be distinct")
    document_id = uuid5(document_namespace, f"document:{upstream_document_id}")
    revision_id = uuid5(
        revision_namespace,
        f"revision:{upstream_document_id}:{document_id}",
    )
    return document_id, revision_id


def _seed_identity_ids(
    namespaces: tuple[UUID, UUID],
) -> tuple[UUID, UUID, UUID, UUID]:
    document_namespace, revision_namespace = namespaces
    if document_namespace == revision_namespace:
        raise ValueError("FiQA seed ID namespaces must be distinct")

    def seeded(name: str) -> UUID:
        return uuid5(document_namespace, f"identity:{name}")

    return seeded("user"), seeded("organization"), seeded("workspace"), seeded("knowledge-base")


def _snapshot_state(seeded: SeededCorpus, config) -> dict[str, object]:
    identity = retrieval_identity(config)
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "dataset": "fiqa",
        "database": DATABASE,
        "collection": COLLECTION,
        "created_at": datetime.now(UTC).isoformat(),
        "user_id": str(seeded.user_id),
        "organization_id": str(seeded.organization_id),
        "workspace_id": str(seeded.workspace_id),
        "knowledge_base_id": str(seeded.knowledge_base_id),
        "kb": {"id": str(seeded.knowledge_base_id)},
        "snapshot_id": str(seeded.snapshot_id),
        "snapshot_hash": seeded.snapshot_hash,
        "documents": seeded.documents,
        "indexed_documents": seeded.documents - seeded.empty_documents,
        "empty_documents": seeded.empty_documents,
        "chunks": seeded.chunks,
        "chunk_sources": seeded.chunk_sources,
        "text_policy": identity["text_policy"],
        "retrieval_identity": identity,
        "index_status": "PENDING",
    }


def validate_state(state: dict[str, object], config=None) -> None:
    """Reject inherited stores, malformed IDs, and non-frozen controls."""

    expected = retrieval_identity(config or configuration())
    required = {
        "schema_version": STATE_SCHEMA_VERSION,
        "dataset": "fiqa",
        "database": DATABASE,
        "collection": COLLECTION,
        "retrieval_identity": expected,
        "text_policy": expected["text_policy"],
    }
    for key, value in required.items():
        if state.get(key) != value:
            raise ValueError(f"seed state {key} differs from FiQA baseline")
    for key in ("user_id", "organization_id", "workspace_id", "knowledge_base_id", "snapshot_id"):
        UUID(str(state[key]))
    if UUID(str(state["kb"]["id"])) != UUID(str(state["knowledge_base_id"])):
        raise ValueError("seed state kb and knowledge_base_id differ")
    for key in ("documents", "indexed_documents", "empty_documents", "chunks"):
        if type(state.get(key)) is not int or int(state[key]) < 0:
            raise ValueError(f"seed state {key} must be a non-negative integer")
    if (
        int(state["indexed_documents"]) + int(state["empty_documents"])
        != int(state["documents"])
    ):
        raise ValueError("seed-state document accounting is inconsistent")
    snapshot_hash = state.get("snapshot_hash")
    if not isinstance(snapshot_hash, str) or len(snapshot_hash) != 64:
        raise ValueError("seed state snapshot_hash must be a SHA-256 digest")
    try:
        bytes.fromhex(snapshot_hash)
    except ValueError:
        raise ValueError("seed state snapshot_hash must be a SHA-256 digest") from None
    if not isinstance(state["chunk_sources"], dict):
        raise ValueError("seed state chunk_sources must be a mapping")
    chunk_sources = state["chunk_sources"]
    if any(
        not isinstance(chunk_id, str)
        or not chunk_id
        or not isinstance(document_id, str)
        or not document_id
        for chunk_id, document_id in chunk_sources.items()
    ):
        raise ValueError("seed state chunk_sources must contain non-empty string IDs")
    if len(chunk_sources) != int(state["chunks"]):
        raise ValueError("seed state chunk count differs from chunk_sources")
    if len(set(chunk_sources.values())) != int(state["indexed_documents"]):
        raise ValueError("seed state indexed document count differs from chunk_sources")


def _read_state(path: Path, config) -> dict[str, object]:
    state = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(state, dict):
        raise ValueError("seed state must be a JSON object")
    validate_state(state, config)
    return state


def _baseline_paths(output: Path) -> tuple[Path, Path, Path, Path]:
    return (
        output / "baseline.jsonl",
        output / "baseline.meta.json",
        output / "measured-source.zip",
        output / "test-exposures.jsonl",
    )


def _baseline_controls() -> dict[str, object]:
    return {
        "dense_model": "BAAI/bge-m3",
        "sparse": "tokenizer log-TF with Qdrant IDF",
        "reranker_model": "BAAI/bge-reranker-v2-m3",
        "dense_top_k": 30,
        "sparse_top_k": 30,
        "rrf_k": 60,
        "candidate_top_k": 20,
        "rerank_top_k": 20,
        "final_top_k": 20,
        "chunk_size_chars": CHUNK_SIZE_CHARS,
        "chunk_overlap_chars": CHUNK_OVERLAP_CHARS,
        "batch_size": 8,
    }


def _protocol(
    path: Path,
    *,
    snapshot_id: object | None,
) -> dict[str, object]:
    protocol = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(protocol, dict):
        raise ValueError("protocol must be a JSON object")
    expected = {
        "dataset_id": "fiqa",
        "split": "test",
        "expected_queries": TEST_QUERY_COUNT,
        "evaluation_role": "independent_confirmation",
        "seed": SEED,
        "database": DATABASE,
        "collection": COLLECTION,
        "frozen_development_selection": FROZEN_DEV_SELECTION,
        "baseline_controls": _baseline_controls(),
    }
    if snapshot_id is not None:
        expected["snapshot_id"] = snapshot_id
    for key, value in expected.items():
        if protocol.get(key) != value:
            raise ValueError(f"protocol {key} is not bound to the frozen FiQA baseline")
    return protocol


def _exact_protocol(path: Path, state: dict[str, object]) -> dict[str, object]:
    return _protocol(path, snapshot_id=state["snapshot_id"])


def _git_provenance() -> dict[str, object]:
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], text=True))
    files: list[str] = []
    for revision in (commit, "HEAD"):
        try:
            files = subprocess.check_output(
                ["git", "ls-tree", "-r", "--name-only", revision, "--", "benchmarks/public_rag"],
                text=True,
            ).splitlines()
        except subprocess.CalledProcessError:
            continue
        if files:
            break
    return {"commit": commit, "dirty": dirty, "source_files": _measured_sources(files)}


def _measured_sources(committed: list[str]) -> list[str]:
    required = {
        "benchmarks/public_rag/fiqa_baseline.py",
        "benchmarks/public_rag/data.py",
        "benchmarks/public_rag/scifact_runner.py",
        "packages/knowledge/retrieval.py",
        "packages/knowledge/composition.py",
        "packages/knowledge/contracts.py",
        "packages/knowledge/models.py",
        "packages/knowledge/chunking.py",
        "packages/knowledge/parser.py",
        "packages/knowledge/snapshots.py",
        "packages/knowledge/adapters/qdrant.py",
        "packages/knowledge/adapters/embeddings.py",
        "packages/knowledge/adapters/sparse.py",
        "packages/knowledge/adapters/reranker.py",
        "apps/worker/tasks/knowledge.py",
    }
    return sorted(required.union(committed))


def _write_measured_source(path: Path, provenance: dict[str, object]) -> None:
    """Create the main-owned source archive before any query is opened."""

    sources = provenance["source_files"]
    assert isinstance(sources, list)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative in sources:
            source = ROOT / relative
            if source.is_file():
                archive.write(source, relative)
        archive.writestr(
            "provenance.json",
            json.dumps(provenance, ensure_ascii=False, indent=2) + "\n",
        )


async def _authorized_context(session, user_id: str, workspace_id: str):
    access = await TenantService().get_workspace_access(
        session,
        principal=PrincipalContext(
            request_id="fiqa-baseline",
            trace_id="fiqa-baseline",
            user_id=user_id,
        ),
        workspace_id=UUID(workspace_id),
    )
    return access.context


async def _flush_rows(session, model, rows: list[dict[str, object]]) -> None:
    if rows:
        await session.execute(insert(model), rows)
        rows.clear()
    await session.flush()


async def _seed_corpus(
    session,
    corpus: dict[str, SourceDocument],
    namespaces: tuple[UUID, UUID],
) -> SeededCorpus:
    user_id, organization_id, workspace_id, knowledge_base_id = _seed_identity_ids(namespaces)
    existing = await session.scalar(
        select(KnowledgeBase.id).where(KnowledgeBase.id == knowledge_base_id)
    )
    if existing is not None:
        recovered = await _recover_seeded_corpus(
            session,
            corpus,
            namespaces,
            (
                user_id,
                organization_id,
                workspace_id,
                knowledge_base_id,
            ),
        )
        if recovered is None:
            raise ValueError("persisted FiQA knowledge base is incomplete")
        return recovered

    user = User(
        id=user_id,
        email=(email := f"fiqa-{user_id}@benchmark.invalid"),
        normalized_email=email,
        password_hash="benchmark-only",
    )
    session.add(user)
    await session.flush()
    organization = Organization(id=organization_id, name="FiQA isolated", created_by=user.id)
    session.add(organization)
    await session.flush()
    session.add(
        OrganizationMembership(organization_id=organization.id, user_id=user.id, role="OWNER")
    )
    workspace = Workspace(id=workspace_id, organization_id=organization.id, name="FiQA")
    session.add(workspace)
    await session.flush()
    kb = KnowledgeBase(
        id=knowledge_base_id,
        workspace_id=workspace.id,
        name="FiQA full corpus",
    )
    session.add(kb)
    await session.flush()

    documents: list[dict[str, object]] = []
    revisions: list[dict[str, object]] = []
    chunks: list[dict[str, object]] = []
    chunk_sources: dict[str, str] = {}
    empty_documents = 0
    for doc in corpus.values():
        document_id, revision_id = _seed_document_ids(doc.document_id, namespaces)
        metadata = _document_metadata(doc, revision_id)
        documents.append(
            {
                "id": document_id,
                "workspace_id": workspace.id,
                "knowledge_base_id": kb.id,
                "name": metadata["document_name"],
            }
        )
        revisions.append(
            {
                "id": revision_id,
                "workspace_id": workspace.id,
                "knowledge_base_id": kb.id,
                "document_id": document_id,
                "revision_number": 1,
                "original_filename": metadata["original_filename"],
                "blob_key": metadata["blob_key"],
                "media_type": "text/plain",
                "file_size": metadata["file_size"],
                "ingestion_status": RevisionIngestionStatus.READY,
                "lifecycle_status": RevisionLifecycleStatus.ACTIVE,
            }
        )
        candidates = chunk_document(revision_id, doc)
        empty_documents += not candidates
        for candidate in candidates:
            chunk_sources[candidate.chunk_id] = doc.document_id
            chunks.append(
                {
                    "id": uuid4(),
                    "chunk_id": candidate.chunk_id,
                    "workspace_id": workspace.id,
                    "knowledge_base_id": kb.id,
                    "document_id": document_id,
                    "document_revision_id": revision_id,
                    "ordinal": candidate.ordinal,
                    "normalized_content_hash": candidate.normalized_content_hash,
                    "text": candidate.text,
                    "locator": candidate.locator,
                }
            )
        if max(len(documents), len(revisions), len(chunks)) >= INSERT_BATCH_SIZE:
            await _flush_rows(session, Document, documents)
            await _flush_rows(session, DocumentRevision, revisions)
            await _flush_rows(session, DocumentChunk, chunks)
    await _flush_rows(session, Document, documents)
    await _flush_rows(session, DocumentRevision, revisions)
    await _flush_rows(session, DocumentChunk, chunks)
    await session.commit()

    context = await _authorized_context(session, str(user.id), str(workspace.id))
    snapshot = await KnowledgeSnapshotService().create_current_snapshot(session, context, kb.id)
    return SeededCorpus(
        user_id=user.id,
        organization_id=organization.id,
        workspace_id=workspace.id,
        knowledge_base_id=kb.id,
        snapshot_id=snapshot.snapshot_id,
        snapshot_hash=snapshot.content_hash,
        documents=len(corpus),
        empty_documents=empty_documents,
        chunks=len(chunk_sources),
        chunk_sources=chunk_sources,
    )


async def _recover_seeded_corpus(
    session,
    corpus: dict[str, SourceDocument],
    namespaces: tuple[UUID, UUID],
    ids: tuple[UUID, UUID, UUID, UUID],
) -> SeededCorpus | None:
    """Adopt a corpus committed by an interrupted prepare without reinserting it."""

    user_id, organization_id, workspace_id, knowledge_base_id = ids
    user = await session.get(User, user_id)
    organization = await session.get(Organization, organization_id)
    workspace = await session.get(Workspace, workspace_id)
    kb = await session.get(KnowledgeBase, knowledge_base_id)
    if any(item is None for item in (user, organization, workspace, kb)):
        return None
    if (
        organization.created_by != user.id
        or workspace.organization_id != organization.id
        or kb.workspace_id != workspace.id
    ):
        raise ValueError("persisted FiQA tenant scope differs from frozen identities")
    membership = await session.get(OrganizationMembership, (organization.id, user.id))
    if membership is None or membership.role != "OWNER":
        raise ValueError("persisted FiQA organization owner is missing")

    documents = list(
        await session.scalars(
            select(Document).where(
                Document.workspace_id == workspace.id,
                Document.knowledge_base_id == kb.id,
            )
        )
    )
    if not documents:
        raise ValueError("persisted FiQA knowledge base is incomplete")
    if len(documents) != len(corpus):
        raise ValueError("persisted FiQA document count differs from corpus")
    upstream_by_document_id: dict[UUID, str] = {}
    for upstream_document_id in corpus:
        document_id, _ = _seed_document_ids(upstream_document_id, namespaces)
        upstream_by_document_id[document_id] = upstream_document_id
    if {document.id for document in documents} != set(upstream_by_document_id):
        raise ValueError("persisted FiQA document identities differ from corpus")

    revisions = list(
        await session.scalars(
            select(DocumentRevision).where(
                DocumentRevision.workspace_id == workspace.id,
                DocumentRevision.knowledge_base_id == kb.id,
                DocumentRevision.ingestion_status == RevisionIngestionStatus.READY,
                DocumentRevision.lifecycle_status == RevisionLifecycleStatus.ACTIVE,
            )
        )
    )
    revision_by_document: dict[UUID, DocumentRevision] = {}
    for revision in revisions:
        upstream_document_id = upstream_by_document_id.get(revision.document_id)
        expected_document_id, expected_revision_id = _seed_document_ids(
            str(upstream_document_id),
            namespaces,
        )
        if (
            upstream_document_id is None
            or revision.document_id != expected_document_id
            or revision.id != expected_revision_id
            or revision.document_id in revision_by_document
        ):
            raise ValueError("persisted FiQA revision identities differ from corpus")
        revision_by_document[revision.document_id] = revision
    if len(revision_by_document) != len(corpus):
        raise ValueError("persisted FiQA current revision count differs from corpus")

    chunks = list(
        await session.scalars(
            select(DocumentChunk).where(
                DocumentChunk.workspace_id == workspace.id,
                DocumentChunk.knowledge_base_id == kb.id,
            )
        )
    )
    chunk_sources: dict[str, str] = {}
    indexed_document_ids: set[UUID] = set()
    for chunk in chunks:
        upstream_document_id = upstream_by_document_id.get(chunk.document_id)
        revision = revision_by_document.get(chunk.document_id)
        if (
            upstream_document_id is None
            or revision is None
            or chunk.document_revision_id != revision.id
            or chunk.chunk_id in chunk_sources
        ):
            raise ValueError("persisted FiQA chunk scope differs from frozen corpus")
        chunk_sources[chunk.chunk_id] = upstream_document_id
        indexed_document_ids.add(chunk.document_id)

    context = await _authorized_context(session, str(user.id), str(workspace.id))
    snapshot = await KnowledgeSnapshotService().create_current_snapshot(session, context, kb.id)
    return SeededCorpus(
        user_id=user.id,
        organization_id=organization.id,
        workspace_id=workspace.id,
        knowledge_base_id=kb.id,
        snapshot_id=snapshot.snapshot_id,
        snapshot_hash=snapshot.content_hash,
        documents=len(documents),
        empty_documents=len(documents) - len(indexed_document_ids),
        chunks=len(chunks),
        chunk_sources=chunk_sources,
    )


async def _resolve_seeded_corpus(
    session,
    state: dict[str, object],
) -> SeededCorpus:
    """Validate a committed state without loading or creating corpus rows."""

    validate_state(state)
    context = await _authorized_context(session, str(state["user_id"]), str(state["workspace_id"]))
    snapshot = await KnowledgeSnapshotService().resolve_snapshot(
        session,
        context,
        UUID(str(state["knowledge_base_id"])),
        str(state["snapshot_id"]),
    )
    if snapshot.content_hash != state["snapshot_hash"]:
        raise ValueError("persisted snapshot hash differs from seed state")
    if snapshot.item_count != state["documents"]:
        raise ValueError("persisted snapshot document count differs from seed state")
    if snapshot.workspace_id != UUID(str(state["workspace_id"])):
        raise ValueError("persisted snapshot workspace differs from seed state")
    if snapshot.knowledge_base_id != UUID(str(state["knowledge_base_id"])):
        raise ValueError("persisted snapshot knowledge base differs from seed state")
    return SeededCorpus(
        user_id=UUID(str(state["user_id"])),
        organization_id=UUID(str(state["organization_id"])),
        workspace_id=UUID(str(state["workspace_id"])),
        knowledge_base_id=UUID(str(state["knowledge_base_id"])),
        snapshot_id=snapshot.snapshot_id,
        snapshot_hash=snapshot.content_hash,
        documents=int(state["documents"]),
        empty_documents=int(state["empty_documents"]),
        chunks=int(state["chunks"]),
        chunk_sources=dict(state["chunk_sources"]),
    )


async def _seed_inventory(session, state: dict[str, object]) -> tuple[int, int]:
    workspace_id = UUID(str(state["workspace_id"]))
    knowledge_base_id = UUID(str(state["knowledge_base_id"]))
    snapshot_id = UUID(str(state["snapshot_id"]))
    chunk_count = await session.scalar(
        select(func.count())
        .select_from(DocumentChunk)
        .where(
            DocumentChunk.workspace_id == workspace_id,
            DocumentChunk.knowledge_base_id == knowledge_base_id,
        )
    )
    indexed_documents = await session.scalar(
        select(func.count(func.distinct(DocumentChunk.document_revision_id)))
        .select_from(DocumentChunk)
        .join(
            KnowledgeSnapshotItem,
            (KnowledgeSnapshotItem.workspace_id == DocumentChunk.workspace_id)
            & (KnowledgeSnapshotItem.knowledge_base_id == DocumentChunk.knowledge_base_id)
            & (KnowledgeSnapshotItem.document_id == DocumentChunk.document_id)
            & (
                KnowledgeSnapshotItem.document_revision_id
                == DocumentChunk.document_revision_id
            ),
        )
        .where(
            DocumentChunk.workspace_id == workspace_id,
            DocumentChunk.knowledge_base_id == knowledge_base_id,
            KnowledgeSnapshotItem.snapshot_id == snapshot_id,
        )
    )
    if chunk_count != state["chunks"]:
        raise ValueError("persisted chunk count differs from seed state")
    if indexed_documents != int(state["indexed_documents"]):
        raise ValueError("persisted indexed document count differs from seed state")
    return int(chunk_count), int(indexed_documents)


async def _seed_chunk_batches(
    session,
    state: dict[str, object],
) -> AsyncIterator[list[DocumentChunk]]:
    offset = 0
    while offset < int(state["chunks"]):
        result = await session.scalars(
            select(DocumentChunk)
            .join(
                KnowledgeSnapshotItem,
                (KnowledgeSnapshotItem.workspace_id == DocumentChunk.workspace_id)
                & (
                    KnowledgeSnapshotItem.knowledge_base_id
                    == DocumentChunk.knowledge_base_id
                )
                & (KnowledgeSnapshotItem.document_id == DocumentChunk.document_id)
                & (
                    KnowledgeSnapshotItem.document_revision_id
                    == DocumentChunk.document_revision_id
                ),
            )
            .where(
                DocumentChunk.workspace_id == UUID(str(state["workspace_id"])),
                DocumentChunk.knowledge_base_id
                == UUID(str(state["knowledge_base_id"])),
                KnowledgeSnapshotItem.snapshot_id == UUID(str(state["snapshot_id"])),
            )
            .order_by(
                DocumentChunk.document_revision_id,
                DocumentChunk.ordinal,
                DocumentChunk.id,
            )
            .offset(offset)
            .limit(INDEX_BATCH_SIZE)
        )
        batch = list(result)
        if not batch:
            raise ValueError("persisted chunk batches ended before seed-state count")
        yield batch
        offset += len(batch)
    if offset != int(state["chunks"]):
        raise ValueError("persisted chunk batches exceed seed-state count")


async def _index_seed(args, state: dict[str, object], *, finalize: bool) -> None:
    config = configuration()
    engine, factory = create_database(config.database_url)
    components = None
    try:
        async with factory() as session:
            seeded = await _resolve_seeded_corpus(session, state)
            chunk_count, indexed_documents = await _seed_inventory(session, state)
        offline_models()
        components = production_retrieval_components(config)
        load_ms: dict[str, float] = {}
        for name in ("dense", "sparse"):
            component = getattr(components, name)
            started = time.perf_counter()
            await asyncio.to_thread(component.warm)
            load_ms[name] = (time.perf_counter() - started) * 1000
        started = time.perf_counter()
        await asyncio.to_thread(components.index.ensure_collection)
        claim = SimpleNamespace(
            workspace_id=seeded.workspace_id,
            knowledge_base_id=seeded.knowledge_base_id,
        )
        indexed = 0
        async with factory() as session:
            async for batch in _seed_chunk_batches(session, state):
                for chunk in batch:
                    if state["chunk_sources"].get(chunk.chunk_id) is None:
                        raise ValueError("persisted chunks differ from seed-state mapping")
                records = await asyncio.to_thread(
                    _vector_records,
                    claim,
                    batch,
                    components.dense,
                    components.sparse,
                )
                await asyncio.to_thread(components.index.upsert, records)
                indexed += len(batch)
                print(f"indexed {indexed}/{chunk_count}", flush=True)
        if indexed != chunk_count:
            raise ValueError("indexed chunks differ from seed-state count")
        if finalize:
            state = dict(state)
            state.update(
                {
                    "index_status": "COMPLETE",
                    "indexed_documents": indexed_documents,
                    "indexed_chunks": indexed,
                    "index_model_load_ms": load_ms,
                    "index_ms": (time.perf_counter() - started) * 1000,
                    "completed_at": datetime.now(UTC).isoformat(),
                }
            )
            atomic_dump(args.state, state)
    finally:
        if components is not None:
            components.index.close()
        await engine.dispose()


async def prepare(args) -> None:
    if args.state.exists():
        state = _read_state(args.state, configuration())
        if state.get("index_status") == "COMPLETE":
            raise ValueError("refusing to resume an already complete final seed state")
        print(
            "resuming committed corpus "
            f"documents={state['documents']} chunks={state['chunks']}",
            flush=True,
        )
        await _index_seed(args, state, finalize=True)
        return

    config = configuration()
    if args.protocol is not None:
        _protocol(args.protocol, snapshot_id=None)
    ensure_fresh_collection(config)
    sentinel_path = _prepare_sentinel_path(args.state)
    if sentinel_path.exists():
        sentinel = json.loads(sentinel_path.read_text(encoding="utf-8"))
        if (
            not isinstance(sentinel, dict)
            or sentinel.get("dataset") != "fiqa"
            or sentinel.get("database") != DATABASE
            or sentinel.get("collection") != COLLECTION
            or sentinel.get("seed") != SEED
        ):
            raise ValueError("prepare sentinel does not belong to the FiQA baseline")
        namespaces = (
            UUID(str(sentinel["document_id_namespace"])),
            UUID(str(sentinel["revision_id_namespace"])),
        )
        if namespaces[0] == namespaces[1]:
            raise ValueError("FiQA seed ID namespaces must be distinct")
    else:
        sentinel = _seed_sentinel_state()
        atomic_dump(sentinel_path, sentinel)
        namespaces = (
            UUID(str(sentinel["document_id_namespace"])),
            UUID(str(sentinel["revision_id_namespace"])),
        )
    corpus = _load_corpus(args.data / "corpus.jsonl")
    engine, factory = create_database(config.database_url)
    try:
        async with factory() as session:
            seeded = await _seed_corpus(session, corpus, namespaces)
            state = _snapshot_state(seeded, config)
            indexed_document_ids = {
                str(_seed_document_ids(upstream, namespaces)[0])
                for upstream in seeded.chunk_sources.values()
            }
            if len(indexed_document_ids) != seeded.documents - seeded.empty_documents:
                raise ValueError("FiQA seed document identity accounting is inconsistent")
        print(
            f"seeded documents={seeded.documents} chunks={seeded.chunks} "
            f"indexed_documents={seeded.documents - seeded.empty_documents} "
            f"empty_documents={seeded.empty_documents}",
            flush=True,
        )
    finally:
        await engine.dispose()
    atomic_dump(args.state, state)
    sentinel_path.unlink()
    await _index_seed(args, state, finalize=True)


def _trace_stages(result) -> dict[str, dict[str, object]]:
    stages: dict[str, dict[str, object]] = {}
    for name in ("dense", "sparse", "fusion", "selection", "rerank"):
        stage = getattr(result.trace, name)
        if stage is None:
            stages[name] = {"latency_ms": 0.0, "ranking": []}
            continue
        stages[name] = {
            "latency_ms": stage.latency_ms,
            "ranking": [
                {"chunk_id": hit.chunk_id, "score": hit.score} for hit in stage.results
            ],
        }
    return stages


def _baseline_row(
    query_id: str,
    ordinal: int,
    result,
    total_ms: float,
    chunk_sources: dict[str, str],
) -> dict[str, object]:
    try:
        stages = _trace_stages(result)
        for stage in stages.values():
            for hit in stage["ranking"]:
                hit["document_id"] = chunk_sources[hit["chunk_id"]]
    except KeyError as exc:
        raise ValueError(f"retrieved chunk lacks a source document: {exc.args[0]}") from None
    return {
        "query_id": query_id,
        "ordinal": ordinal,
        "stages": stages,
        "total_ms": total_ms,
    }


async def baseline(args) -> None:
    """Load FiQA test only after protocol validation and exposure logging."""

    if not args.protocol.is_file():
        raise ValueError("baseline requires an explicit frozen protocol file")
    state = _read_state(args.state, configuration())
    if state.get("index_status") != "COMPLETE":
        raise ValueError("prepare has not completed vector indexing")
    output_path, meta_path, source_path, exposure = _baseline_paths(args.output)
    if any(path.exists() for path in (output_path, meta_path, source_path)):
        raise ValueError("refusing to overwrite a historical FiQA baseline")
    protocol = _exact_protocol(args.protocol, state)
    seed = int(protocol["seed"])

    args.output.mkdir(parents=True, exist_ok=True)
    provenance = _git_provenance()
    _write_measured_source(source_path, provenance)
    with exposure.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "event": "INDEPENDENT_TEST_LABEL_ACCESS",
                    "at": datetime.now(UTC).isoformat(),
                    "dataset": "fiqa",
                    "split": "test",
                    "protocol": protocol,
                    "state_snapshot_id": state["snapshot_id"],
                    "arm": "baseline",
                }
            )
            + "\n"
        )
        stream.flush()
        os.fsync(stream.fileno())

    source = load_beir(args.data, "test")
    query_ids = sorted(source.queries)
    if len(query_ids) != TEST_QUERY_COUNT:
        raise ValueError(f"FiQA test must contain exactly {TEST_QUERY_COUNT} labeled queries")
    random.Random(seed).shuffle(query_ids)

    config = configuration()
    identity = retrieval_identity(config)
    engine, factory = create_database(config.database_url)
    components = None
    retriever = None
    try:
        async with factory() as session:
            context = await _authorized_context(
                session, str(state["user_id"]), str(state["workspace_id"])
            )
            snapshot = await KnowledgeSnapshotService().resolve_snapshot(
                session,
                context,
                UUID(str(state["knowledge_base_id"])),
                str(state["snapshot_id"]),
            )
            if snapshot.content_hash != state["snapshot_hash"]:
                raise ValueError("persisted snapshot differs from frozen seed state")
            if snapshot.item_count != state["documents"]:
                raise ValueError("persisted snapshot membership differs from frozen seed state")
        offline_models()
        components = production_retrieval_components(config)
        load_ms = {}
        for name, component in (
            ("dense", components.dense),
            ("sparse", components.sparse),
            ("reranker", components.reranker),
        ):
            started = time.perf_counter()
            await asyncio.to_thread(component.warm)
            load_ms[name] = (time.perf_counter() - started) * 1000
        retriever = HybridKnowledgeRetriever(
            session_factory=factory,
            dense_embedder=components.dense,
            sparse_encoder=components.sparse,
            reranker=components.reranker,
            vector_index=components.index,
            rrf_k=60,
            min_rerank_score=None,
            superseded_rank_penalty=0.0,
        )
        atomic_dump(
            meta_path,
            {
                "schema_version": 1,
                "benchmark": "fiqa-production-retrieval-baseline",
                "dataset": "fiqa",
                "split": "test",
                "protocol": protocol,
                "queries": len(query_ids),
                "seed": seed,
                "database": DATABASE,
                "collection": COLLECTION,
                "dataset_id": "fiqa",
                "user_id": state["user_id"],
                "organization_id": state["organization_id"],
                "workspace_id": state["workspace_id"],
                "knowledge_base_id": state["knowledge_base_id"],
                "kb": state["kb"],
                "snapshot_id": state["snapshot_id"],
                "snapshot_hash": state["snapshot_hash"],
                "documents": state["documents"],
                "indexed_documents": state["indexed_documents"],
                "empty_documents": state["empty_documents"],
                "chunks": state["chunks"],
                "retrieval_identity": identity,
                "arm": "baseline",
                "git_commit": provenance["commit"],
                "git_dirty": provenance["dirty"],
                "source_zip": "measured-source.zip",
                "cold_load_ms": load_ms,
                "scope": "retrieval-only; no LLM generation, answer quality, or API usage",
            },
        )
        with output_path.open("x", encoding="utf-8", newline="\n") as output:
            for ordinal, query_id in enumerate(query_ids):
                started = time.perf_counter()
                result = await retriever.retrieve_with_trace(
                    context,
                    RetrievalQuery(
                        text=source.queries[query_id],
                        knowledge_base_id=str(state["knowledge_base_id"]),
                        knowledge_snapshot_id=str(state["snapshot_id"]),
                        strategy=RetrievalStrategy.HYBRID_RERANK,
                        dense_top_k=30,
                        sparse_top_k=30,
                        candidate_top_k=20,
                        final_top_k=20,
                        rerank_top_k=20,
                        max_chunks_per_document=None,
                    ),
                )
                total_ms = (time.perf_counter() - started) * 1000
                output.write(
                    json.dumps(
                        _baseline_row(
                            query_id,
                            ordinal,
                            result,
                            total_ms,
                            state["chunk_sources"],
                        ),
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                    + "\n"
                )
                output.flush()
                print(
                    f"query {ordinal + 1}/{len(query_ids)} id={query_id} ms={total_ms:.1f}",
                    flush=True,
                )
    finally:
        if components is not None:
            components.index.close()
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "baseline"))
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--protocol", type=Path)
    args = parser.parse_args()
    if args.action == "baseline" and args.protocol is None:
        parser.error("baseline requires --protocol")
    offline_models()
    asyncio.run(prepare(args) if args.action == "prepare" else baseline(args))


if __name__ == "__main__":
    main()
