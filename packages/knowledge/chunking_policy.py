"""Revision-scoped, versioned policies for the production ingestion worker."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from packages.core.config.settings import Settings
from packages.knowledge.adaptive_chunking import build_adaptive_chunks
from packages.knowledge.chunking import ChunkCandidate, build_deterministic_chunks
from packages.knowledge.parser import ParsedDocument
from packages.knowledge.structural_chunking import build_structural_chunks


class ChunkingStrategy(StrEnum):
    FIXED = "fixed_v1"
    STRUCTURAL = "structural_v1"
    ADAPTIVE = "adaptive_v1"


@dataclass(frozen=True)
class ChunkingPolicy:
    strategy: ChunkingStrategy
    size_chars: int
    overlap_chars: int

    @classmethod
    def resolve(
        cls, strategy: ChunkingStrategy, media_type: str, settings: Settings
    ) -> ChunkingPolicy:
        limit = settings.knowledge_chunk_size_chars
        if strategy == ChunkingStrategy.FIXED:
            return cls(strategy, limit, settings.knowledge_chunk_overlap_chars)
        if strategy == ChunkingStrategy.STRUCTURAL:
            return cls(strategy, limit, 0)
        # Shorter windows localize a section or page citation. Delimited tables
        # retain the full configured limit to accommodate a complete wide row.
        if media_type == "text/markdown":
            limit = max(1, limit * 3 // 4)
        elif media_type in {"application/pdf", "application/json"}:
            limit = max(1, limit * 2 // 3)
        return cls(strategy, limit, 0)

    def build(
        self, revision_id: UUID, parsed: ParsedDocument, *, media_type: str
    ) -> tuple[ChunkCandidate, ...]:
        if self.strategy == ChunkingStrategy.ADAPTIVE:
            return build_adaptive_chunks(
                revision_id, parsed, media_type=media_type, chunk_size_chars=self.size_chars
            )
        if self.strategy == ChunkingStrategy.STRUCTURAL:
            return build_structural_chunks(revision_id, parsed, chunk_size_chars=self.size_chars)
        return build_deterministic_chunks(
            revision_id,
            parsed,
            chunk_size_chars=self.size_chars,
            chunk_overlap_chars=self.overlap_chars,
        )
