"""OpenAI-compatible HTTP dense embedding adapter (e.g. DashScope Qwen3.7)."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

from packages.core.canonical.json_hash import canonical_json_hash
from packages.knowledge.contracts import KnowledgeProviderError


def _normalize_vector(values: tuple[float, ...]) -> tuple[float, ...]:
    norm = math.sqrt(sum(value * value for value in values))
    if not math.isfinite(norm) or norm <= 0.0:
        raise KnowledgeProviderError(
            "EMBEDDER_INVALID_VECTOR",
            "The dense embedding provider returned a zero-norm vector.",
        )
    return tuple(value / norm for value in values)


class OpenAICompatibleDenseEmbedder:
    """HTTP adapter implementing :class:`DenseEmbedder` for OpenAI-compatible gateways."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model_name: str,
        expected_dimension: int = 1024,
        batch_size: int = 20,
        timeout_seconds: float = 30.0,
        normalize_embeddings: bool = True,
        client: httpx.Client | None = None,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url must be a non-empty string")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("api_key must be a non-empty string")
        if not isinstance(model_name, str) or not model_name.strip():
            raise ValueError("model_name must be a non-empty string")
        if (
            isinstance(expected_dimension, bool)
            or not isinstance(expected_dimension, int)
            or expected_dimension < 1
        ):
            raise ValueError("expected_dimension must be a positive integer")
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise ValueError("batch_size must be a positive integer")
        self.base_url = base_url.strip().rstrip("/")
        self.api_key = api_key.strip()
        self.model_name = model_name.strip()
        self.expected_dimension = expected_dimension
        self.batch_size = batch_size
        self.timeout_seconds = float(timeout_seconds)
        self.normalize_embeddings = normalize_embeddings
        self._client = client

    @property
    def dimension(self) -> int:
        return self.expected_dimension

    def document_cache_fingerprint(self) -> str:
        return canonical_json_hash(
            {
                "encoder": "openai-compatible-dense-v1",
                "model": self.model_name,
                "dimension": self.expected_dimension,
                "normalize_embeddings": self.normalize_embeddings,
            }
        )

    def _endpoint(self) -> str:
        if self.base_url.endswith("/embeddings"):
            return self.base_url
        return f"{self.base_url}/embeddings"

    def _validate(self, vector: Any) -> tuple[float, ...]:
        if not isinstance(vector, Sequence) or isinstance(vector, (str, bytes)):
            raise KnowledgeProviderError(
                "EMBEDDER_INVALID_VECTOR",
                "The dense embedding provider returned an invalid vector.",
            )
        try:
            values = tuple(float(value) for value in vector)
        except (TypeError, ValueError) as exc:
            raise KnowledgeProviderError(
                "EMBEDDER_INVALID_VECTOR",
                "The dense embedding provider returned a non-numeric vector.",
            ) from exc
        if len(values) != self.expected_dimension or not all(
            math.isfinite(value) for value in values
        ):
            raise KnowledgeProviderError(
                "EMBEDDER_DIMENSION_MISMATCH"
                if len(values) != self.expected_dimension
                else "EMBEDDER_INVALID_VECTOR",
                "The dense embedding provider returned an incompatible vector.",
            )
        return _normalize_vector(values) if self.normalize_embeddings else values

    def _embed_batch(self, batch: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        payload = {"model": self.model_name, "input": list(batch)}
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            if self._client is not None:
                response = self._client.post(self._endpoint(), headers=headers, json=payload)
            else:
                with httpx.Client(timeout=self.timeout_seconds) as client:
                    response = client.post(self._endpoint(), headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise KnowledgeProviderError(
                "EMBEDDER_INFERENCE_FAILED",
                "The dense embedding API request failed.",
            ) from exc

        if response.status_code != 200:
            raise KnowledgeProviderError(
                "EMBEDDER_INFERENCE_FAILED",
                f"The dense embedding API returned HTTP {response.status_code}.",
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise KnowledgeProviderError(
                "EMBEDDER_INVALID_VECTOR",
                "The dense embedding API returned invalid JSON.",
            ) from exc

        if not isinstance(body, Mapping) or not isinstance(body.get("data"), list):
            raise KnowledgeProviderError(
                "EMBEDDER_INVALID_VECTOR",
                "The dense embedding API response is missing data.",
            )
        items = body["data"]
        if len(items) != len(batch):
            raise KnowledgeProviderError(
                "EMBEDDER_INVALID_VECTOR",
                "The dense embedding API returned an unexpected vector count.",
            )
        indexed: dict[int, tuple[float, ...]] = {}
        for pos, item in enumerate(items):
            if not isinstance(item, Mapping):
                raise KnowledgeProviderError(
                    "EMBEDDER_INVALID_VECTOR",
                    "The dense embedding API returned an invalid item.",
                )
            idx = item.get("index", pos)
            if type(idx) is not int or not 0 <= idx < len(batch) or idx in indexed:
                raise KnowledgeProviderError(
                    "EMBEDDER_INVALID_VECTOR",
                    "The dense embedding API returned invalid vector indices.",
                )
            indexed[idx] = self._validate(item.get("embedding"))
        return tuple(indexed[i] for i in range(len(batch)))

    def embed_documents(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        if not texts:
            return ()
        vectors: list[tuple[float, ...]] = []
        for offset in range(0, len(texts), self.batch_size):
            vectors.extend(self._embed_batch(texts[offset : offset + self.batch_size]))
        return tuple(vectors)

    def embed_query(self, text: str) -> tuple[float, ...]:
        return self.embed_documents((text,))[0]


__all__ = ["OpenAICompatibleDenseEmbedder"]
