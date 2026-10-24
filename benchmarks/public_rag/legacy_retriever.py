"""Frozen pre-optimization SQL control, used only in the paired benchmark.

The method below is the production chunk query from commit 23f1a38. Keep the
production ranking/model path shared; only the candidate hydration SQL differs.
"""

from __future__ import annotations

import time
from uuid import UUID

from sqlalchemy import select

from packages.knowledge.models import Document, DocumentChunk, DocumentRevision
from packages.knowledge.retrieval import HybridKnowledgeRetriever


class LegacySqlRetriever(HybridKnowledgeRetriever):
    async def _load_chunks(
        self,
        session,
        *,
        workspace_id,
        knowledge_base_id,
        revision_ids,
        chunk_ids,
        snapshot_id=None,
    ):
        if not revision_ids or not chunk_ids:
            return {}
        result = await session.execute(
            select(DocumentChunk, Document, DocumentRevision)
            .join(
                Document,
                (Document.id == DocumentChunk.document_id)
                & (Document.workspace_id == DocumentChunk.workspace_id)
                & (Document.knowledge_base_id == DocumentChunk.knowledge_base_id),
            )
            .join(
                DocumentRevision,
                (DocumentRevision.id == DocumentChunk.document_revision_id)
                & (DocumentRevision.workspace_id == DocumentChunk.workspace_id)
                & (DocumentRevision.knowledge_base_id == DocumentChunk.knowledge_base_id),
            )
            .where(
                DocumentChunk.workspace_id == workspace_id,
                DocumentChunk.knowledge_base_id == knowledge_base_id,
                DocumentChunk.document_revision_id.in_(tuple(UUID(item) for item in revision_ids)),
                DocumentChunk.chunk_id.in_(chunk_ids),
            )
        )
        return {chunk.chunk_id: (chunk, document, revision) for chunk, document, revision in result}


class DatabaseTimingMixin:
    """Equal instrumentation on both implementations, outside product code."""

    async def _snapshot_scope(self, *args, **kwargs):
        started = time.perf_counter()
        result = await super()._snapshot_scope(*args, **kwargs)
        self.snapshot_scope_ms = (time.perf_counter() - started) * 1000
        return result

    async def _load_chunks(self, *args, **kwargs):
        started = time.perf_counter()
        result = await super()._load_chunks(*args, **kwargs)
        self.chunk_load_ms = (time.perf_counter() - started) * 1000
        return result


class BaselineRetriever(DatabaseTimingMixin, LegacySqlRetriever):
    pass


class OptimizedRetriever(DatabaseTimingMixin, HybridKnowledgeRetriever):
    pass
