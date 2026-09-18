"""Lazy multilingual lexical adapter based on the BGE-M3 tokenizer."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from threading import Lock
from typing import Any

from packages.knowledge.contracts import KnowledgeProviderError, SparseEncoding


class BgeM3SparseEncoder:
    def __init__(self, *, model_name: str = "BAAI/bge-m3") -> None:
        self.model_name = model_name
        self._tokenizer: Any | None = None
        self._load_lock = Lock()

    def _load_tokenizer(self) -> Any:
        if self._tokenizer is not None:
            return self._tokenizer
        with self._load_lock:
            if self._tokenizer is not None:
                return self._tokenizer
            try:
                from transformers import AutoTokenizer
            except ImportError as exc:
                raise KnowledgeProviderError(
                    "SPARSE_ENCODER_UNAVAILABLE",
                    "The sparse tokenizer runtime is not installed.",
                ) from exc
            try:
                self._tokenizer = AutoTokenizer.from_pretrained(self.model_name)
            except Exception as exc:
                raise KnowledgeProviderError(
                    "SPARSE_TOKENIZER_LOAD_FAILED",
                    "The multilingual sparse tokenizer could not be loaded.",
                ) from exc
            return self._tokenizer

    def encode_documents(self, texts: Sequence[str]) -> tuple[SparseEncoding, ...]:
        return tuple(self.encode_query(text) for text in texts)

    def encode_query(self, text: str) -> SparseEncoding:
        tokenizer = self._load_tokenizer()
        try:
            encoded = tokenizer(text, add_special_tokens=True, truncation=True)
            token_ids = encoded["input_ids"]
            special_ids = set(tokenizer.all_special_ids)
        except Exception as exc:
            raise KnowledgeProviderError(
                "SPARSE_ENCODING_FAILED",
                "The sparse tokenizer failed.",
            ) from exc
        counts = Counter(
            int(token_id) for token_id in token_ids if int(token_id) not in special_ids
        )
        indices = tuple(sorted(counts))
        values = tuple(1.0 + math.log(counts[index]) for index in indices)
        return SparseEncoding(indices=indices, values=values)


__all__ = ["BgeM3SparseEncoder"]
