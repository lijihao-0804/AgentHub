"""Lazy BGE cross-encoder reranker adapter."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from packages.knowledge.adapters.embeddings import _resolve_device
from packages.knowledge.contracts import KnowledgeProviderError, RerankCandidate


class BgeReranker:
    def __init__(
        self,
        *,
        model_name: str = "BAAI/bge-reranker-v2-m3",
        device: str = "auto",
        batch_size: int = 8,
    ) -> None:
        self.model_name = model_name
        self.device_request = device
        self.batch_size = batch_size
        self._tokenizer: Any | None = None
        self._model: Any | None = None
        self._torch: Any | None = None
        self._device: str | None = None

    def _load(self) -> tuple[Any, Any]:
        if self._model is not None and self._tokenizer is not None and self._torch is not None:
            return self._tokenizer, self._model
        try:
            import torch
            from transformers import AutoModelForSequenceClassification, AutoTokenizer
        except ImportError as exc:
            raise KnowledgeProviderError(
                "RERANKER_UNAVAILABLE",
                "The reranker runtime is not installed.",
            ) from exc
        self._torch = torch
        self._device = _resolve_device(self.device_request, torch)
        dtype = torch.float16 if self._device == "cuda" else torch.float32
        try:
            tokenizer = AutoTokenizer.from_pretrained(self.model_name)
            model = AutoModelForSequenceClassification.from_pretrained(
                self.model_name,
                torch_dtype=dtype,
            )
            model.to(self._device)
            model.eval()
        except Exception as exc:
            raise KnowledgeProviderError(
                "RERANKER_LOAD_FAILED",
                "The reranker model could not be loaded.",
            ) from exc
        self._tokenizer = tokenizer
        self._model = model
        return tokenizer, model

    def rerank(self, query: str, candidates: Sequence[RerankCandidate]) -> tuple[float, ...]:
        if not candidates:
            return ()
        tokenizer, model = self._load()
        torch = self._torch
        try:
            scores: list[float] = []
            for offset in range(0, len(candidates), self.batch_size):
                batch = candidates[offset : offset + self.batch_size]
                encoded = tokenizer(
                    [query] * len(batch),
                    [candidate.text for candidate in batch],
                    padding=True,
                    truncation=True,
                    return_tensors="pt",
                )
                encoded = {key: value.to(self._device) for key, value in encoded.items()}
                with torch.inference_mode():
                    logits = model(**encoded).logits.reshape(-1)
                scores.extend(float(score) for score in logits.detach().cpu().tolist())
            return tuple(scores)
        except Exception as exc:
            raise KnowledgeProviderError(
                "RERANKER_INFERENCE_FAILED",
                "The reranker provider failed.",
            ) from exc


__all__ = ["BgeReranker"]
