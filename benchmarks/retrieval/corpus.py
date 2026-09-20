"""Shared production-semantics corpus seeding helpers for retrieval benchmarks."""

from __future__ import annotations

from uuid import UUID, uuid5

from benchmarks.retrieval.schema import CorpusDocument
from packages.knowledge.chunking import ChunkCandidate, build_deterministic_chunks
from packages.knowledge.parser import ParsedBlock, ParsedDocument

BENCHMARK_REVISION_NAMESPACE = UUID("6f2e0d1e-5c1b-4b92-9f7d-1b9f8f3f1a62")
BENCHMARK_CHUNK_SIZE_CHARS = 450
BENCHMARK_CHUNK_OVERLAP_CHARS = 80


def benchmark_revision_id(document: CorpusDocument) -> UUID:
    """Return a stable fixture revision identity for static benchmark artifacts."""

    return uuid5(
        BENCHMARK_REVISION_NAMESPACE,
        f"{document.document_key}:{document.revision_key}",
    )


def build_benchmark_chunks(
    document: CorpusDocument, *, revision_id: UUID | None = None
) -> tuple[ChunkCandidate, ...]:
    """Build chunks through the production parser/chunker and its chunk-id contract."""

    parsed = ParsedDocument(
        blocks=tuple(
            ParsedBlock(
                text=section.text,
                locator={"type": "section", "section_key": section.section_key},
            )
            for section in document.sections
        )
    )
    return build_deterministic_chunks(
        revision_id or benchmark_revision_id(document),
        parsed,
        chunk_size_chars=BENCHMARK_CHUNK_SIZE_CHARS,
        chunk_overlap_chars=BENCHMARK_CHUNK_OVERLAP_CHARS,
    )


def chunk_ids_by_section(document: CorpusDocument) -> dict[str, tuple[str, ...]]:
    """Return formal chunk IDs grouped by their production locator."""

    grouped: dict[str, list[str]] = {section.section_key: [] for section in document.sections}
    for candidate in build_benchmark_chunks(document):
        section_key = candidate.locator.get("section_key")
        if isinstance(section_key, str):
            grouped.setdefault(section_key, []).append(candidate.chunk_id)
    return {key: tuple(value) for key, value in grouped.items()}


__all__ = [
    "BENCHMARK_CHUNK_OVERLAP_CHARS",
    "BENCHMARK_CHUNK_SIZE_CHARS",
    "BENCHMARK_REVISION_NAMESPACE",
    "benchmark_revision_id",
    "build_benchmark_chunks",
    "chunk_ids_by_section",
]
