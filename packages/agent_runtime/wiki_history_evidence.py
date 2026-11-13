"""Database-backed recovery of compact Wiki evidence from completed history."""

from __future__ import annotations

import hashlib
from collections.abc import Collection, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from packages.agent_runtime.history_evidence import (
    FORBIDDEN,
    HISTORY_EVIDENCE_INVALID,
    validate_history_evidence,
)
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.knowledge.chunking import normalize_chunk_text
from packages.knowledge.evidence_records import MAX_EVIDENCE_EXCERPT_CHARS
from packages.knowledge.models import DocumentChunk
from packages.model_gateway.contracts import ModelMessage
from packages.wiki.evidence_envelope import EVIDENCE_ORIGIN_WIKI_DERIVED
from packages.wiki.models import WikiCompilation
from packages.wiki.service import WikiService, publication_hash

_REQUIRED_PERMISSIONS = frozenset({"knowledge_run", "tool_run"})
_BINDING_MODES = frozenset({"PINNED", "LATEST"})


@dataclass(frozen=True, slots=True)
class _FrozenPublication:
    knowledge_base_id: UUID
    publication_id: UUID
    publication_hash: str


@dataclass(frozen=True, slots=True)
class _ResolvedPublication:
    knowledge_base_id: UUID
    publication_id: UUID
    publication_hash: str
    snapshot_id: UUID
    snapshot_hash: str
    pages: tuple[Mapping[str, Any], ...]
    source_revision_ids: frozenset[UUID]


async def validate_wiki_history_evidence(
    session: AsyncSession,
    context: WorkspaceExecutionContext,
    *,
    workspace_id: UUID,
    messages: Sequence[ModelMessage],
    registry: Mapping[str, Any],
    source_knowledge_snapshots: Sequence[Mapping[str, Any]],
    current_knowledge_snapshots: Sequence[Mapping[str, Any]],
    source_wiki_publications: Sequence[Mapping[str, Any]],
    current_wiki_publications: Sequence[Mapping[str, Any]],
    permissions: Collection[str],
    publication_cache: MutableMapping[tuple[UUID, UUID, UUID, str], Any] | None = None,
) -> dict[str, dict[str, Any]]:
    """Return the exact visible Wiki exchange only when current DB state proves it.

    The source and current Runs must freeze the same publication id and hash.
    The database row supplies the immutable source snapshot; no historical
    record is allowed to nominate its own snapshot. The existing Wiki source
    ACL checks snapshot membership, revocation and tenant scope. The full page
    set is hashed once against the frozen publication hash. One bounded batch
    query then reads only chunks cited by the visible envelope; no unrelated
    chunk in the frozen source scope is read or hashed. ``publication_cache``
    must be fresh for one history-validation call; it is not a cross-Run cache
    of database or permission evidence.
    """

    _require_permissions(context, permissions)
    try:
        if UUID(str(context.workspace_id)) != workspace_id:
            raise _invalid()
        source = _single_publication(source_wiki_publications)
        current = _single_publication(current_wiki_publications)
        if source != current:
            raise _invalid()

        cache = publication_cache if publication_cache is not None else {}
        cache_key = (
            workspace_id,
            current.knowledge_base_id,
            current.publication_id,
            current.publication_hash,
        )
        resolved = cache.get(cache_key)
        if resolved is None:
            publication = await session.scalar(
                select(WikiCompilation).where(
                    WikiCompilation.workspace_id == workspace_id,
                    WikiCompilation.knowledge_base_id == current.knowledge_base_id,
                    WikiCompilation.id == current.publication_id,
                )
            )
            if (
                publication is None
                or publication.status != "PUBLISHED"
                or publication.content_hash != current.publication_hash
                or publication.source_snapshot_hash is None
            ):
                raise _invalid()
            resolved = await _load_resolved_publication(session, context, publication)
            cache[cache_key] = resolved
        if not isinstance(resolved, _ResolvedPublication):
            raise _invalid()
        verified = validate_history_evidence(
            messages,
            registry,
            source_knowledge_snapshots,
            current_knowledge_snapshots,
            source_wiki_publications,
            current_wiki_publications,
            permissions,
            resolved_wiki_snapshots=(
                {
                    "knowledge_base_id": str(resolved.knowledge_base_id),
                    "wiki_publication_id": str(resolved.publication_id),
                    "publication_hash": resolved.publication_hash,
                    "snapshot_id": str(resolved.snapshot_id),
                    "snapshot_hash": resolved.snapshot_hash,
                },
            ),
        )
        await _verify_wiki_records(session, verified, resolved, workspace_id=workspace_id)
        return verified
    except AgentHubError as error:
        if error.code == FORBIDDEN:
            raise
        if error.code == HISTORY_EVIDENCE_INVALID:
            raise
        raise _invalid() from None
    except (KeyError, TypeError, ValueError):
        raise _invalid() from None


