"""Qdrant adapter; Qdrant SDK types do not cross this module boundary."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from qdrant_client import QdrantClient, models

from packages.knowledge.contracts import (
    KnowledgeProviderError,
    SparseEncoding,
    VectorRecord,
    VectorScope,
    VectorSearchHit,
)


class QdrantVectorIndex:
    def __init__(
        self,
        *,
        url: str,
        collection_name: str,
        dense_vector_size: int,
        timeout_seconds: float = 10,
        client: QdrantClient | None = None,
    ) -> None:
        self.collection_name = collection_name
        self.dense_vector_size = dense_vector_size
        self._client = client or QdrantClient(url=url, timeout=int(timeout_seconds))

    @staticmethod
    def _expected_vectors(
        size: int,
    ) -> tuple[dict[str, models.VectorParams], dict[str, models.SparseVectorParams]]:
        return (
            {"dense": models.VectorParams(size=size, distance=models.Distance.COSINE)},
            {"sparse": models.SparseVectorParams(modifier=models.Modifier.IDF)},
        )

    @staticmethod
    def _vector_params_match(actual: Any, expected_size: int) -> bool:
        if not isinstance(actual, dict) or set(actual) != {"dense"}:
            return False
        dense = actual["dense"]
        distance = getattr(getattr(dense, "distance", None), "value", dense.distance)
        return dense.size == expected_size and str(distance).lower() == "cosine"

    @staticmethod
    def _sparse_params_match(actual: Any) -> bool:
        if not isinstance(actual, dict) or set(actual) != {"sparse"}:
            return False
        sparse = actual["sparse"]
        if sparse is None:
            return False
        if isinstance(sparse, Mapping):
            if "modifier" not in sparse:
                return True
            modifier = sparse["modifier"]
        else:
            # Qdrant versions before sparse modifier exposure may omit the field.
            if not hasattr(sparse, "modifier"):
                return True
            modifier = sparse.modifier
        value = getattr(modifier, "value", modifier)
        return str(value).lower() == models.Modifier.IDF.value

    def _validate_schema(self, info: Any) -> None:
        params = info.config.params
        if not self._vector_params_match(params.vectors, self.dense_vector_size):
            raise KnowledgeProviderError(
                "QDRANT_SCHEMA_MISMATCH",
                "The Qdrant dense vector schema is incompatible.",
            )
        if not self._sparse_params_match(params.sparse_vectors):
            raise KnowledgeProviderError(
                "QDRANT_SCHEMA_MISMATCH",
                "The Qdrant sparse vector schema is incompatible.",
            )

    def ensure_collection(self) -> None:
        try:
            if not self._client.collection_exists(self.collection_name):
                vectors, sparse_vectors = self._expected_vectors(self.dense_vector_size)
                self._client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=vectors,
                    sparse_vectors_config=sparse_vectors,
                )
            self._validate_schema(self._client.get_collection(self.collection_name))
        except KnowledgeProviderError:
            raise
        except Exception as exc:
            raise KnowledgeProviderError(
                "QDRANT_UNAVAILABLE",
                "The knowledge vector index is unavailable.",
            ) from exc

    @staticmethod
    def _validate_record(record: VectorRecord, expected_size: int) -> None:
        if len(record.dense) != expected_size or not all(
            math.isfinite(value) for value in record.dense
        ):
            raise KnowledgeProviderError(
                "INVALID_DENSE_VECTOR",
                "The dense vector is invalid.",
            )
        if len(record.sparse.indices) != len(record.sparse.values):
            raise KnowledgeProviderError(
                "INVALID_SPARSE_VECTOR",
                "The sparse vector is invalid.",
            )
        if tuple(sorted(set(record.sparse.indices))) != record.sparse.indices or any(
            index < 0 or not math.isfinite(value) or value < 0
            for index, value in zip(record.sparse.indices, record.sparse.values, strict=True)
        ):
            raise KnowledgeProviderError(
                "INVALID_SPARSE_VECTOR",
                "The sparse vector is invalid.",
            )

    def upsert(self, records: Sequence[VectorRecord]) -> None:
        for record in records:
            self._validate_record(record, self.dense_vector_size)
        points = [
            models.PointStruct(
                id=record.point_id,
                vector={
                    "dense": list(record.dense),
                    "sparse": models.SparseVector(
                        indices=list(record.sparse.indices),
                        values=list(record.sparse.values),
                    ),
                },
                payload=record.payload,
            )
            for record in records
        ]
        if not points:
            return
        try:
            self._client.upsert(self.collection_name, points=points, wait=True)
        except Exception as exc:
            raise KnowledgeProviderError(
                "QDRANT_UNAVAILABLE",
                "The knowledge vector index is unavailable.",
            ) from exc

    @staticmethod
    def _scope_filter(scope: VectorScope) -> models.Filter:
        return models.Filter(
            must=[
                models.FieldCondition(
                    key="workspace_id",
                    match=models.MatchValue(value=scope.workspace_id),
                ),
                models.FieldCondition(
                    key="knowledge_base_id",
                    match=models.MatchValue(value=scope.knowledge_base_id),
                ),
                models.FieldCondition(
                    key="document_revision_id",
                    match=models.MatchAny(any=list(scope.document_revision_ids)),
                ),
            ]
        )

    @staticmethod
    def _hits(response: Any) -> tuple[VectorSearchHit, ...]:
        return tuple(
            VectorSearchHit(
                point_id=str(point.id),
                score=float(point.score),
                payload=dict(point.payload or {}),
            )
            for point in response.points
        )

    def dense_search(
        self,
        query: Sequence[float],
        *,
        scope: VectorScope,
        limit: int,
    ) -> tuple[VectorSearchHit, ...]:
        if not scope.document_revision_ids:
            return ()
        if len(query) != self.dense_vector_size or not all(math.isfinite(value) for value in query):
            raise KnowledgeProviderError("INVALID_DENSE_VECTOR", "The dense vector is invalid.")
        try:
            response = self._client.query_points(
                self.collection_name,
                query=list(query),
                using="dense",
                query_filter=self._scope_filter(scope),
                limit=limit,
                with_payload=True,
                with_vectors=False,
            )
            return self._hits(response)
        except Exception as exc:
            raise KnowledgeProviderError(
                "QDRANT_UNAVAILABLE",
                "The knowledge vector index is unavailable.",
            ) from exc

    def sparse_search(
        self,
        query: SparseEncoding,
        *,
        scope: VectorScope,
        limit: int,
    ) -> tuple[VectorSearchHit, ...]:
        if not scope.document_revision_ids:
            return ()
        if (
            len(query.indices) != len(query.values)
            or tuple(sorted(set(query.indices))) != query.indices
            or any(
                index < 0 or not math.isfinite(value) or value < 0
                for index, value in zip(query.indices, query.values, strict=True)
            )
        ):
            raise KnowledgeProviderError("INVALID_SPARSE_VECTOR", "The sparse vector is invalid.")
        try:
            response = self._client.query_points(
                self.collection_name,
                query=models.SparseVector(indices=list(query.indices), values=list(query.values)),
                using="sparse",
                query_filter=self._scope_filter(scope),
                limit=limit,
                with_payload=True,
                with_vectors=False,
            )
            return self._hits(response)
        except Exception as exc:
            raise KnowledgeProviderError(
                "QDRANT_UNAVAILABLE",
                "The knowledge vector index is unavailable.",
            ) from exc


__all__ = ["QdrantVectorIndex"]
