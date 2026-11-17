"""Exact document encodings, scoped to one workspace and one immutable encoder.

The v1 binary payload uses network-order IEEE binary64 values (sparse: uint32,
binary64 pairs). Neither rounding to float32 nor a normalized text hash is safe
here: fresh and replayed records must use the same inputs and values.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import struct
from collections.abc import Callable, Sequence
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from packages.knowledge.contracts import KnowledgeProviderError, SparseEncoding
from packages.knowledge.models import DocumentVectorCache as Cache


def encoder_fingerprint(encoder: object) -> str | None:
    try:
        method = getattr(encoder, "document_cache_fingerprint", None)
        value = method() if callable(method) else None
    except Exception:
        # Adapters may still encode even when their runtime identity is unavailable.
        return None
    return value if isinstance(value, str) and re.fullmatch("[a-f0-9]{64}", value) else None


def _invalid() -> KnowledgeProviderError:
    return KnowledgeProviderError("INVALID_PROVIDER_RESULT", "Invalid document vector data.")


def _pack(vector, dimension: int | None) -> bytes:
    if dimension is not None:
        if dimension < 1 or len(vector) != dimension or not all(map(math.isfinite, vector)):
            raise _invalid()
        return struct.pack(f"!{dimension}d", *vector)
    if not isinstance(vector, SparseEncoding) or len(vector.indices) != len(vector.values):
        raise _invalid()
    previous = -1
    output = bytearray()
    for index, value in zip(vector.indices, vector.values, strict=True):
        if (
            type(index) is not int
            or not previous < index <= 0xFFFFFFFF
            or not math.isfinite(value)
            or value < 0
        ):
            raise _invalid()
        output.extend(struct.pack("!Id", index, value))
        previous = index
    return bytes(output)


def _unpack(payload: bytes, checksum: bytes, dimension: int | None):
    if hashlib.sha256(payload).digest() != checksum:
        raise _invalid()
    if dimension is not None:
        if dimension < 1 or len(payload) != dimension * 8:
            raise _invalid()
        vector = struct.unpack(f"!{dimension}d", payload)
        if not all(map(math.isfinite, vector)):
            raise _invalid()
        return vector
    if len(payload) % 12:
        raise _invalid()
    pairs = tuple(struct.iter_unpack("!Id", payload))
    indices = tuple(i for i, _ in pairs)
    values = tuple(v for _, v in pairs)
    if indices != tuple(sorted(set(indices))) or not all(
        math.isfinite(v) and v >= 0 for v in values
    ):
        raise _invalid()
    return SparseEncoding(indices, values)


async def resolve_document_vectors(
    factory,
    workspace_id: UUID,
    texts: Sequence[str],
    encode: Callable,
    *,
    modality: str,
    fingerprint: str | None,
    dimension: int | None = None,
):
    """Resolve one modality in input order. A None identity always does a full encode."""
    if not fingerprint:
        return await asyncio.to_thread(encode, texts)
    keys = [hashlib.sha256(text.encode("utf-8")).hexdigest() for text in texts]
    distinct = dict(zip(keys, texts, strict=True))
    scope = (
        Cache.workspace_id == workspace_id,
        Cache.modality == modality,
        Cache.encoder_fingerprint == fingerprint,
    )
    query = select(Cache.text_hash, Cache.vector, Cache.vector_sha256).where(*scope)

    async def read(session, hashes, *, evict_invalid=False):
        rows = await session.execute(query.where(Cache.text_hash.in_(hashes)))
        valid = {}
        for key, payload, checksum in sorted(rows):
            try:
                valid[key] = _unpack(payload, checksum, dimension)
            except KnowledgeProviderError:
                if not evict_invalid:
                    raise
                # Delete only the invalid version we actually observed. A
                # concurrent repair may have already installed a good value.
                await session.execute(
                    delete(Cache).where(
                        *scope,
                        Cache.text_hash == key,
                        Cache.vector == payload,
                        Cache.vector_sha256 == checksum,
                    )
                )
        return valid

    async with factory() as session, session.begin():
        vectors = await read(session, list(distinct), evict_invalid=True)
    missing = [k for k in distinct if k not in vectors]
    if missing:
        # No database transaction or connection is held over model inference.
        encoded = await asyncio.to_thread(encode, [distinct[k] for k in missing])
        if len(encoded) != len(missing):
            raise _invalid()
        rows = []
        for key, vector in zip(missing, encoded, strict=True):
            payload = _pack(vector, dimension)
            rows.append(
                dict(
                    workspace_id=workspace_id,
                    modality=modality,
                    encoder_fingerprint=fingerprint,
                    text_hash=key,
                    vector=payload,
                    vector_sha256=hashlib.sha256(payload).digest(),
                )
            )
        # Consistent acquisition order prevents lock-order inversion between batches.
        # A contender never overwrites the first committed value, and always reads
        # that winner (even if its own batch produced a slightly different float).
        async with factory() as session, session.begin():
            await session.execute(
                insert(Cache.__table__)
                .values(sorted(rows, key=lambda row: row["text_hash"]))
                .on_conflict_do_nothing(),
            )
            winners = await read(session, missing)
            if len(winners) != len(missing):
                raise _invalid()
            vectors.update(winners)
    return tuple(vectors[k] for k in keys)