def _single_publication(values: Sequence[Mapping[str, Any]]) -> _FrozenPublication:
    if isinstance(values, (str, bytes, bytearray, Mapping)) or not isinstance(values, Sequence):
        raise _invalid()
    bindings = tuple(values)
    if len(bindings) != 1 or not isinstance(bindings[0], Mapping):
        raise _invalid()
    binding = bindings[0]
    mode = binding.get("binding_mode")
    if not isinstance(mode, str) or mode not in _BINDING_MODES:
        raise _invalid()
    return _FrozenPublication(
        knowledge_base_id=_uuid(binding.get("knowledge_base_id")),
        publication_id=_uuid(binding.get("wiki_publication_id")),
        publication_hash=_sha256(binding.get("publication_hash")),
    )


async def _verify_wiki_records(
    session: AsyncSession,
    registry: Mapping[str, Mapping[str, Any]],
    publication: _ResolvedPublication,
    *,
    workspace_id: UUID,
) -> None:
    pages = {page.get("page_key"): page for page in publication.pages}
    if len(pages) != len(publication.pages):
        raise _invalid()

    records: list[tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]] = []
    keys: set[tuple[UUID, str]] = set()
    for entry in registry.values():
        reference = entry.get("reference")
        record = entry.get("record")
        if not isinstance(reference, Mapping) or not isinstance(record, Mapping):
            raise _invalid()
        if record.get("origin") != EVIDENCE_ORIGIN_WIKI_DERIVED:
            continue
        page_id = _text(record.get("wiki_page_id"))
        page = pages.get(page_id)
        if not isinstance(page, Mapping):
            raise _invalid()
        claim = _matching_claim(publication, record, reference, page)
        _verify_source_revisions(record, page)
        if any(
            _uuid(revision_id) not in publication.source_revision_ids
            for revision_id in record["source_revisions"]
        ):
            raise _invalid()
        revision_id = _uuid(reference.get("document_revision_id"))
        chunk_id = _text(reference.get("chunk_id"))
        if revision_id not in publication.source_revision_ids:
            raise _invalid()
        records.append((reference, record, claim))
        keys.add((revision_id, chunk_id))

    if not keys:
        return
    ordered_keys = sorted(keys, key=lambda value: (str(value[0]), value[1]))
    rows = list(
        await session.scalars(
            select(DocumentChunk).where(
                DocumentChunk.workspace_id == workspace_id,
                DocumentChunk.knowledge_base_id == publication.knowledge_base_id,
                tuple_(
                    DocumentChunk.document_revision_id,
                    DocumentChunk.chunk_id,
                ).in_(ordered_keys),
            )
        )
    )
    chunks = {(row.document_revision_id, row.chunk_id): row for row in rows}
    if len(chunks) != len(rows) or set(chunks) != keys:
        raise _invalid()

    for reference, record, claim in records:
        revision_id = _uuid(reference.get("document_revision_id"))
        chunk_id = _text(reference.get("chunk_id"))
        chunk = chunks[(revision_id, chunk_id)]
        if chunk is None:
            raise _invalid()
        normalized = normalize_chunk_text(chunk.text)
        if hashlib.sha256(normalized.encode("utf-8")).hexdigest() != chunk.normalized_content_hash:
            raise _invalid()
        if (
            str(chunk.document_id) != _text(record.get("document_id"))
            or chunk.document_revision_id != revision_id
            or chunk.chunk_id != chunk_id
            or dict(chunk.locator or {}) != dict(claim.get("locator") or {})
            or _text(record.get("quote")) not in chunk.text
        ):
            raise _invalid()


