"""Lazy BGE-M3 dense embedding adapter."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from packages.knowledge.contracts import KnowledgeProviderError


def _resolve_device(requested: str, torch: Any) -> str:
    if requested == "auto":
        return "cuda" if torch.cuda.is_available() else "cpu"
    if requested == "cuda" and not torch.cuda.is_available():
        raise KnowledgeProviderError("CUDA_UNAVAILABLE", "CUDA was requested but is unavailable.")
    return requested


class BgeM3DenseEmbedder:
    def __init__(
        self,
        *,
        model_name: str = "BAAI/bge-m3",
        device: str = "auto",
        batch_size: int = 8,
        expected_dimension: int = 1024,
    ) -> None:
        self.model_name = model_name
        self.device_request = device
        self.batch_size = batch_size
        self.expected_dimension = expected_dimension
        self._model: Any | None = None
        self._device: str | None = None

    @property
    def dimension(self) -> int:
        return self.expected_dimension

    def _load_model(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            import torch
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise KnowledgeProviderError(
                "EMBEDDER_UNAVAILABLE",
                "The dense embedding runtime is not installed.",
            ) from exc
        self._device = _resolve_device(self.device_request, torch)
        model_kwargs: dict[str, Any] = {}
        if self._device == "cuda":
            model_kwargs["torch_dtype"] = torch.float16
        try:
            model = SentenceTransformer(
                self.model_name,
                device=self._device,
                model_kwargs=model_kwargs,
            )
        except Exception as exc:
            raise KnowledgeProviderError(
                "EMBEDDER_LOAD_FAILED",
                "The dense embedding model could not be loaded.",
            ) from exc
        actual_dimension = model.get_sentence_embedding_dimension()
        if actual_dimension != self.expected_dimension:
            raise KnowledgeProviderError(
                "EMBEDDER_DIMENSION_MISMATCH",
                "The dense embedding model dimension is incompatible.",
            )
        self._model = model
        return model

    @staticmethod
    def _validate(vector: Any, expected_dimension: int) -> tuple[float, ...]:
        values = tuple(float(value) for value in vector)
        if len(values) != expected_dimension or not all(math.isfinite(value) for value in values):
            raise KnowledgeProviderError(
                "EMBEDDER_INVALID_VECTOR",
                "The dense embedding provider returned an invalid vector.",
            )
        return values

    def embed_documents(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        if not texts:
            return ()
        model = self._load_model()
        try:
            vectors = model.encode(
                list(texts),
                batch_size=self.batch_size,
                convert_to_numpy=True,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
        except Exception as exc:
            raise KnowledgeProviderError(
                "EMBEDDER_INFERENCE_FAILED",
                "The dense embedding provider failed.",
            ) from exc
        return tuple(self._validate(vector, self.expected_dimension) for vector in vectors)

    def embed_query(self, text: str) -> tuple[float, ...]:
        return self.embed_documents((text,))[0]


__all__ = ["BgeM3DenseEmbedder"]
