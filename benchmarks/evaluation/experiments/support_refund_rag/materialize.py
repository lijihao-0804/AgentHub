"""Bounded zero-generation S1 materialization for the support-refund DEV corpus.

This entry point is deliberately narrower than a complete Agent Run. It performs:

* an explicit ``alembic upgrade head`` and LangGraph checkpoint bootstrap in a
  fixed, experiment-owned PostgreSQL database;
* creation of a fresh organization/workspace and one real knowledge base;
* real ``KnowledgeService.upload_document`` calls followed by the production
  ingestion worker path, CUDA embedding, Qdrant indexing and snapshot creation;
* lifecycle writes through ``KnowledgeService.update_document_lifecycle``; and
* one direct production retrieval per DEV question, with private candidate and
  final evidence plus post-hoc exact-quote coverage.

It never initializes the model gateway, dispatches a generation request, creates
an AgentVersion, invokes a tool runtime, configures a WRITE tool, reads a holdout,
or uses a seed corpus. Because publication and ContextBudget admission are outside
this slice, its terminal status is ``S1_PARTIAL`` rather than ``PASS``.

Existing experiment resources are resumed only when ``private/s1/state.json``
proves this exact run owns them. Without that state the script refuses rather than
reusing or dropping anything.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections import Counter
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import asyncpg
from qdrant_client import QdrantClient
from sqlalchemy import select
from sqlalchemy import text as sql_text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.worker.tasks.knowledge import _process_knowledge_ingestion
from packages.control_plane.models import (
    Organization,
    OrganizationMembership,
    User,
    Workspace,
    WorkspaceMembership,
)
from packages.core.config.settings import Settings
from packages.core.database import create_database
from packages.core.execution_context.models import (
    OrganizationContext,
    PrincipalContext,
    WorkspaceExecutionContext,
)
from packages.knowledge.blob_store import LocalBlobStore
from packages.knowledge.chunking import normalize_chunk_text
from packages.knowledge.composition import (
    production_retrieval_components,
    retrieval_model_identity_mismatches,
)
from packages.knowledge.contracts import RetrievalQuery, RetrievalStrategy
from packages.knowledge.models import (
    Document,
    DocumentChunk,
    DocumentRevision,
    IngestionJob,
    KnowledgeBase,
    KnowledgeSnapshotItem,
)
from packages.knowledge.retrieval import SessionScopedKnowledgeRetriever
from packages.knowledge.services import KnowledgeService
from packages.knowledge.snapshots import KnowledgeSnapshotService

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DOCS = DATA / "docs"
AUDIT_FILE = ROOT / "material-audit.json"
PRIVATE = ROOT / "private" / "s1"
STATE_FILE = PRIVATE / "state.json"
BLOB_ROOT = PRIVATE / "blobs"
LOCK_FILE = PRIVATE / ".lock"

DATABASE_NAME = "agenthub_support_refund_rag_20261026"
COLLECTION_NAME = DATABASE_NAME
ORG_NAME = "support-refund-rag-dev-20261026"
WORKSPACE_NAME = "support-refund-rag-dev-20261026"
KNOWLEDGE_BASE_NAME = "support-refund-rag-policy-dev-20261026"

EXPECTED_DOCUMENTS = 16
EXPECTED_CASES = 50
STATE_SCHEMA_VERSION = 1
STATE_INTERFACE_VERSION = 1
PENDING_S1_ITEMS = [
    "published AgentVersion and immutable resolved_spec binding",
    "ContextBudgetPolicy evidence observation admission",
    "ToolRuntime observation projection and AgentRun",
    "A/B replay and S2/S3 model evaluation",
]

# Frozen for this zero-call S1 slice from the live RetrievalQuery defaults. This
# is not evidence of a published AgentVersion and introduces no strategy or
# budget override.
_RETRIEVAL_QUERY_DEFAULTS = RetrievalQuery(
    text="",
    knowledge_base_id="",
    knowledge_snapshot_id="",
)
RETRIEVAL_CONFIG: dict[str, Any] = {
    "retrieval_strategy": _RETRIEVAL_QUERY_DEFAULTS.strategy.value,
    "dense_top_k": _RETRIEVAL_QUERY_DEFAULTS.dense_top_k,
    "sparse_top_k": _RETRIEVAL_QUERY_DEFAULTS.sparse_top_k,
    "candidate_top_k": _RETRIEVAL_QUERY_DEFAULTS.candidate_top_k,
    "final_top_k": _RETRIEVAL_QUERY_DEFAULTS.final_top_k,
    "rerank_top_k": _RETRIEVAL_QUERY_DEFAULTS.rerank_top_k,
    "max_chunks_per_document": _RETRIEVAL_QUERY_DEFAULTS.max_chunks_per_document,
    "min_rerank_score": _RETRIEVAL_QUERY_DEFAULTS.min_rerank_score,
    "context_budget_bytes": _RETRIEVAL_QUERY_DEFAULTS.context_budget_bytes,
    "evidence_projection": _RETRIEVAL_QUERY_DEFAULTS.evidence_projection,
    "superseded_rank_penalty": 0.0,
}
if int(RETRIEVAL_CONFIG["candidate_top_k"]) != 20 or int(RETRIEVAL_CONFIG["final_top_k"]) != 6:
    raise RuntimeError("live RetrievalQuery defaults changed from candidate=20/final=6")

_SAFE_DATABASE_NAME = re.compile(r"^agenthub_support_refund_rag_20261026$")
_SECRET_IN_URL = re.compile(r"(?i)(postgres(?:ql)?(?:\+[a-z0-9_]+)?://)([^\s:/@]+)(?::[^@\s]*)?@")


class MaterializeError(RuntimeError):
    """A fail-closed condition with a safe, experiment-facing message."""


class _RecordedQueue:
    """The production upload boundary enqueues durably; S1 runs the worker inline."""

    def __init__(self) -> None:
        self.job_ids: list[UUID] = []

    async def enqueue(self, job_id: UUID) -> None:
        self.job_ids.append(job_id)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _json_default(value: object) -> str:
    if isinstance(value, (UUID, Path, datetime, date)):
        return str(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MaterializeError(f"required file is missing: {path.relative_to(ROOT)}") from exc
    except (OSError, ValueError) as exc:
        raise MaterializeError(f"JSON file is unreadable: {path.relative_to(ROOT)}") from exc
    if not isinstance(value, dict):
        raise MaterializeError(f"JSON root must be an object: {path.relative_to(ROOT)}")
    return value


def _atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as target:
            temporary = Path(target.name)
            target.write(value)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _atomic_write_json(path: Path, value: object) -> None:
    _atomic_write_text(
        path,
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
            default=_json_default,
        )
        + "\n",
    )


def _redact_sensitive(text: str, database_url: str) -> str:
    redacted = _SECRET_IN_URL.sub(r"\1***@", text)
    try:
        password = make_url(database_url).password
    except Exception:
        password = None
    if password:
        redacted = redacted.replace(password, "***")
    return redacted


def _safe_error(error: BaseException, database_url: str | None = None) -> dict[str, str]:
    message = str(error)
    if database_url is not None:
        message = _redact_sensitive(message, database_url)
    else:
        message = _SECRET_IN_URL.sub(r"\1***@", message)
    return {
        "type": type(error).__name__,
        "message": message[:2_000],
    }


@contextmanager
def _exclusive_lock() -> Iterator[None]:
    """Hold an OS lock so two materializers cannot interleave writes."""

    PRIVATE.mkdir(parents=True, exist_ok=True)
    lock = LOCK_FILE.open("a+b")
    if lock.tell() == 0:
        lock.write(b"1")
        lock.flush()
    lock.seek(0)
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise MaterializeError("another S1 materialization process holds the lock") from exc
        else:
            import fcntl

            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise MaterializeError("another S1 materialization process holds the lock") from exc
        locked = True
        yield
    finally:
        if locked:
            lock.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def _require_material_audit() -> tuple[dict[str, Any], dict[str, Any]]:
    """Refuse any real-resource work unless the offline material audit passed."""

    audit = _read_json(AUDIT_FILE)
    if audit.get("status") != "PASS" or audit.get("errors"):
        raise MaterializeError("material-audit.json must have status PASS and no errors")

    manifest = _read_json(DATA / "manifest.json")
    cases = _read_json(DATA / "cases.json")
    if manifest.get("fictional") is not True or cases.get("fictional") is not True:
        raise MaterializeError("the S1 corpus and case set must be explicitly fictional")

    documents = manifest.get("documents")
    if not isinstance(documents, list) or len(documents) != EXPECTED_DOCUMENTS:
        raise MaterializeError(f"manifest must contain exactly {EXPECTED_DOCUMENTS} documents")
    if manifest.get("source_count") not in (None, len(documents)):
        raise MaterializeError("manifest source_count does not match documents")

    by_source: dict[str, dict[str, Any]] = {}
    source_text: dict[str, str] = {}
    for raw_document in documents:
        if not isinstance(raw_document, dict):
            raise MaterializeError("every manifest document must be an object")
        source_id = raw_document.get("source_id")
        relative = raw_document.get("path")
        if not isinstance(source_id, str) or not source_id.strip():
            raise MaterializeError("every manifest document needs a source_id")
        if source_id in by_source:
            raise MaterializeError(f"duplicate source_id: {source_id}")
        if raw_document.get("fictional") is not True:
            raise MaterializeError(f"source is not fictional: {source_id}")
        if not isinstance(relative, str) or not relative:
            raise MaterializeError(f"source path is missing: {source_id}")
        path = (DATA / relative).resolve()
        try:
            path.relative_to(DOCS.resolve())
        except ValueError as exc:
            raise MaterializeError(f"source path escapes data/docs: {source_id}") from exc
        if not path.is_file():
            raise MaterializeError(f"source file is missing: {source_id}")
        try:
            body = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise MaterializeError(f"source file is unreadable: {source_id}") from exc
        if f"source_id: {source_id}" not in body and source_id not in body:
            raise MaterializeError(f"source identity is absent from the document: {source_id}")

        for key in ("effective_from", "effective_to"):
            try:
                parsed = datetime.fromisoformat(str(raw_document.get(key)))
            except ValueError as exc:
                raise MaterializeError(f"invalid {key}: {source_id}") from exc
            if parsed.tzinfo is None:
                raise MaterializeError(f"{key} lacks a timezone: {source_id}")
        lifecycle = raw_document.get("lifecycle")
        scope = raw_document.get("scope")
        if not isinstance(lifecycle, dict) or lifecycle.get("status") not in {
            "active",
            "superseded",
        }:
            raise MaterializeError(f"invalid lifecycle: {source_id}")
        if not isinstance(scope, dict) or not scope.get("channels") or not scope.get("products"):
            raise MaterializeError(f"missing policy scope: {source_id}")
        by_source[source_id] = raw_document
        source_text[source_id] = body

    for source_id, document in by_source.items():
        lifecycle = document["lifecycle"]
        replacement = lifecycle.get("superseded_by")
        if lifecycle["status"] == "superseded":
            if replacement not in by_source or replacement == source_id:
                raise MaterializeError(f"invalid supersession target: {source_id}")
        elif replacement is not None:
            raise MaterializeError(f"active source names a successor: {source_id}")

    questions = cases.get("questions")
    if not isinstance(questions, list) or len(questions) != EXPECTED_CASES:
        raise MaterializeError(f"cases.json must contain exactly {EXPECTED_CASES} questions")
    case_ids: set[str] = set()
    for question in questions:
        if not isinstance(question, dict):
            raise MaterializeError("every case must be an object")
        case_id = question.get("case_id")
        if not isinstance(case_id, str) or not case_id or case_id in case_ids:
            raise MaterializeError("case IDs must be non-empty and unique")
        case_ids.add(case_id)
        if not isinstance(question.get("input"), str) or not question["input"].strip():
            raise MaterializeError(f"case input is missing: {case_id}")
        groups = question.get("evidence_groups")
        if not isinstance(groups, list) or not groups:
            raise MaterializeError(f"case evidence_groups is empty: {case_id}")
        for group in groups:
            all_of = group.get("all_of") if isinstance(group, dict) else None
            if not isinstance(all_of, list) or not all_of:
                raise MaterializeError(f"evidence all_of is empty: {case_id}")
            for evidence in all_of:
                if not isinstance(evidence, dict):
                    raise MaterializeError(f"invalid evidence item: {case_id}")
                source_id = evidence.get("source_id")
                quote = evidence.get("quote")
                if source_id not in source_text or not isinstance(quote, str) or not quote:
                    raise MaterializeError(f"invalid evidence source/quote: {case_id}")
                if quote not in source_text[source_id]:
                    raise MaterializeError(f"evidence quote absent from source: {case_id}")
    return manifest, cases


def _database_url(settings: Settings) -> str:
    url = make_url(settings.database_url)
    if url.database == DATABASE_NAME:
        raise MaterializeError("configured business database is the S1 database; refusing")
    if url.get_backend_name() != "postgresql":
        raise MaterializeError("S1 requires a PostgreSQL source connection")
    return url.set(database=DATABASE_NAME).render_as_string(hide_password=False)


def _admin_url(settings: Settings) -> str:
    url = make_url(settings.database_url).set(drivername="postgresql", database="postgres")
    return url.render_as_string(hide_password=False)


def _asyncpg_url(database_url: str) -> str:
    return database_url.replace("postgresql+asyncpg://", "postgresql://", 1).replace(
        "postgresql+psycopg://", "postgresql://", 1
    )


def _target_settings(base: Settings, database_url: str) -> Settings:
    return base.model_copy(
        update={
            "database_url": database_url,
            "knowledge_qdrant_collection": COLLECTION_NAME,
            "blob_root": str(BLOB_ROOT.resolve()),
            "knowledge_embedding_device": "cuda",
            "knowledge_reranker_device": "cuda",
            "knowledge_embedding_model": "BAAI/bge-m3",
            "knowledge_reranker_model": "BAAI/bge-reranker-v2-m3",
            "knowledge_dense_vector_size": 1024,
            "knowledge_chunk_size_chars": 1_200,
            "knowledge_chunk_overlap_chars": 200,
            "knowledge_min_rerank_score": None,
            "knowledge_superseded_rank_penalty": 0.0,
            "knowledge_onyx_selector_base_url": None,
            "knowledge_onyx_selector_api_key": None,
        }
    )


async def _database_exists_for_url(database_url: str) -> bool:
    connection = await asyncpg.connect(
        make_url(database_url)
        .set(drivername="postgresql", database="postgres")
        .render_as_string(hide_password=False),
        timeout=10,
    )
    try:
        return bool(
            await connection.fetchval("SELECT 1 FROM pg_database WHERE datname=$1", DATABASE_NAME)
        )
    finally:
        await connection.close()


def _frozen_settings(settings: Settings) -> dict[str, Any]:
    """Project the retrieval-relevant settings that later resume/replay uses."""

    return {
        "blob_root": settings.blob_root,
        "database_url": settings.database_url,
        "qdrant_url": settings.qdrant_url,
        "knowledge_qdrant_collection": settings.knowledge_qdrant_collection,
        "knowledge_embedding_model": settings.knowledge_embedding_model,
        "knowledge_embedding_device": settings.knowledge_embedding_device,
        "knowledge_reranker_model": settings.knowledge_reranker_model,
        "knowledge_reranker_device": settings.knowledge_reranker_device,
        "knowledge_dense_vector_size": settings.knowledge_dense_vector_size,
        "knowledge_chunk_size_chars": settings.knowledge_chunk_size_chars,
        "knowledge_chunk_overlap_chars": settings.knowledge_chunk_overlap_chars,
        "knowledge_rrf_k": settings.knowledge_rrf_k,
        "knowledge_min_rerank_score": settings.knowledge_min_rerank_score,
        "knowledge_superseded_rank_penalty": settings.knowledge_superseded_rank_penalty,
    }


def _settings_from_snapshot(
    base: Settings,
    *,
    settings_snapshot: dict[str, Any],
) -> Settings:
    """Rebuild the searchable/runtime target from the first-run frozen values."""

    expected_keys = {
        "blob_root",
        "database_url",
        "qdrant_url",
        "knowledge_qdrant_collection",
        "knowledge_embedding_model",
        "knowledge_embedding_device",
        "knowledge_reranker_model",
        "knowledge_reranker_device",
        "knowledge_dense_vector_size",
        "knowledge_chunk_size_chars",
        "knowledge_chunk_overlap_chars",
        "knowledge_rrf_k",
        "knowledge_min_rerank_score",
        "knowledge_superseded_rank_penalty",
    }
    if not isinstance(settings_snapshot, dict) or set(settings_snapshot) != expected_keys:
        raise MaterializeError("state frozen settings are incomplete")
    frozen = base.model_copy(update=settings_snapshot)
    if _stored_database_url({"database_url": frozen.database_url}) != frozen.database_url:
        raise MaterializeError("frozen database target differs from its canonical URL")
    if frozen.blob_root != str(BLOB_ROOT.resolve()):
        raise MaterializeError("frozen blob root differs from the fixed S1 directory")
    if frozen.knowledge_qdrant_collection != COLLECTION_NAME:
        raise MaterializeError("frozen Qdrant collection differs from the fixed S1 collection")
    if frozen.knowledge_embedding_device != "cuda" or frozen.knowledge_reranker_device != "cuda":
        raise MaterializeError("frozen retrieval devices are not CUDA")
    if frozen.knowledge_embedding_model != "BAAI/bge-m3":
        raise MaterializeError("frozen dense model differs from the S1 allowlist")
    if frozen.knowledge_reranker_model != "BAAI/bge-reranker-v2-m3":
        raise MaterializeError("frozen reranker model differs from the S1 allowlist")
    if (
        frozen.knowledge_dense_vector_size != 1024
        or frozen.knowledge_chunk_size_chars != 1_200
        or frozen.knowledge_chunk_overlap_chars != 200
        or frozen.knowledge_min_rerank_score is not None
        or frozen.knowledge_superseded_rank_penalty != 0.0
    ):
        raise MaterializeError("frozen retrieval settings differ from the S1 configuration")
    return frozen


async def _database_exists(settings: Settings) -> bool:
    connection = await asyncpg.connect(_admin_url(settings), timeout=10)
    try:
        return bool(
            await connection.fetchval("SELECT 1 FROM pg_database WHERE datname=$1", DATABASE_NAME)
        )
    finally:
        await connection.close()


def _stored_database_url(state: dict[str, Any]) -> str:
    stored = state.get("database_url")
    if not isinstance(stored, str) or not stored:
        raise MaterializeError("existing state has no private database_url")
    url = make_url(stored)
    if url.get_backend_name() != "postgresql" or url.database != DATABASE_NAME:
        raise MaterializeError("existing state database_url is not the fixed S1 database")
    return url.render_as_string(hide_password=False)


async def _create_database(settings: Settings) -> None:
    if not _SAFE_DATABASE_NAME.fullmatch(DATABASE_NAME):
        raise MaterializeError("unsafe isolated database name")
    connection = await asyncpg.connect(_admin_url(settings), timeout=10)
    try:
        if await connection.fetchval("SELECT 1 FROM pg_database WHERE datname=$1", DATABASE_NAME):
            raise MaterializeError("isolated database appeared before creation")
        await connection.execute(f'CREATE DATABASE "{DATABASE_NAME}"')
    finally:
        await connection.close()


def _collection_exists(settings: Settings) -> bool:
    client = QdrantClient(url=settings.qdrant_url, timeout=10)
    try:
        return bool(client.collection_exists(COLLECTION_NAME))
    finally:
        close = getattr(client, "close", None)
        if close is not None:
            close()


def _collection_count(settings: Settings) -> int:
    client = QdrantClient(url=settings.qdrant_url, timeout=10)
    try:
        return int(client.count(COLLECTION_NAME, exact=True).count)
    finally:
        close = getattr(client, "close", None)
        if close is not None:
            close()


def _load_state() -> dict[str, Any]:
    state = _read_json(STATE_FILE)
    required = {
        "schema_version": STATE_SCHEMA_VERSION,
        "database_name": DATABASE_NAME,
        "collection_name": COLLECTION_NAME,
        "ownership_marker": "support_refund_rag_s1_20261026",
    }
    for key, expected in required.items():
        if state.get(key) != expected:
            raise MaterializeError(f"existing state is not this experiment's state: {key}")
    ownership_token = state.get("ownership_token")
    try:
        UUID(str(ownership_token))
    except (TypeError, ValueError) as exc:
        raise MaterializeError("existing state has no valid ownership token") from exc
    if state.get("state_interface_version") != STATE_INTERFACE_VERSION:
        raise MaterializeError("unsupported S1 state interface version")
    _stored_database_url(state)
    if not isinstance(state.get("qdrant_url"), str) or not state["qdrant_url"]:
        raise MaterializeError("existing state has no qdrant_url interface")
    if not isinstance(state.get("frozen_settings"), dict):
        raise MaterializeError("existing state has no frozen settings interface")
    if state["frozen_settings"].get("database_url") != state["database_url"]:
        raise MaterializeError("state database_url differs from frozen settings")
    if state["frozen_settings"].get("qdrant_url") != state["qdrant_url"]:
        raise MaterializeError("state qdrant_url differs from frozen settings")
    if not isinstance(state.get("documents"), dict):
        raise MaterializeError("existing state has no document map")
    identities = state.get("identities")
    if not isinstance(identities, dict) or set(identities) != {
        "user_id",
        "organization_id",
        "workspace_id",
        "org_role",
        "workspace_role",
    }:
        raise MaterializeError("existing state has an invalid identities interface")
    context_ids = state.get("ctx", {}).get("ids") if isinstance(state.get("ctx"), dict) else None
    if not isinstance(context_ids, dict) or set(context_ids) != {
        "user_id",
        "organization_id",
        "workspace_id",
    }:
        raise MaterializeError("existing state has an invalid ctx.ids interface")
    if context_ids != {
        "user_id": identities["user_id"],
        "organization_id": identities["organization_id"],
        "workspace_id": identities["workspace_id"],
    }:
        raise MaterializeError("state ctx.ids differs from identities")
    knowledge = state.get("knowledge")
    if not isinstance(knowledge, dict) or not {
        "knowledge_base_id",
        "snapshot_id",
        "snapshot_hash",
        "snapshot_item_count",
        "snapshot_schema_version",
    }.issubset(knowledge):
        raise MaterializeError("existing state has an invalid knowledge interface")
    return state


def _save_state(state: dict[str, Any]) -> None:
    state["updated_at_utc"] = _utc_now()
    _atomic_write_json(STATE_FILE, state)


def _new_state(ownership_token: str, settings: Settings) -> dict[str, Any]:
    frozen_settings = _frozen_settings(settings)
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "ownership_marker": "support_refund_rag_s1_20261026",
        "ownership_token": ownership_token,
        "database_name": DATABASE_NAME,
        "collection_name": COLLECTION_NAME,
        "qdrant_url": frozen_settings["qdrant_url"],
        "blob_root": str(BLOB_ROOT.resolve()),
        "output_root": str(PRIVATE.resolve()),
        "created_at_utc": _utc_now(),
        "updated_at_utc": _utc_now(),
        "phase": "database_created",
        "database_created_by_this_run": True,
        "collection_creation_intent": False,
        "collection_owned_by_this_run": False,
        "alembic": {"status": "PENDING", "head": None},
        "checkpoint": {"status": "PENDING"},
        "terminal_status": None,
        "state_interface_version": STATE_INTERFACE_VERSION,
        "database_url": frozen_settings["database_url"],
        "frozen_settings": frozen_settings,
        "identities": {
            "user_id": None,
            "organization_id": None,
            "workspace_id": None,
            "org_role": "OWNER",
            "workspace_role": "DEVELOPER",
        },
        "knowledge": {
            "knowledge_base_id": None,
            "snapshot_id": None,
            "snapshot_hash": None,
            "snapshot_item_count": None,
            "snapshot_schema_version": None,
        },
        "retrieval_config": RETRIEVAL_CONFIG,
        "documents": {},
        "ctx": {
            "ids": {
                "user_id": None,
                "organization_id": None,
                "workspace_id": None,
            },
            "org_role": "OWNER",
            "workspace_role": "DEVELOPER",
        },
        "retrieval": {
            "completed_case_ids": [],
            "failed_case_id": None,
        },
        "last_error": None,
    }


async def _preflight_resources(settings: Settings, *, state_exists: bool) -> tuple[bool, bool]:
    database_exists, collection_exists = await asyncio.gather(
        _database_exists(settings), asyncio.to_thread(_collection_exists, settings)
    )
    blob_exists = BLOB_ROOT.exists() and any(BLOB_ROOT.iterdir())

    if not state_exists:
        unexpected_root = (
            [path.name for path in PRIVATE.iterdir() if path.name != LOCK_FILE.name]
            if PRIVATE.exists()
            else []
        )
        if unexpected_root:
            raise MaterializeError("private/s1 has artifacts without its ownership state")
        if database_exists:
            raise MaterializeError(
                "isolated database already exists without this experiment's state; refusing"
            )
        if collection_exists:
            raise MaterializeError(
                "isolated Qdrant collection already exists without state; refusing"
            )
        if blob_exists:
            raise MaterializeError("isolated blob root contains data without state; refusing")
    return database_exists, collection_exists


async def _preflight_owned_resources(settings: Settings) -> tuple[bool, bool]:
    """Preflight from the state-frozen connection, never the current business URL."""

    database_exists, collection_exists = await asyncio.gather(
        _database_exists_for_url(settings.database_url),
        asyncio.to_thread(_collection_exists, settings),
    )
    return database_exists, collection_exists


def _runtime_python() -> str:
    candidates = (
        ROOT.parents[3] / ".venv" / "Scripts" / "python.exe",
        ROOT.parents[3] / ".venv" / "bin" / "python",
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def _run_captured(
    command: list[str],
    *,
    environment: dict[str, str],
    database_url: str,
    log_path: Path,
    timeout_seconds: int,
) -> None:
    started = time.perf_counter()
    try:
        result = subprocess.run(
            command,
            cwd=ROOT.parents[3],
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        output = _redact_sensitive(result.stdout + result.stderr, database_url)
        exit_code = result.returncode
    except subprocess.TimeoutExpired as exc:
        stdout = (
            exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout
        )
        stderr = (
            exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr
        )
        output = _redact_sensitive((stdout or "") + (stderr or ""), database_url)
        exit_code = -1
        _atomic_write_text(
            log_path,
            output + f"\n[timeout after {timeout_seconds}s]\n",
        )
        raise MaterializeError(f"deployment command timed out: {log_path.name}") from exc
    _atomic_write_text(
        log_path,
        output + f"\n[exit_code={exit_code} elapsed_seconds={time.perf_counter() - started:.3f}]\n",
    )
    if exit_code != 0:
        raise MaterializeError(f"deployment command failed: {log_path.name}")


def _alembic_head(settings: Settings) -> str:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(ROOT.parents[3] / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT.parents[3] / "migrations"))
    head = ScriptDirectory.from_config(config).get_current_head()
    if head is None:
        raise MaterializeError("Alembic has no head revision")
    return head


async def _assert_alembic_head(settings: Settings, expected_head: str) -> None:
    engine, factory = create_database(settings.database_url)
    try:
        async with factory() as session:
            current = await session.scalar(sql_text("SELECT version_num FROM alembic_version"))
    finally:
        await engine.dispose()
    if current != expected_head:
        raise MaterializeError(
            f"Alembic current revision does not equal actual head ({expected_head})"
        )


def _migrate_and_checkpoint(settings: Settings, database_url: str, state: dict[str, Any]) -> None:
    repository = ROOT.parents[3]
    environment = dict(os.environ, AGENTHUB_DATABASE_URL=database_url)
    expected_head = _alembic_head(settings)
    _run_captured(
        [_runtime_python(), "-m", "alembic", "upgrade", "head"],
        environment=environment,
        database_url=database_url,
        log_path=PRIVATE / "logs" / "alembic-head.log",
        timeout_seconds=600,
    )
    state["alembic"] = {
        "status": "UPGRADED",
        "head": expected_head,
        "verified_at_utc": _utc_now(),
    }
    state["phase"] = "alembic_head"
    _save_state(state)

    _run_captured(
        [_runtime_python(), str(repository / "scripts" / "bootstrap_checkpoint.py")],
        environment=environment,
        database_url=database_url,
        log_path=PRIVATE / "logs" / "checkpoint-bootstrap.log",
        timeout_seconds=300,
    )
    state["checkpoint"] = {"status": "BOOTSTRAPPED", "verified_at_utc": _utc_now()}
    state["phase"] = "checkpoint_ready"
    _save_state(state)


async def _assert_alembic_and_checkpoint(settings: Settings, expected_head: str) -> None:
    await _assert_alembic_head(settings, expected_head)
    connection = await asyncpg.connect(_asyncpg_url(settings.database_url), timeout=10)
    try:
        checkpoints = await connection.fetchval(
            "SELECT to_regclass('langgraph_checkpoint.checkpoints')"
        )
        writes = await connection.fetchval(
            "SELECT to_regclass('langgraph_checkpoint.checkpoint_writes')"
        )
    finally:
        await connection.close()
    if checkpoints is None or writes is None:
        raise MaterializeError("LangGraph checkpoint bootstrap tables are absent")


async def _ensure_collection(
    settings: Settings, state: dict[str, Any], *, existed_at_preflight: bool
) -> None:
    if state.get("collection_owned_by_this_run"):
        if not await asyncio.to_thread(_collection_exists, settings):
            raise MaterializeError("an owned Qdrant collection disappeared; refusing to rebuild")
        return
    if not existed_at_preflight and state.get("collection_creation_intent"):
        # The prior process durably recorded its intent before calling Qdrant.
        # Absence at preflight means no collection was created; retrying that
        # idempotent ensure operation is therefore safe.
        components = production_retrieval_components(settings)
        await asyncio.to_thread(components.index.ensure_collection)
        if not await asyncio.to_thread(_collection_exists, settings):
            raise MaterializeError("owned collection intent did not produce a collection")
        state["collection_owned_by_this_run"] = True
        state["collection_creation_intent"] = False
        state["phase"] = "collection_ready"
        _save_state(state)
        return
    if existed_at_preflight:
        raise MaterializeError("Qdrant collection exists but this state does not own it")

    state["collection_creation_intent"] = True
    _save_state(state)
    components = production_retrieval_components(settings)
    await asyncio.to_thread(components.index.ensure_collection)
    if not await asyncio.to_thread(_collection_exists, settings):
        raise MaterializeError("Qdrant collection creation did not produce a collection")
    state["collection_owned_by_this_run"] = True
    state["collection_creation_intent"] = False
    state["phase"] = "collection_ready"
    _save_state(state)


async def _ensure_workspace(
    factory: async_sessionmaker[AsyncSession], state: dict[str, Any]
) -> dict[str, str]:
    existing = state.get("identities")
    if existing is not None and all(
        existing.get(key) for key in ("user_id", "organization_id", "workspace_id")
    ):
        identities = {
            "user_id": str(existing["user_id"]),
            "organization_id": str(existing["organization_id"]),
            "workspace_id": str(existing["workspace_id"]),
        }
        async with factory() as session:
            user = await session.get(User, UUID(identities["user_id"]))
            organization = await session.get(Organization, UUID(identities["organization_id"]))
            workspace = await session.get(Workspace, UUID(identities["workspace_id"]))
        if (
            user is None
            or organization is None
            or workspace is None
            or organization.name != ORG_NAME
            or workspace.name != WORKSPACE_NAME
            or workspace.organization_id != organization.id
        ):
            raise MaterializeError("recorded S1 organization/workspace identities are inconsistent")
        return identities

    async with factory() as session:
        organizations = list(
            await session.scalars(select(Organization).where(Organization.name == ORG_NAME))
        )
        if len(organizations) > 1:
            raise MaterializeError("multiple S1 organizations exist without recorded state")
        if organizations:
            organization = organizations[0]
            user = await session.get(User, organization.created_by)
            workspaces = list(
                await session.scalars(
                    select(Workspace).where(
                        Workspace.organization_id == organization.id,
                        Workspace.name == WORKSPACE_NAME,
                    )
                )
            )
            if user is None or len(workspaces) != 1:
                raise MaterializeError("partially created S1 workspace cannot be recovered safely")
            workspace = workspaces[0]
        else:
            token = str(state["ownership_token"])
            email = f"support-refund-rag-{token}@benchmark.invalid"
            user = User(email=email, normalized_email=email, password_hash="unused-s1-fixture")
            session.add(user)
            await session.flush()
            organization = Organization(name=ORG_NAME, created_by=user.id)
            session.add(organization)
            await session.flush()
            workspace = Workspace(organization_id=organization.id, name=WORKSPACE_NAME)
            session.add(workspace)
            await session.flush()
            session.add_all(
                [
                    OrganizationMembership(
                        organization_id=organization.id,
                        user_id=user.id,
                        role="OWNER",
                    ),
                    WorkspaceMembership(
                        workspace_id=workspace.id,
                        user_id=user.id,
                        role="DEVELOPER",
                    ),
                ]
            )
            await session.commit()

        organization_membership = await session.scalar(
            select(OrganizationMembership).where(
                OrganizationMembership.organization_id == organization.id,
                OrganizationMembership.user_id == user.id,
            )
        )
        workspace_membership = await session.scalar(
            select(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == workspace.id,
                WorkspaceMembership.user_id == user.id,
            )
        )
        if organization_membership is None or workspace_membership is None:
            raise MaterializeError("S1 organization and workspace memberships are incomplete")

    identities = {
        "user_id": str(user.id),
        "organization_id": str(organization.id),
        "workspace_id": str(workspace.id),
    }
    state["identities"] = {
        **identities,
        "org_role": "OWNER",
        "workspace_role": "DEVELOPER",
    }
    state["ctx"] = {
        "ids": identities,
        "org_role": "OWNER",
        "workspace_role": "DEVELOPER",
    }
    state["phase"] = "workspace_ready"
    _save_state(state)
    return identities


def _context(identities: dict[str, str], purpose: str) -> WorkspaceExecutionContext:
    return WorkspaceExecutionContext(
        organization=OrganizationContext(
            principal=PrincipalContext(
                user_id=identities["user_id"],
                request_id=f"support-refund-rag-s1-{purpose}-{uuid4().hex}",
                trace_id=f"support-refund-rag-s1-{purpose}-{uuid4().hex}",
            ),
            organization_id=identities["organization_id"],
            org_role="OWNER",
        ),
        workspace_id=identities["workspace_id"],
        workspace_role="DEVELOPER",
        permissions=frozenset(
            {"workspace_read", "knowledge_create", "knowledge_edit", "knowledge_run"}
        ),
    )


async def _ensure_knowledge_base(
    factory: async_sessionmaker[AsyncSession],
    state: dict[str, Any],
    context: WorkspaceExecutionContext,
) -> KnowledgeBase:
    knowledge = state["knowledge"]
    workspace_id = UUID(context.workspace_id)
    if knowledge.get("knowledge_base_id"):
        async with factory() as session:
            knowledge_base = await session.get(KnowledgeBase, UUID(knowledge["knowledge_base_id"]))
        if (
            knowledge_base is None
            or knowledge_base.workspace_id != workspace_id
            or knowledge_base.name != KNOWLEDGE_BASE_NAME
        ):
            raise MaterializeError("recorded S1 knowledge base is inconsistent")
        return knowledge_base

    async with factory() as session:
        matches = list(
            await session.scalars(
                select(KnowledgeBase).where(KnowledgeBase.workspace_id == workspace_id)
            )
        )
        if matches:
            raise MaterializeError(
                "workspace has knowledge bases without a recorded S1 knowledge_base_id"
            )
        knowledge_base = await KnowledgeService().create_knowledge_base(
            session, context=context, name=KNOWLEDGE_BASE_NAME
        )

    knowledge["knowledge_base_id"] = str(knowledge_base.id)
    _save_state(state)
    return knowledge_base


async def _read_blob(store: LocalBlobStore, blob_key: str) -> bytes:
    value = bytearray()
    async for item in store.read(blob_key):
        value.extend(item)
    return bytes(value)


async def _latest_document_bundle(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    knowledge_base_id: UUID,
    document_id: UUID,
) -> tuple[Document, DocumentRevision, IngestionJob]:
    document = await session.get(Document, document_id)
    revision = await session.scalar(
        select(DocumentRevision)
        .where(DocumentRevision.document_id == document_id)
        .order_by(DocumentRevision.revision_number.desc())
        .limit(1)
    )
    job = (
        await session.scalar(
            select(IngestionJob).where(
                IngestionJob.workspace_id == workspace_id,
                IngestionJob.knowledge_base_id == knowledge_base_id,
                IngestionJob.document_revision_id == revision.id,
            )
        )
        if revision is not None
        else None
    )
    if (
        document is None
        or revision is None
        or job is None
        or document.workspace_id != workspace_id
        or document.knowledge_base_id != knowledge_base_id
        or revision.document_id != document.id
        or job.document_revision_id != revision.id
    ):
        raise MaterializeError("uploaded document revision/job bundle is inconsistent")
    return document, revision, job


async def _load_chunks(session: AsyncSession, revision_id: UUID) -> list[DocumentChunk]:
    return list(
        await session.scalars(
            select(DocumentChunk)
            .where(DocumentChunk.document_revision_id == revision_id)
            .order_by(DocumentChunk.ordinal, DocumentChunk.chunk_id)
        )
    )


def _document_name(source_id: str, relative_path: str) -> str:
    suffix = Path(relative_path).name
    value = f"{source_id}__{suffix}"
    if len(value) > 255:
        raise MaterializeError(f"upload filename is too long: {source_id}")
    return value


def _verify_chunks(source: str, chunks: list[DocumentChunk]) -> None:
    if not chunks:
        raise MaterializeError("a READY document has no chunks")
    normalized_source = normalize_chunk_text(source)
    for chunk in chunks:
        start = chunk.locator.get("char_start")
        end = chunk.locator.get("char_end")
        if type(start) is not int or type(end) is not int or not 0 <= start < end:
            raise MaterializeError(f"chunk locator is invalid: {chunk.chunk_id}")
        # Production chunking strips each window separately. Coordinates still
        # span the untrimmed window, so compare with the same normalization.
        if (
            end > len(normalized_source)
            or normalize_chunk_text(normalized_source[start:end]) != chunk.text
        ):
            raise MaterializeError(f"chunk text does not match the stored source: {chunk.chunk_id}")


def _retrieval_identity(settings: Settings, components: object) -> dict[str, Any]:
    config = {
        **RETRIEVAL_CONFIG,
        "embedding_model": settings.knowledge_embedding_model,
        "reranker_model": settings.knowledge_reranker_model,
        "rrf_k": settings.knowledge_rrf_k,
    }
    if retrieval_model_identity_mismatches(config, components):
        raise MaterializeError("deployed retrieval model identity differs from frozen S1 settings")
    return config


async def _adopt_or_upload_document(
    factory: async_sessionmaker[AsyncSession],
    *,
    state: dict[str, Any],
    context: WorkspaceExecutionContext,
    knowledge_base: KnowledgeBase,
    manifest_document: dict[str, Any],
    source: str,
    settings: Settings,
    retrieval_identity: dict[str, Any],
) -> dict[str, Any]:
    source_id = manifest_document["source_id"]
    filename = _document_name(source_id, manifest_document["path"])
    workspace_id = UUID(context.workspace_id)
    record = state["documents"].setdefault(source_id, {})

    if record.get("document_id"):
        async with factory() as session:
            document, revision, job = await _latest_document_bundle(
                session,
                workspace_id=workspace_id,
                knowledge_base_id=knowledge_base.id,
                document_id=UUID(record["document_id"]),
            )
        if document.name != filename:
            raise MaterializeError(f"recorded document name mismatch: {source_id}")
    else:
        async with factory() as session:
            matches = list(
                await session.scalars(
                    select(Document).where(
                        Document.workspace_id == workspace_id,
                        Document.knowledge_base_id == knowledge_base.id,
                    )
                )
            )
        mapped_ids = {
            UUID(mapped["document_id"])
            for mapped in state["documents"].values()
            if mapped.get("document_id")
        }
        if any(document.id not in mapped_ids for document in matches):
            raise MaterializeError(
                "knowledge base contains documents without a recorded source_id map"
            )

        content = source.encode("utf-8")

        async def uploaded_bytes() -> AsyncIterator[bytes]:
            yield content

        queue = _RecordedQueue()
        async with factory() as session:
            document, revision, job = await KnowledgeService().upload_document(
                session,
                context=context,
                knowledge_base_id=knowledge_base.id,
                filename=filename,
                media_type="text/markdown",
                chunks=uploaded_bytes(),
                blob_store=LocalBlobStore(settings.blob_root),
                queue=queue,
            )

        record.update(
            {
                "document_id": str(document.id),
                "revision_id": str(revision.id),
                "job_id": str(job.id),
                "blob_key": revision.blob_key,
                "filename": filename,
                "ingestion_status": "PENDING",
            }
        )
        _save_state(state)

    async with factory() as session:
        document, revision, job = await _latest_document_bundle(
            session,
            workspace_id=workspace_id,
            knowledge_base_id=knowledge_base.id,
            document_id=UUID(record["document_id"]),
        )
    stored_bytes = await _read_blob(LocalBlobStore(settings.blob_root), revision.blob_key)
    if stored_bytes != source.encode("utf-8"):
        raise MaterializeError(f"stored blob does not match source bytes: {source_id}")

    if job.status == "FAILED" or revision.ingestion_status == "FAILED":
        raise MaterializeError(
            f"ingestion previously failed: {source_id}: {job.last_error_code or 'UNKNOWN'}"
        )
    if job.status != "SUCCEEDED" or revision.ingestion_status != "READY":
        components = production_retrieval_components(settings)
        await _process_knowledge_ingestion(
            job.id,
            settings,
            indexing_factory=lambda _settings: components,
        )
        async with factory() as session:
            document, revision, job = await _latest_document_bundle(
                session,
                workspace_id=workspace_id,
                knowledge_base_id=knowledge_base.id,
                document_id=UUID(record["document_id"]),
            )

    if job.status != "SUCCEEDED" or revision.ingestion_status != "READY":
        raise MaterializeError(
            f"production ingestion did not succeed: {source_id}: "
            f"job={job.status} revision={revision.ingestion_status} "
            f"code={job.last_error_code or 'UNKNOWN'}"
        )
    async with factory() as session:
        chunks = await _load_chunks(session, revision.id)
    _verify_chunks(source, chunks)

    record.update(
        {
            "document_id": str(document.id),
            "revision_id": str(revision.id),
            "job_id": str(job.id),
            "blob_key": revision.blob_key,
            "filename": filename,
            "file_size": revision.file_size,
            "ingestion_status": "READY",
            "chunks": [
                {
                    "chunk_id": chunk.chunk_id,
                    "ordinal": chunk.ordinal,
                    "locator": dict(chunk.locator),
                    "normalized_content_hash": chunk.normalized_content_hash,
                    "text": chunk.text,
                }
                for chunk in chunks
            ],
            "metadata": {
                "source_group": manifest_document.get("source_group"),
                "document_kind": manifest_document.get("document_kind"),
                "fictional": True,
                "effective_from": manifest_document["effective_from"],
                "effective_to": manifest_document["effective_to"],
                "scope": manifest_document["scope"],
                "declared_lifecycle": manifest_document["lifecycle"],
                "retrieval_identity": retrieval_identity,
            },
            "lifecycle_verified": False,
        }
    )
    _save_state(state)
    return record


async def _apply_lifecycles(
    factory: async_sessionmaker[AsyncSession],
    *,
    state: dict[str, Any],
    context: WorkspaceExecutionContext,
    knowledge_base: KnowledgeBase,
    manifest: dict[str, Any],
) -> None:
    by_source = {item["source_id"]: item for item in manifest["documents"]}
    service = KnowledgeService()
    for source_id, declaration in by_source.items():
        record = state["documents"][source_id]
        document_id = UUID(record["document_id"])
        successor_source = declaration["lifecycle"].get("superseded_by")
        successor_id = (
            UUID(state["documents"][successor_source]["document_id"])
            if successor_source is not None
            else None
        )
        effective_date = datetime.fromisoformat(declaration["effective_from"]).date()
        async with factory() as session:
            current = await session.get(Document, document_id)
            if current is None:
                raise MaterializeError(f"document disappeared before lifecycle write: {source_id}")
            needs_write = (
                current.effective_date != effective_date
                or current.superseded_by_document_id != successor_id
            )
            if needs_write:
                current = await service.update_document_lifecycle(
                    session,
                    context=context,
                    knowledge_base_id=knowledge_base.id,
                    document_id=document_id,
                    effective_date=effective_date,
                    superseded_by_document_id=successor_id,
                    set_effective_date=True,
                    set_superseded_by=True,
                )
            if (
                current.effective_date != effective_date
                or current.superseded_by_document_id != successor_id
            ):
                raise MaterializeError(f"lifecycle readback mismatch: {source_id}")

        record["lifecycle_verified"] = True
        record["stored_lifecycle"] = {
            "effective_date": current.effective_date.isoformat(),
            "superseded_by_document_id": (
                str(current.superseded_by_document_id)
                if current.superseded_by_document_id is not None
                else None
            ),
            "effective_to_preserved_in_manifest": declaration["effective_to"],
            "scope_preserved_in_manifest": declaration["scope"],
        }
        _save_state(state)


async def _create_or_resolve_snapshot(
    factory: async_sessionmaker[AsyncSession],
    *,
    state: dict[str, Any],
    context: WorkspaceExecutionContext,
    knowledge_base: KnowledgeBase,
) -> dict[str, Any]:
    service = KnowledgeSnapshotService()
    async with factory() as session:
        if state["knowledge"].get("snapshot_id"):
            snapshot = await service.resolve_snapshot(
                session,
                context,
                knowledge_base.id,
                UUID(state["knowledge"]["snapshot_id"]),
            )
        else:
            snapshot = await service.create_current_snapshot(session, context, knowledge_base.id)

        members = list(
            await session.execute(
                select(
                    KnowledgeSnapshotItem.document_id,
                    KnowledgeSnapshotItem.document_revision_id,
                )
                .where(
                    KnowledgeSnapshotItem.workspace_id == UUID(context.workspace_id),
                    KnowledgeSnapshotItem.knowledge_base_id == knowledge_base.id,
                    KnowledgeSnapshotItem.snapshot_id == snapshot.snapshot_id,
                )
                .order_by(
                    KnowledgeSnapshotItem.document_id,
                    KnowledgeSnapshotItem.document_revision_id,
                )
            )
        )

    expected = {
        (UUID(record["document_id"]), UUID(record["revision_id"]))
        for record in state["documents"].values()
    }
    actual = {(document_id, revision_id) for document_id, revision_id in members}
    if actual != expected or len(actual) != EXPECTED_DOCUMENTS:
        raise MaterializeError("knowledge snapshot membership does not match all 16 documents")

    state["knowledge"].update(
        {
            "snapshot_id": str(snapshot.snapshot_id),
            "snapshot_hash": snapshot.content_hash,
            "snapshot_item_count": snapshot.item_count,
            "snapshot_schema_version": snapshot.snapshot_schema_version,
        }
    )
    state["phase"] = "snapshot_ready"
    _save_state(state)
    _atomic_write_json(
        PRIVATE / "snapshot.json",
        {
            "snapshot_id": str(snapshot.snapshot_id),
            "snapshot_hash": snapshot.content_hash,
            "snapshot_schema_version": snapshot.snapshot_schema_version,
            "item_count": snapshot.item_count,
            "members": [
                {"document_id": str(document_id), "document_revision_id": str(revision_id)}
                for document_id, revision_id in members
            ],
            "status": "MATERIALIZED",
        },
    )
    return {
        "snapshot_id": str(snapshot.snapshot_id),
        "snapshot_hash": snapshot.content_hash,
        "snapshot_schema_version": snapshot.snapshot_schema_version,
        "item_count": snapshot.item_count,
    }


def _source_map(state: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    documents: dict[str, Any] = {}
    source_by_chunk: dict[str, str] = {}
    source_by_document: dict[str, str] = {}
    for source_id, record in state["documents"].items():
        documents[source_id] = {
            "document_id": record["document_id"],
            "document_revision_id": record["revision_id"],
            "job_id": record["job_id"],
            "blob_key": record["blob_key"],
            "filename": record["filename"],
            "file_size": record["file_size"],
            "snapshot_id": snapshot["snapshot_id"],
            "snapshot_hash": snapshot["snapshot_hash"],
            "metadata": record["metadata"],
            "stored_lifecycle": record["stored_lifecycle"],
            "chunks": record["chunks"],
        }
        source_by_document[record["document_id"]] = source_id
        for chunk in record["chunks"]:
            if chunk["chunk_id"] in source_by_chunk:
                raise MaterializeError("chunk identity maps to multiple source documents")
            source_by_chunk[chunk["chunk_id"]] = source_id
    return {
        "documents": documents,
        "source_by_document_id": source_by_document,
        "source_by_chunk_id": source_by_chunk,
        "snapshot": snapshot,
    }


def _persist_source_map(state: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    """Keep the runtime-visible source/chunk map in the single S1 state."""

    source_map = _source_map(state, snapshot)
    state["source_map"] = {
        "documents": {
            source_id: {
                "document_id": record["document_id"],
                "document_revision_id": record["revision_id"],
                "snapshot_id": snapshot["snapshot_id"],
                "snapshot_hash": snapshot["snapshot_hash"],
                "chunks": record["chunks"],
            }
            for source_id, record in state["documents"].items()
        },
        "source_by_document_id": source_map["source_by_document_id"],
        "source_by_chunk_id": source_map["source_by_chunk_id"],
        "snapshot": snapshot,
    }
    _save_state(state)
    return source_map


def _verify_retrieval_identity(source_map: dict[str, Any]) -> None:
    identities = {
        json.dumps(
            record.get("metadata", {}).get("retrieval_identity"),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        for record in source_map["documents"].values()
    }
    if len(identities) != 1 or identities == {"null"}:
        raise MaterializeError("ingestion documents do not share one frozen retrieval identity")


def _gpu_identity(components: object) -> dict[str, Any]:
    import torch

    if not torch.cuda.is_available():
        raise MaterializeError("CUDA is required for ingestion and retrieval")
    dense_device = getattr(getattr(components, "dense", None), "_device", None)
    reranker_device = getattr(getattr(components, "reranker", None), "_device", None)
    if dense_device != "cuda" or reranker_device != "cuda":
        raise MaterializeError(
            f"retrieval models did not resolve to CUDA: dense={dense_device}, "
            f"reranker={reranker_device}"
        )
    capability = torch.cuda.get_device_capability(0)
    return {
        "required_device": "cuda",
        "dense_device": str(dense_device),
        "reranker_device": str(reranker_device),
        "gpu_name": torch.cuda.get_device_name(0),
        "gpu_capability": list(capability),
        "torch_version": torch.__version__,
        "cuda_version": torch.version.cuda,
    }


async def _warm_retrieval(settings: Settings) -> tuple[object, dict[str, Any]]:
    components = production_retrieval_components(settings)
    for component in (components.dense, components.sparse, components.reranker):
        warm = getattr(component, "warm", None)
        if warm is not None:
            await asyncio.to_thread(warm)
    gpu = await asyncio.to_thread(_gpu_identity, components)
    return components, gpu


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))


def _quote_coverage(
    question: dict[str, Any], candidate_rows: list[dict[str, Any]], final_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    groups: list[dict[str, Any]] = []
    for group_index, group in enumerate(question["evidence_groups"]):
        requirements: list[dict[str, Any]] = []
        for evidence in group["all_of"]:
            source_id = evidence["source_id"]
            quote = _normalized(evidence["quote"])
            candidate_ids = [
                row["chunk_id"]
                for row in candidate_rows
                if row["source_id"] == source_id and quote in _normalized(row["text"])
            ]
            final_ids = [
                row["chunk_id"]
                for row in final_rows
                if row["source_id"] == source_id and quote in _normalized(row["text"])
            ]
            requirements.append(
                {
                    "source_id": source_id,
                    "quote": evidence["quote"],
                    "candidate_present": bool(candidate_ids),
                    "final_present": bool(final_ids),
                    "candidate_chunk_ids": candidate_ids,
                    "final_chunk_ids": final_ids,
                }
            )
        groups.append(
            {
                "group_index": group_index,
                "label": group.get("label"),
                "all_of": requirements,
                "candidate_complete": all(item["candidate_present"] for item in requirements),
                "final_complete": all(item["final_present"] for item in requirements),
            }
        )
    return {
        "logic": "outer_OR_inner_AND",
        "groups": groups,
        "candidate_complete": any(group["candidate_complete"] for group in groups),
        "final_complete": any(group["final_complete"] for group in groups),
    }


def _validate_result_file(
    result: dict[str, Any], *, question: dict[str, Any], knowledge: dict[str, Any]
) -> None:
    if result.get("case_id") != question["case_id"]:
        raise MaterializeError("retrieval result case identity mismatch")
    if result.get("query") != question["input"]:
        raise MaterializeError("retrieval result query differs from the case input")
    if result.get("knowledge_base_id") != knowledge["knowledge_base_id"]:
        raise MaterializeError("retrieval result knowledge base mismatch")
    if result.get("snapshot_id") != knowledge["snapshot_id"]:
        raise MaterializeError("retrieval result snapshot mismatch")
    if result.get("model_dispatches") != 0 or result.get("tool_runtime_invoked") is not False:
        raise MaterializeError("retrieval result crossed the zero-generation boundary")


def _case_result_path(case_id: str) -> Path:
    return PRIVATE / "retrieval-results" / f"{case_id}.json"


async def _retrieve_cases(
    factory: async_sessionmaker[AsyncSession],
    *,
    state: dict[str, Any],
    context: WorkspaceExecutionContext,
    cases: dict[str, Any],
    source_map: dict[str, Any],
    components: object,
    retrieval_identity: dict[str, Any],
    rrf_k: int,
) -> None:
    retriever = SessionScopedKnowledgeRetriever(
        session_factory=factory,
        components=components,
        rrf_k=rrf_k,
        min_rerank_score=RETRIEVAL_CONFIG["min_rerank_score"],
        superseded_rank_penalty=RETRIEVAL_CONFIG["superseded_rank_penalty"],
    )
    chunks_by_id: dict[str, dict[str, Any]] = {}
    source_by_chunk = source_map["source_by_chunk_id"]
    document_names: dict[str, str] = {}
    for source_id, document in source_map["documents"].items():
        document_names[document["document_id"]] = source_id
        for chunk in document["chunks"]:
            chunks_by_id[chunk["chunk_id"]] = {
                **chunk,
                "source_id": source_id,
                "document_id": document["document_id"],
                "document_revision_id": document["document_revision_id"],
            }

    completed = set(state["retrieval"]["completed_case_ids"])
    if state["retrieval"].get("failed_case_id"):
        raise MaterializeError(
            "a prior retrieval failure is preserved; refusing an implicit retry "
            f"({state['retrieval']['failed_case_id']})"
        )

    for question in cases["questions"]:
        case_id = question["case_id"]
        path = _case_result_path(case_id)
        if case_id in completed:
            if not path.is_file():
                raise MaterializeError(f"completed retrieval result is missing: {case_id}")
            _validate_result_file(
                _read_json(path),
                question=question,
                knowledge=state["knowledge"],
            )
            continue
        if path.exists():
            adopted = _read_json(path)
            _validate_result_file(adopted, question=question, knowledge=state["knowledge"])
            state["retrieval"]["completed_case_ids"].append(case_id)
            completed.add(case_id)
            _save_state(state)
            continue

        query = RetrievalQuery(
            text=question["input"],
            knowledge_base_id=state["knowledge"]["knowledge_base_id"],
            knowledge_snapshot_id=state["knowledge"]["snapshot_id"],
            strategy=RetrievalStrategy(RETRIEVAL_CONFIG["retrieval_strategy"]),
            dense_top_k=RETRIEVAL_CONFIG["dense_top_k"],
            sparse_top_k=RETRIEVAL_CONFIG["sparse_top_k"],
            candidate_top_k=RETRIEVAL_CONFIG["candidate_top_k"],
            final_top_k=RETRIEVAL_CONFIG["final_top_k"],
            rerank_top_k=RETRIEVAL_CONFIG["rerank_top_k"],
            max_chunks_per_document=RETRIEVAL_CONFIG["max_chunks_per_document"],
            context_budget_bytes=RETRIEVAL_CONFIG["context_budget_bytes"],
            evidence_projection=RETRIEVAL_CONFIG["evidence_projection"],
        )
        started = time.perf_counter()
        try:
            result = await retriever.retrieve_with_trace(context, query)
        except Exception as exc:
            failure = {
                "case_id": case_id,
                "status": "FAILED",
                "attempted_at_utc": _utc_now(),
                "model_dispatches": 0,
                "error": _safe_error(exc),
            }
            _atomic_write_json(PRIVATE / "retrieval-failures" / f"{case_id}.json", failure)
            state["retrieval"]["failed_case_id"] = case_id
            state["last_error"] = failure["error"]
            state["phase"] = "retrieval_failed"
            _save_state(state)
            raise
        latency_ms = (time.perf_counter() - started) * 1_000

        candidate_rows: list[dict[str, Any]] = []
        for trace_row in result.trace.fusion.results[: RETRIEVAL_CONFIG["candidate_top_k"]]:
            chunk = chunks_by_id.get(trace_row.chunk_id)
            if chunk is None:
                raise MaterializeError(
                    f"candidate chunk is outside the snapshot source map: {case_id}"
                )
            candidate_rows.append(
                {
                    "rank": trace_row.rank,
                    "chunk_id": trace_row.chunk_id,
                    "source_id": chunk["source_id"],
                    "document_id": chunk["document_id"],
                    "document_revision_id": chunk["document_revision_id"],
                    "retrieval_score": trace_row.score,
                    "ordinal": chunk["ordinal"],
                    "locator": chunk["locator"],
                    "text": chunk["text"],
                }
            )
        if not candidate_rows:
            raise MaterializeError(f"production retriever returned no candidates: {case_id}")

        final_rows: list[dict[str, Any]] = []
        for item in result.evidence:
            source_id = source_by_chunk.get(item.chunk_id)
            document_source = document_names.get(item.document_id)
            if source_id is None or document_source is None or source_id != document_source:
                raise MaterializeError(f"final evidence source binding is broken: {case_id}")
            chunk = chunks_by_id[item.chunk_id]
            if item.document_revision_id != chunk["document_revision_id"]:
                raise MaterializeError(f"final evidence revision binding is broken: {case_id}")
            final_rows.append(
                {
                    "rank": len(final_rows) + 1,
                    "chunk_id": item.chunk_id,
                    "source_id": source_id,
                    "document_id": item.document_id,
                    "document_revision_id": item.document_revision_id,
                    "retrieval_score": item.retrieval_score,
                    "rerank_score": item.rerank_score,
                    "ordinal": item.metadata.get("ordinal"),
                    "locator": item.locator,
                    "metadata": item.metadata,
                    "text": item.text,
                }
            )
        coverage = _quote_coverage(question, candidate_rows, final_rows)
        output = {
            "schema_version": 1,
            "case_id": case_id,
            "bucket": question.get("bucket"),
            "answerability": question.get("answerability"),
            "query": question["input"],
            "knowledge_base_id": state["knowledge"]["knowledge_base_id"],
            "snapshot_id": state["knowledge"]["snapshot_id"],
            "retrieval_config": RETRIEVAL_CONFIG,
            "retrieval_identity": retrieval_identity,
            "candidate_evidence": candidate_rows,
            "final_evidence": final_rows,
            "trace": asdict(result.trace),
            "quote_coverage": coverage,
            "latency_ms": round(latency_ms, 3),
            "model_dispatches": 0,
            "tool_runtime_invoked": False,
            "context_budget_admission": "PENDING_NOT_CLAIMED",
            "agent_run_executed": False,
        }
        _atomic_write_json(path, output)
        state["retrieval"]["completed_case_ids"].append(case_id)
        completed.add(case_id)
        state["phase"] = "retrieving"
        _save_state(state)


def _retrieval_summary(cases: dict[str, Any]) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for question in cases["questions"]:
        path = _case_result_path(question["case_id"])
        if not path.is_file():
            raise MaterializeError(
                f"retrieval result missing at summary time: {question['case_id']}"
            )
        results.append(_read_json(path))

    buckets = sorted({result.get("bucket") for result in results})
    by_bucket: dict[str, dict[str, int]] = {}
    for bucket in buckets:
        selected = [result for result in results if result.get("bucket") == bucket]
        by_bucket[bucket] = {
            "cases": len(selected),
            "candidate_group_complete": sum(
                bool(result["quote_coverage"]["candidate_complete"]) for result in selected
            ),
            "final_group_complete": sum(
                bool(result["quote_coverage"]["final_complete"]) for result in selected
            ),
        }
    return {
        "scope": (
            "Direct production retriever output only. Exact quotes are checked after retrieval; "
            "gold labels are never included in the query."
        ),
        "cases": len(results),
        "candidate_top_k": RETRIEVAL_CONFIG["candidate_top_k"],
        "final_top_k": RETRIEVAL_CONFIG["final_top_k"],
        "candidate_group_complete": sum(
            bool(result["quote_coverage"]["candidate_complete"]) for result in results
        ),
        "final_group_complete": sum(
            bool(result["quote_coverage"]["final_complete"]) for result in results
        ),
        "by_bucket": by_bucket,
        "model_dispatches": 0,
        "tool_runtime_invocations": 0,
        "context_budget_admissions": "PENDING_NOT_CLAIMED",
        "quality_pass": None,
    }


def _write_retrieval_rollup(cases: dict[str, Any]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for question in cases["questions"]:
        path = _case_result_path(question["case_id"])
        if not path.is_file():
            raise MaterializeError(
                f"retrieval result missing at rollup time: {question['case_id']}"
            )
        rows.append(_read_json(path))
    _atomic_write_text(
        PRIVATE / "retrieval-results.jsonl",
        "".join(
            json.dumps(row, ensure_ascii=False, allow_nan=False, default=_json_default) + "\n"
            for row in rows
        ),
    )
    summary = _retrieval_summary(cases)
    _atomic_write_json(PRIVATE / "retrieval-summary.json", summary)
    return summary


def _result_document(
    *,
    state: dict[str, Any],
    manifest: dict[str, Any],
    cases: dict[str, Any],
    gpu: dict[str, Any] | None,
    retrieval_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    documents = state["documents"]
    chunk_count = sum(len(record.get("chunks", [])) for record in documents.values())
    completed = len(state["retrieval"]["completed_case_ids"])
    return {
        "schema_version": 1,
        "name": "support-refund-rag-S1-zero-generation",
        "status": "S1_PARTIAL",
        "operation_status": "SUCCEEDED" if completed == EXPECTED_CASES else "INCOMPLETE",
        "partial_reason": "Agent publication and ContextBudget admission are outside this slice.",
        "pending": PENDING_S1_ITEMS,
        "isolation": {
            "database_name": DATABASE_NAME,
            "database_url_persisted": "private/s1/state.json only; never printed",
            "collection_name": COLLECTION_NAME,
            "blob_root": BLOB_ROOT.relative_to(ROOT).as_posix(),
            "output_root": PRIVATE.relative_to(ROOT).as_posix(),
            "organization_name": ORG_NAME,
            "workspace_name": WORKSPACE_NAME,
        },
        "materialization": {
            "material_audit": "PASS",
            "manifest_documents": len(manifest["documents"]),
            "ready_documents": sum(
                record.get("ingestion_status") == "READY" for record in documents.values()
            ),
            "lifecycle_verified_documents": sum(
                bool(record.get("lifecycle_verified")) for record in documents.values()
            ),
            "chunk_count": chunk_count,
            "snapshot_id": state["knowledge"].get("snapshot_id"),
            "snapshot_hash": state["knowledge"].get("snapshot_hash"),
            "snapshot_item_count": state["knowledge"].get("snapshot_item_count"),
            "alembic_head": state["alembic"].get("head"),
            "checkpoint_status": state["checkpoint"].get("status"),
            "gpu": gpu,
        },
        "retrieval": {
            "planned_cases": EXPECTED_CASES,
            "completed_cases": completed,
            "config": RETRIEVAL_CONFIG,
            "summary": retrieval_summary,
            "direct_real_retriever": True,
            "fake_retriever": False,
            "final_model_admission": "PENDING_NOT_CLAIMED",
        },
        "generation_boundary": {
            "model_dispatches": 0,
            "model_gateway_initialized": False,
            "agent_version_created": False,
            "agent_run_executed": False,
            "write_tools_configured": False,
            "holdout_read": False,
            "seed_corpus_used": False,
        },
        "case_bucket_counts": dict(Counter(q.get("bucket") for q in cases["questions"])),
        "updated_at_utc": _utc_now(),
    }


async def _materialize(
    *,
    phase: str,
    resume: bool,
) -> dict[str, Any]:
    # This is intentionally the first real action: no database, Qdrant or blob
    # work happens before the offline material audit says PASS.
    manifest, cases = _require_material_audit()

    with _exclusive_lock():
        base_settings = Settings()
        database_url = _database_url(base_settings)
        settings = _target_settings(base_settings, database_url)
        state_exists = STATE_FILE.is_file()
        if state_exists and not resume:
            raise MaterializeError(
                "private/s1/state.json exists; pass --resume to continue this owned state"
            )

        if state_exists:
            state = _load_state()
            settings = _settings_from_snapshot(
                base_settings,
                settings_snapshot=state["frozen_settings"],
            )
            database_url = _stored_database_url(state)
            if settings.database_url != database_url or settings.qdrant_url != state["qdrant_url"]:
                raise MaterializeError("state connection settings are internally inconsistent")
            database_exists, collection_exists = await _preflight_owned_resources(settings)
            if not database_exists:
                raise MaterializeError("state exists but its fixed isolated database is missing")
        else:
            database_exists, collection_exists = await _preflight_resources(
                settings, state_exists=False
            )
            if database_exists or collection_exists:
                raise MaterializeError("orphaned S1 resources exist without ownership state")
            await _create_database(settings)
            state = _new_state(str(uuid4()), settings)
            _save_state(state)
            database_exists = True

        expected_head = _alembic_head(settings)
        _migrate_and_checkpoint(settings, database_url, state)
        await _assert_alembic_and_checkpoint(settings, expected_head)
        await _ensure_collection(settings, state, existed_at_preflight=collection_exists)

        engine, factory = create_database(settings.database_url)
        gpu: dict[str, Any] | None = None
        try:
            identities = await _ensure_workspace(factory, state)
            context = _context(identities, "materialization")
            knowledge_base = await _ensure_knowledge_base(factory, state, context)

            components = production_retrieval_components(settings)
            for component in (components.dense, components.sparse, components.reranker):
                warm = getattr(component, "warm", None)
                if warm is not None:
                    await asyncio.to_thread(warm)
            gpu = await asyncio.to_thread(_gpu_identity, components)
            retrieval_identity = _retrieval_identity(settings, components)
            state["gpu"] = gpu
            state["phase"] = "ingesting"
            _save_state(state)

            for manifest_document in manifest["documents"]:
                source_path = (DATA / manifest_document["path"]).resolve()
                source = source_path.read_text(encoding="utf-8")
                await _adopt_or_upload_document(
                    factory,
                    state=state,
                    context=context,
                    knowledge_base=knowledge_base,
                    manifest_document=manifest_document,
                    source=source,
                    settings=settings,
                    retrieval_identity=retrieval_identity,
                )

            await _apply_lifecycles(
                factory,
                state=state,
                context=context,
                knowledge_base=knowledge_base,
                manifest=manifest,
            )
            snapshot = await _create_or_resolve_snapshot(
                factory,
                state=state,
                context=context,
                knowledge_base=knowledge_base,
            )
            source_map = _persist_source_map(state, snapshot)
            _verify_retrieval_identity(source_map)
            _atomic_write_json(PRIVATE / "source-map.json", source_map)

            expected_points = sum(len(record["chunks"]) for record in state["documents"].values())
            actual_points = await asyncio.to_thread(_collection_count, settings)
            if actual_points != expected_points:
                raise MaterializeError(
                    "Qdrant point count does not match the persisted chunk map "
                    f"({actual_points} != {expected_points})"
                )
            state["qdrant_point_count"] = actual_points
            state["phase"] = "materialized"
            _save_state(state)

            retrieval_summary: dict[str, Any] | None = None
            if phase in {"all", "retrieve"}:
                retrieval_context = _context(identities, "retrieval")
                await _retrieve_cases(
                    factory,
                    state=state,
                    context=retrieval_context,
                    cases=cases,
                    source_map=source_map,
                    components=components,
                    retrieval_identity=retrieval_identity,
                    rrf_k=settings.knowledge_rrf_k,
                )
                retrieval_summary = _write_retrieval_rollup(cases)
                state["phase"] = "retrieval_complete"
            else:
                state["phase"] = "materialized"
            state["terminal_status"] = "S1_PARTIAL"
            state["state_interface_version"] = STATE_INTERFACE_VERSION
            if state.get("retrieval_config") != RETRIEVAL_CONFIG:
                raise MaterializeError("state retrieval configuration changed from frozen defaults")
            _save_state(state)

            result = _result_document(
                state=state,
                manifest=manifest,
                cases=cases,
                gpu=gpu,
                retrieval_summary=retrieval_summary,
            )
            _atomic_write_json(PRIVATE / "s1-result.json", result)
            state["last_error"] = None
            _save_state(state)
            return result
        except Exception as exc:
            state["last_error"] = _safe_error(exc, database_url)
            _save_state(state)
            raise
        finally:
            await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase",
        choices=("all", "materialize", "retrieve"),
        default="all",
        help="materialize only, retrieve against an existing owned snapshot, or both",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="continue only when private/s1/state.json already proves ownership",
    )
    args = parser.parse_args()
    try:
        result = asyncio.run(_materialize(phase=args.phase, resume=args.resume))
    except MaterializeError as exc:
        print(json.dumps({"status": "REFUSED_OR_FAILED", "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1) from exc
    except Exception as exc:
        print(
            json.dumps(
                {"status": "FAILED", "error": _safe_error(exc)},
                ensure_ascii=False,
            )
        )
        raise SystemExit(1) from exc
    print(
        json.dumps(
            {
                "status": result["status"],
                "operation_status": result["operation_status"],
                "documents": result["materialization"]["ready_documents"],
                "snapshot_item_count": result["materialization"]["snapshot_item_count"],
                "completed_cases": result["retrieval"]["completed_cases"],
                "model_dispatches": 0,
                "output_root": result["isolation"]["output_root"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
