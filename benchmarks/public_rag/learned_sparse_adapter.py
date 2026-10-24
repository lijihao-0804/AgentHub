"""Experimental BGE-M3 learned sparse adapter and Qdrant helpers.

This module deliberately stays outside the production knowledge composition.
It reuses the dense embedder's SentenceTransformer instance and writes to a
separate collection whose sparse vectors do not receive Qdrant IDF weighting.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

from qdrant_client import models

from packages.knowledge.adapters.embeddings import BgeM3DenseEmbedder
from packages.knowledge.adapters.qdrant import QdrantVectorIndex
from packages.knowledge.contracts import (
    KnowledgeProviderError,
    SparseEncoding,
    VectorIndex,
    VectorRecord,
    VectorScope,
    VectorSearchHit,
)

_BGE_M3_HIDDEN_SIZE = 1024
_SPECIAL_TOKEN_NAMES = ("cls_token_id", "eos_token_id", "pad_token_id", "unk_token_id")


@dataclass(frozen=True, slots=True)
class DenseCopyRecord:
    """A provider-neutral dense record read from an existing Qdrant collection."""

    point_id: str
    chunk_id: str
    dense: tuple[float, ...]
    payload: dict[str, Any]


class SharedDenseTreatmentIndex:
    """Anchor dense searches to one ANN graph while replacing only sparse search.

    Identical stored dense vectors do not guarantee identical approximate search
    results after an index rebuild. This read-only experimental facade removes
    that confound. Both wrapped clients remain owned by the experiment runner.
    """

    def __init__(self, baseline: VectorIndex, learned: VectorIndex) -> None:
        self.baseline = baseline
        self.learned = learned

    def ensure_collection(self) -> None:
        self.baseline.ensure_collection()
        self.learned.ensure_collection()

    def upsert(self, records: Sequence[VectorRecord]) -> None:
        raise KnowledgeProviderError(
            "EXPERIMENT_INDEX_READ_ONLY", "The paired retrieval index is read-only."
        )

    def dense_search(
        self, query: Sequence[float], *, scope: VectorScope, limit: int
    ) -> tuple[VectorSearchHit, ...]:
        return self.baseline.dense_search(query, scope=scope, limit=limit)

    def sparse_search(
        self, query: SparseEncoding, *, scope: VectorScope, limit: int
    ) -> tuple[VectorSearchHit, ...]:
        return self.learned.sparse_search(query, scope=scope, limit=limit)


class LearnedM3SparseEncoder:
    """Compute BGE-M3 learned sparse vectors from the shared dense backbone."""

    def __init__(
        self,
        dense_embedder: BgeM3DenseEmbedder,
        model_directory: Path,
        batch_size: int = 8,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        self.dense_embedder = dense_embedder
        self.model_directory = model_directory
        self.batch_size = batch_size
        self._model: Any | None = None
        self._sparse_linear: Any | None = None
        self._tokenizer: Any | None = None
        self._special_token_ids: frozenset[int] = frozenset()
        self._vocab_size: int | None = None
        self._load_lock = Lock()

    def _load_runtime(self) -> tuple[Any, Any, Any]:
        if self._model is not None and self._sparse_linear is not None:
            return self._model, self._sparse_linear, self._tokenizer

        with self._load_lock:
            if self._model is not None and self._sparse_linear is not None:
                return self._model, self._sparse_linear, self._tokenizer

            model = self.dense_embedder._load_model()
            try:
                model.eval()
                import torch
            except ImportError as exc:
                raise KnowledgeProviderError(
                    "LEARNED_SPARSE_UNAVAILABLE",
                    "The learned sparse runtime is not installed.",
                ) from exc
            except Exception as exc:
                raise KnowledgeProviderError(
                    "LEARNED_SPARSE_RUNTIME_INVALID",
                    "The shared BGE-M3 runtime is incompatible.",
                ) from exc

            tokenizer = getattr(model, "tokenizer", None)
            if tokenizer is None:
                raise KnowledgeProviderError(
                    "LEARNED_SPARSE_RUNTIME_INVALID",
                    "The shared BGE-M3 runtime has no public tokenizer.",
                )
            special_token_ids = self._read_special_token_ids(tokenizer)
            vocab_size = self._read_vocab_size(tokenizer, special_token_ids)
            device, dtype = self._model_device_dtype(model, torch)
            sparse_linear = self._load_sparse_linear(
                torch=torch,
                device=device,
                dtype=dtype,
            )

            self._tokenizer = tokenizer
            self._special_token_ids = special_token_ids
            self._vocab_size = vocab_size
            self._sparse_linear = sparse_linear
            self._model = model
            return model, sparse_linear, tokenizer

    @staticmethod
    def _read_special_token_ids(tokenizer: Any) -> frozenset[int]:
        special_ids: list[int] = []
        for attribute in _SPECIAL_TOKEN_NAMES:
            token_id = getattr(tokenizer, attribute, None)
            if not isinstance(token_id, int):
                raise KnowledgeProviderError(
                    "LEARNED_SPARSE_TOKENIZER_INVALID",
                    "The shared BGE-M3 tokenizer does not expose the required special IDs.",
                )
            special_ids.append(token_id)
        return frozenset(special_ids)

    @staticmethod
    def _read_vocab_size(tokenizer: Any, special_token_ids: frozenset[int]) -> int:
        vocab_size = getattr(tokenizer, "vocab_size", None)
        if not isinstance(vocab_size, int) or vocab_size < 1:
            raise KnowledgeProviderError(
                "LEARNED_SPARSE_TOKENIZER_INVALID",
                "The shared BGE-M3 tokenizer has an invalid vocabulary size.",
            )
        if any(token_id < 0 or token_id >= vocab_size for token_id in special_token_ids):
            raise KnowledgeProviderError(
                "LEARNED_SPARSE_TOKENIZER_INVALID",
                "The shared BGE-M3 tokenizer has an out-of-range special ID.",
            )
        return vocab_size

    @staticmethod
    def _model_device_dtype(model: Any, torch: Any) -> tuple[Any, Any]:
        parameters = getattr(model, "parameters", None)
        first_parameter = next(parameters(), None) if callable(parameters) else None
        device_value = getattr(model, "device", None)
        dtype_value = getattr(model, "dtype", None)
        if device_value is None and first_parameter is not None:
            device_value = first_parameter.device
        if dtype_value is None and first_parameter is not None:
            dtype_value = first_parameter.dtype
        try:
            device = torch.device(device_value)
            if not isinstance(dtype_value, torch.dtype):
                raise TypeError("model dtype is not a torch dtype")
        except (TypeError, RuntimeError) as exc:
            raise KnowledgeProviderError(
                "LEARNED_SPARSE_RUNTIME_INVALID",
                "The shared BGE-M3 runtime has no usable device or dtype.",
            ) from exc
        return device, dtype_value

    def _load_sparse_linear(self, *, torch: Any, device: Any, dtype: Any) -> Any:
        weights_path = self.model_directory / "sparse_linear.pt"
        if not weights_path.is_file():
            raise KnowledgeProviderError(
                "LEARNED_SPARSE_WEIGHTS_MISSING",
                "The learned sparse weights are missing.",
            )
        try:
            state_dict = torch.load(
                weights_path,
                weights_only=True,
                map_location="cpu",
            )
        except Exception as exc:
            raise KnowledgeProviderError(
                "LEARNED_SPARSE_WEIGHTS_LOAD_FAILED",
                "The learned sparse weights could not be loaded.",
            ) from exc

        if not isinstance(state_dict, Mapping) or set(state_dict) != {"weight", "bias"}:
            raise KnowledgeProviderError(
                "LEARNED_SPARSE_WEIGHTS_INVALID",
                "The learned sparse state dict has unexpected keys.",
            )
        weight = state_dict["weight"]
        bias = state_dict["bias"]
        if (
            not torch.is_tensor(weight)
            or not torch.is_tensor(bias)
            or tuple(weight.shape) != (1, _BGE_M3_HIDDEN_SIZE)
            or tuple(bias.shape) != (1,)
            or not weight.is_floating_point()
            or not bias.is_floating_point()
            or not bool(torch.isfinite(weight).all())
            or not bool(torch.isfinite(bias).all())
        ):
            raise KnowledgeProviderError(
                "LEARNED_SPARSE_WEIGHTS_INVALID",
                "The learned sparse linear weights are incompatible.",
            )

        try:
            sparse_linear = torch.nn.Linear(
                _BGE_M3_HIDDEN_SIZE,
                1,
                bias=True,
                device=device,
                dtype=dtype,
            )
            with torch.no_grad():
                sparse_linear.weight.copy_(weight)
                sparse_linear.bias.copy_(bias)
        except Exception as exc:
            raise KnowledgeProviderError(
                "LEARNED_SPARSE_WEIGHTS_INVALID",
                "The learned sparse linear weights could not be converted.",
            ) from exc
        sparse_linear.eval()
        return sparse_linear

    def encode_documents(self, texts: Sequence[str]) -> tuple[SparseEncoding, ...]:
        if not texts:
            return ()
        encoded: list[SparseEncoding] = []
        for start in range(0, len(texts), self.batch_size):
            encoded.extend(self._encode_batch(tuple(texts[start : start + self.batch_size])))
        return tuple(encoded)

    def encode_query(self, text: str) -> SparseEncoding:
        if not text:
            return SparseEncoding(indices=(), values=())
        return self._encode_batch((text,))[0]

    def _encode_batch(self, texts: Sequence[str]) -> tuple[SparseEncoding, ...]:
        model, sparse_linear, tokenizer = self._load_runtime()
        import torch

        try:
            with torch.inference_mode():
                model.eval()
                features = model.tokenize(list(texts))
                if not isinstance(features, Mapping):
                    raise KnowledgeProviderError(
                        "LEARNED_SPARSE_INVALID_OUTPUT",
                        "The shared tokenizer returned an invalid feature mapping.",
                    )
                device, _ = self._model_device_dtype(model, torch)
                features = {
                    name: value.to(device) if torch.is_tensor(value) else value
                    for name, value in features.items()
                }
                output = model.forward(features)
                if not isinstance(output, Mapping) or "token_embeddings" not in output:
                    raise KnowledgeProviderError(
                        "LEARNED_SPARSE_INVALID_OUTPUT",
                        "The shared model returned no token embeddings.",
                    )
                hidden_states = output["token_embeddings"]
                input_ids = features.get("input_ids")
                attention_mask = features.get("attention_mask")
                encodings = self._sparsify(
                    torch=torch,
                    hidden_states=hidden_states,
                    input_ids=input_ids,
                    attention_mask=attention_mask,
                    sparse_linear=sparse_linear,
                    expected_batch_size=len(texts),
                )
        except KnowledgeProviderError:
            raise
        except Exception as exc:
            raise KnowledgeProviderError(
                "LEARNED_SPARSE_INFERENCE_FAILED",
                "The shared BGE-M3 sparse inference failed.",
            ) from exc
        return encodings

    def _sparsify(
        self,
        *,
        torch: Any,
        hidden_states: Any,
        input_ids: Any,
        attention_mask: Any,
        sparse_linear: Any,
        expected_batch_size: int,
    ) -> tuple[SparseEncoding, ...]:
        if (
            not torch.is_tensor(hidden_states)
            or hidden_states.ndim != 3
            or tuple(hidden_states.shape[0::2]) != (expected_batch_size, _BGE_M3_HIDDEN_SIZE)
            or not torch.is_tensor(input_ids)
            or not torch.is_tensor(attention_mask)
            or input_ids.ndim != 2
            or attention_mask.ndim != 2
            or tuple(input_ids.shape) != tuple(hidden_states.shape[:2])
            or tuple(attention_mask.shape) != tuple(hidden_states.shape[:2])
            or self._vocab_size is None
        ):
            raise KnowledgeProviderError(
                "LEARNED_SPARSE_INVALID_OUTPUT",
                "The shared model returned incompatible tensor shapes.",
            )

        input_ids = input_ids.to(device=hidden_states.device, dtype=torch.long)
        attention_mask = attention_mask.to(device=hidden_states.device, dtype=torch.bool)
        if input_ids.numel():
            minimum_id = int(input_ids.min().item())
            maximum_id = int(input_ids.max().item())
            if minimum_id < 0 or maximum_id >= self._vocab_size:
                raise KnowledgeProviderError(
                    "LEARNED_SPARSE_INVALID_OUTPUT",
                    "The shared tokenizer returned an out-of-range token ID.",
                )

        token_weights = torch.relu(sparse_linear(hidden_states).squeeze(-1))
        if not bool(torch.isfinite(token_weights).all()):
            raise KnowledgeProviderError(
                "LEARNED_SPARSE_INVALID_OUTPUT",
                "The learned sparse layer returned non-finite weights.",
            )

        exclude = ~attention_mask
        for token_id in self._special_token_ids:
            exclude |= input_ids.eq(token_id)
        token_weights = token_weights.masked_fill(exclude, 0)

        # The vocabulary axis is required by scatter_reduce, but no
        # batch x sequence x vocabulary intermediate is materialized.
        sparse_weights = torch.zeros(
            (expected_batch_size, self._vocab_size),
            dtype=token_weights.dtype,
            device=token_weights.device,
        )
        sparse_weights.scatter_reduce_(
            1,
            input_ids,
            token_weights,
            reduce="amax",
            include_self=True,
        )

        encodings: list[SparseEncoding] = []
        for row in sparse_weights:
            indices = torch.nonzero(row > 0, as_tuple=False).flatten()
            values = row.index_select(0, indices)
            if not bool(torch.isfinite(values).all()):
                raise KnowledgeProviderError(
                    "LEARNED_SPARSE_INVALID_OUTPUT",
                    "The learned sparse encoder returned non-finite coordinates.",
                )
            encodings.append(
                SparseEncoding(
                    indices=tuple(int(index) for index in indices.tolist()),
                    values=tuple(float(value) for value in values.tolist()),
                )
            )
        return tuple(encodings)

    def warm(self) -> None:
        """Load the shared backbone and sparse linear layer without user input."""

        model, _, _ = self._load_runtime()
        import torch

        try:
            with torch.inference_mode():
                model.eval()
        except Exception as exc:
            raise KnowledgeProviderError(
                "LEARNED_SPARSE_RUNTIME_INVALID",
                "The shared BGE-M3 runtime could not enter evaluation mode.",
            ) from exc


class LearnedSparseIndex(QdrantVectorIndex):
    """Qdrant index variant for learned sparse vectors without IDF correction."""

    @staticmethod
    def _expected_vectors(
        size: int,
    ) -> tuple[dict[str, models.VectorParams], dict[str, models.SparseVectorParams]]:
        return (
            {"dense": models.VectorParams(size=size, distance=models.Distance.COSINE)},
            {"sparse": models.SparseVectorParams(modifier=models.Modifier.NONE)},
        )

    @staticmethod
    def _sparse_params_match(actual: Any) -> bool:
        if not isinstance(actual, dict) or set(actual) != {"sparse"}:
            return False
        sparse = actual["sparse"]
        if sparse is None:
            return False
        if isinstance(sparse, Mapping):
            if "modifier" not in sparse:
                return False
            modifier = sparse["modifier"]
        else:
            if not hasattr(sparse, "modifier"):
                return False
            modifier = sparse.modifier
        if modifier is None:
            return True
        value = getattr(modifier, "value", modifier)
        return str(value).lower() == models.Modifier.NONE.value


def iter_dense_records(
    source_index: QdrantVectorIndex,
    *,
    scope: VectorScope,
    batch_size: int = 128,
) -> Iterator[tuple[DenseCopyRecord, ...]]:
    """Yield validated dense records without exposing Qdrant SDK record types."""

    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    if not scope.document_revision_ids:
        return

    scroll_filter = source_index._scope_filter(scope)
    offset: Any | None = None
    while True:
        previous_offset = offset
        try:
            records, offset = source_index._client.scroll(
                source_index.collection_name,
                scroll_filter=scroll_filter,
                limit=batch_size,
                with_payload=True,
                with_vectors=["dense"],
                offset=offset,
            )
        except KnowledgeProviderError:
            raise
        except Exception as exc:
            raise KnowledgeProviderError(
                "QDRANT_UNAVAILABLE",
                "The source knowledge vector index is unavailable.",
            ) from exc

        yield tuple(
            _dense_copy_record(record, expected_dimension=source_index.dense_vector_size)
            for record in records
        )
        if offset is None:
            return
        if offset == previous_offset:
            raise KnowledgeProviderError(
                "INVALID_PROVIDER_RESULT",
                "The source knowledge index returned a non-advancing scroll offset.",
            )


def _dense_copy_record(record: Any, *, expected_dimension: int) -> DenseCopyRecord:
    vector = getattr(record, "vector", None)
    if not isinstance(vector, Mapping):
        raise KnowledgeProviderError(
            "INVALID_DENSE_VECTOR",
            "The source knowledge record has no named dense vector.",
        )
    raw_dense = vector.get("dense")
    if not isinstance(raw_dense, Sequence) or isinstance(raw_dense, (str, bytes)):
        raise KnowledgeProviderError(
            "INVALID_DENSE_VECTOR",
            "The source knowledge record has an invalid dense vector.",
        )
    try:
        dense = tuple(float(value) for value in raw_dense)
    except (TypeError, ValueError) as exc:
        raise KnowledgeProviderError(
            "INVALID_DENSE_VECTOR",
            "The source knowledge record has an invalid dense vector.",
        ) from exc
    if len(dense) != expected_dimension or not all(math.isfinite(value) for value in dense):
        raise KnowledgeProviderError(
            "INVALID_DENSE_VECTOR",
            "The source knowledge record has an invalid dense vector.",
        )

    payload = getattr(record, "payload", None)
    if not isinstance(payload, Mapping):
        raise KnowledgeProviderError(
            "INVALID_PROVIDER_RESULT",
            "The source knowledge record has no payload.",
        )
    chunk_id = payload.get("chunk_id")
    if not isinstance(chunk_id, str) or not chunk_id:
        raise KnowledgeProviderError(
            "INVALID_PROVIDER_RESULT",
            "The source knowledge record has no valid chunk_id.",
        )
    raw_point_id = getattr(record, "id", None)
    if not isinstance(raw_point_id, (int, str)) or str(raw_point_id) == "":
        raise KnowledgeProviderError(
            "INVALID_PROVIDER_RESULT",
            "The source knowledge record has no point ID.",
        )
    point_id = str(raw_point_id)
    return DenseCopyRecord(
        point_id=point_id,
        chunk_id=chunk_id,
        dense=dense,
        payload=dict(payload),
    )


__all__ = [
    "DenseCopyRecord",
    "LearnedM3SparseEncoder",
    "LearnedSparseIndex",
    "iter_dense_records",
]