async def _load_resolved_publication(
    session: AsyncSession,
    context: WorkspaceExecutionContext,
    publication: WikiCompilation,
) -> _ResolvedPublication:
    """Apply the existing Wiki source ACL and integrity checks in one read pass."""

    revisions = await WikiService().sources(session, context, publication)
    revision_ids = tuple(revision.id for revision in revisions)
    if not revision_ids or len(set(revision_ids)) != len(revision_ids):
        raise _invalid()
    page_rows = tuple(await WikiService._page_rows(session, publication))

    page_ids: set[str] = set()
    for page in page_rows:
        if not isinstance(page, Mapping):
            raise _invalid()
        page_id = _text(page.get("page_key"))
        if page_id in page_ids:
            raise _invalid()
        page_ids.add(page_id)
        claims = page.get("claims")
        if not isinstance(claims, Sequence) or isinstance(claims, (str, bytes, bytearray, Mapping)):
            raise _invalid()
        if any(not isinstance(claim, Mapping) for claim in claims):
            raise _invalid()

    if publication_hash(publication, page_rows) != publication.content_hash:
        raise _invalid()
    return _ResolvedPublication(
        knowledge_base_id=publication.knowledge_base_id,
        publication_id=publication.id,
        publication_hash=_sha256(publication.content_hash),
        snapshot_id=_uuid(publication.source_snapshot_id),
        snapshot_hash=_sha256(publication.source_snapshot_hash),
        pages=page_rows,
        source_revision_ids=frozenset(revision_ids),
    )


def _matching_claim(
    publication: _ResolvedPublication,
    record: Mapping[str, Any],
    reference: Mapping[str, Any],
    page: Mapping[str, Any],
) -> Mapping[str, Any]:
    publication_id = _uuid(record.get("publication_id"))
    publication_hash = _sha256(record.get("publication_hash"))
    knowledge_base_id = _uuid(reference.get("knowledge_base_id"))
    snapshot_id = _uuid(reference.get("snapshot_id"))
    revision_id = _uuid(reference.get("document_revision_id"))
    chunk_id = _text(reference.get("chunk_id"))
    quote = _text(record.get("quote"))
    if (
        publication_id != publication.publication_id
        or publication_hash != publication.publication_hash
        or knowledge_base_id != publication.knowledge_base_id
        or snapshot_id != publication.snapshot_id
        or record.get("document_revision_id") != str(revision_id)
        or record.get("chunk_id") != chunk_id
        or record.get("snippet") != quote[:MAX_EVIDENCE_EXCERPT_CHARS]
    ):
        raise _invalid()

    matches = [
        claim
        for claim in page.get("claims") or ()
        if isinstance(claim, Mapping)
        and claim.get("chunk_id") == chunk_id
        and str(claim.get("document_revision_id")) == str(revision_id)
        and claim.get("document_id") == record.get("document_id")
        and claim.get("quote") == quote
        and dict(claim.get("locator") or {}) == dict(record.get("locator") or {})
        and bool(claim.get("conflict", False)) is record.get("conflict")
    ]
    if len(matches) != 1:
        raise _invalid()
    claim = matches[0]

    return claim


def _verify_source_revisions(
    record: Mapping[str, Any],
    page: Mapping[str, Any],
) -> None:
    revisions = record.get("source_revisions")
    if (
        not isinstance(revisions, list)
        or not revisions
        or any(not isinstance(value, str) or not value.strip() for value in revisions)
        or any(str(_uuid(value)) != value for value in revisions)
        or revisions != sorted(set(revisions))
    ):
        raise _invalid()
    expected = sorted(
        {
            _text(claim.get("document_revision_id"))
            for claim in page.get("claims") or ()
            if isinstance(claim, Mapping)
        }
    )
    if revisions != expected:
        raise _invalid()


def _require_permissions(
    context: WorkspaceExecutionContext,
    permissions: Collection[str],
) -> None:
    if isinstance(permissions, (str, bytes, bytearray, Mapping)) or not isinstance(
        permissions, Collection
    ):
        raise _forbidden()
    try:
        granted = set(permissions)
    except TypeError:
        raise _forbidden() from None
    if any(not isinstance(permission, str) for permission in granted):
        raise _forbidden()
    if not _REQUIRED_PERMISSIONS <= granted:
        raise _forbidden()
    context_permissions = context.permissions
    if isinstance(context_permissions, (str, bytes, bytearray, Mapping)) or not isinstance(
        context_permissions, Collection
    ):
        raise _forbidden()
    try:
        context_granted = set(context_permissions)
    except TypeError:
        raise _forbidden() from None
    if not _REQUIRED_PERMISSIONS <= context_granted or not getattr(context, "user_id", None):
        raise _forbidden()


def _uuid(value: Any) -> UUID:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise _invalid() from None


def _sha256(value: Any) -> str:
    text = _text(value)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise _invalid()
    return text


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid()
    return value


def _invalid() -> AgentHubError:
    return AgentHubError(
        HISTORY_EVIDENCE_INVALID,
        "Historical Wiki evidence is unavailable.",
        422,
    )


def _forbidden() -> AgentHubError:
    return AgentHubError(
        FORBIDDEN,
        "Historical Wiki evidence is not authorized.",
        403,
    )


__all__ = ["validate_wiki_history_evidence"]
