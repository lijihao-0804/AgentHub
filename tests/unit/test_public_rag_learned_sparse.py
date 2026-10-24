from __future__ import annotations

import math
from types import SimpleNamespace
from typing import Any

import pytest
import torch

from benchmarks.public_rag.learned_sparse_adapter import (
    DenseCopyRecord,
    LearnedM3SparseEncoder,
    LearnedSparseIndex,
    iter_dense_records,
)
from packages.knowledge.contracts import KnowledgeProviderError, SparseEncoding, VectorScope


class _FixtureTokenizer:
    cls_token_id = 0
    eos_token_id = 2
    pad_token_id = 1
    unk_token_id = 3
    all_special_ids = (0, 1, 2, 3, 7)
    vocab_size = 8


class _FixtureSentenceTransformer(torch.nn.Module):
    def __init__(self, fixtures: dict[str, dict[str, list[Any]]]) -> None:
        super().__init__()
        self.tokenizer = _FixtureTokenizer()
        self.device = torch.device("cpu")
        self.dtype = torch.float32
        self._fixtures = fixtures
        self._parameter = torch.nn.Parameter(torch.tensor(1.0, dtype=torch.float32))
        self.forward_training_states: list[bool] = []
        self.forward_grad_states: list[bool] = []
        self.eval_grad_states: list[bool] = []

    def eval(self) -> _FixtureSentenceTransformer:
        self.eval_grad_states.append(torch.is_grad_enabled())
        return super().eval()

    def tokenize(self, texts: list[str]) -> dict[str, torch.Tensor]:
        selected = [self._fixtures[text] for text in texts]
        return {
            "input_ids": torch.tensor([item["input_ids"] for item in selected], dtype=torch.long),
            "attention_mask": torch.tensor(
                [item["attention_mask"] for item in selected],
                dtype=torch.long,
            ),
            "hidden_values": torch.tensor(
                [item["hidden_values"] for item in selected],
                dtype=torch.float32,
            ),
        }

    def forward(self, features: dict[str, torch.Tensor]) -> dict[str, torch.Tensor]:
        self.forward_training_states.append(self.training)
        self.forward_grad_states.append(torch.is_grad_enabled())
        hidden_values = features["hidden_values"]
        hidden_states = torch.zeros(
            (*hidden_values.shape, 1024),
            dtype=torch.float32,
            device=hidden_values.device,
        )
        hidden_states[:, :, 0] = hidden_values
        return {"token_embeddings": hidden_states}

    def encode(self, *_: Any, **__: Any) -> None:
        raise AssertionError("learned sparse must use the shared model forward path")


class _FixtureDenseEmbedder:
    def __init__(self, model: _FixtureSentenceTransformer) -> None:
        self._model = model
        self.load_count = 0

    def _load_model(self) -> _FixtureSentenceTransformer:
        self.load_count += 1
        return self._model


class _FixtureLinear(torch.nn.Linear):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.input_dtypes: list[torch.dtype] = []

    def forward(self, input_tensor: torch.Tensor) -> torch.Tensor:
        self.input_dtypes.append(input_tensor.dtype)
        return super().forward(input_tensor)


class _InMemoryWeightsPath:
    def __init__(self, state_dict: dict[str, torch.Tensor] | None) -> None:
        self._state_dict = state_dict

    def __truediv__(self, name: str) -> _InMemoryWeightsPath:
        assert name == "sparse_linear.pt"
        return self

    def is_file(self) -> bool:
        return self._state_dict is not None


def _fixture(
    *,
    input_ids: list[int],
    attention_mask: list[int],
    hidden_values: list[float],
) -> dict[str, list[Any]]:
    return {
        "input_ids": input_ids,
        "attention_mask": attention_mask,
        "hidden_values": hidden_values,
    }


def _valid_weights() -> dict[str, torch.Tensor]:
    weight = torch.zeros((1, 1024), dtype=torch.float16)
    weight[:, 0] = 1
    return {
        "weight": weight,
        "bias": torch.tensor([-0.5], dtype=torch.float32),
    }


def _encoder(
    fixtures: dict[str, dict[str, list[Any]]],
) -> tuple[LearnedM3SparseEncoder, _FixtureDenseEmbedder, _FixtureSentenceTransformer]:
    model = _FixtureSentenceTransformer(fixtures)
    dense_embedder = _FixtureDenseEmbedder(model)
    encoder = LearnedM3SparseEncoder(
        dense_embedder=dense_embedder,  # type: ignore[arg-type]
        model_directory=_InMemoryWeightsPath(_valid_weights()),  # type: ignore[arg-type]
        batch_size=2,
    )
    return encoder, dense_embedder, model


def _patch_weights_loader(
    monkeypatch: pytest.MonkeyPatch,
    state_dict: dict[str, torch.Tensor] | None,
) -> None:
    def load(path: Any, **kwargs: Any) -> dict[str, torch.Tensor]:
        assert path is not None
        assert kwargs == {"weights_only": True, "map_location": "cpu"}
        if state_dict is None:
            raise AssertionError("the missing-weight path must not call torch.load")
        return state_dict

    monkeypatch.setattr(torch, "load", load)


def test_learned_sparse_matches_hand_calculated_relu_max_and_masks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoder, dense_embedder, model = _encoder(
        {
            "query": _fixture(
                input_ids=[0, 4, 4, 3, 5, 2, 7, 1, 6],
                attention_mask=[1, 1, 1, 1, 1, 1, 1, 0, 0],
                hidden_values=[1.2, 0.1, 0.9, 9.0, 0.6, 9.0, 1.0, 4.0, 5.0],
            )
        },
    )
    _patch_weights_loader(
        monkeypatch,
        _valid_weights(),
    )
    monkeypatch.setattr(torch.nn, "Linear", _FixtureLinear)

    encoded = encoder.encode_query("query")

    assert encoder.dense_embedder is dense_embedder
    assert dense_embedder.load_count == 1
    assert encoded == SparseEncoding(indices=(4, 5, 7), values=pytest.approx((0.4, 0.1, 0.5)))
    assert not set(encoded.indices).intersection({0, 1, 2, 3})
    assert 6 not in encoded.indices
    assert model.forward_training_states == [False]
    assert model.forward_grad_states == [False]
    assert model.eval_grad_states[-1] is False
    assert encoder._sparse_linear is not None
    assert isinstance(encoder._sparse_linear, _FixtureLinear)
    assert encoder._sparse_linear.input_dtypes == [torch.float32]
    assert encoder._sparse_linear.weight.device.type == "cpu"
    assert encoder._sparse_linear.weight.dtype == model.dtype
    assert encoder._sparse_linear.bias.device.type == "cpu"
    assert encoder._sparse_linear.bias.dtype == model.dtype


def test_learned_sparse_dot_product_uses_unormalized_coordinates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoder, _, _ = _encoder(
        {
            "query": _fixture(
                input_ids=[0, 4, 5, 6, 3, 2, 1],
                attention_mask=[1, 1, 1, 1, 1, 1, 0],
                hidden_values=[1.2, 1.2, 0.9, 0.8, 9.0, 9.0, 9.0],
            ),
            "document": _fixture(
                input_ids=[0, 4, 5, 6, 3, 2, 1],
                attention_mask=[1, 1, 1, 1, 1, 1, 0],
                hidden_values=[1.4, 1.4, 0.8, 0.7, 9.0, 9.0, 9.0],
            ),
        },
    )
    _patch_weights_loader(
        monkeypatch,
        _valid_weights(),
    )

    query = encoder.encode_query("query")
    document = encoder.encode_documents(("document",))[0]
    document_values = dict(zip(document.indices, document.values, strict=True))
    dot_product = math.fsum(
        value * document_values[index]
        for index, value in zip(query.indices, query.values, strict=True)
    )

    assert query == SparseEncoding(indices=(4, 5, 6), values=pytest.approx((0.7, 0.4, 0.3)))
    assert document == SparseEncoding(indices=(4, 5, 6), values=pytest.approx((0.9, 0.3, 0.2)))
    assert dot_product == pytest.approx(0.81)


def test_empty_inputs_do_not_load_models() -> None:
    encoder, dense_embedder, _ = _encoder({})

    assert encoder.encode_documents(()) == ()
    assert encoder.encode_query("") == SparseEncoding(indices=(), values=())
    assert dense_embedder.load_count == 0
    assert encoder._sparse_linear is None


def test_missing_learned_weights_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    model = _FixtureSentenceTransformer({})
    dense_embedder = _FixtureDenseEmbedder(model)
    _patch_weights_loader(monkeypatch, None)
    encoder = LearnedM3SparseEncoder(
        dense_embedder=dense_embedder,  # type: ignore[arg-type]
        model_directory=_InMemoryWeightsPath(None),  # type: ignore[arg-type]
    )

    with pytest.raises(KnowledgeProviderError) as error:
        encoder.warm()

    assert error.value.code == "LEARNED_SPARSE_WEIGHTS_MISSING"
    assert model.training is False
    assert model.eval_grad_states[-1] is True
    assert dense_embedder.load_count == 1


@pytest.mark.parametrize(
    "state_dict",
    [
        {
            "weight": torch.zeros((1, 1023)),
            "bias": torch.zeros((1,)),
        },
        {
            "weight": torch.zeros((1, 1024)),
        },
        {
            "weight": torch.full((1, 1024), float("nan")),
            "bias": torch.zeros((1,)),
        },
        {
            "weight": torch.zeros((1, 1024)),
            "bias": torch.tensor([float("inf")]),
        },
    ],
)
def test_invalid_learned_state_dicts_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    state_dict: dict[str, torch.Tensor],
) -> None:
    monkeypatch.setattr(torch.nn, "Linear", _FixtureLinear)
    _patch_weights_loader(monkeypatch, state_dict)
    encoder = LearnedM3SparseEncoder(
        dense_embedder=_FixtureDenseEmbedder(_FixtureSentenceTransformer({})),  # type: ignore[arg-type]
        model_directory=_InMemoryWeightsPath(state_dict),  # type: ignore[arg-type]
    )

    with pytest.raises(KnowledgeProviderError) as error:
        encoder.warm()

    assert error.value.code == "LEARNED_SPARSE_WEIGHTS_INVALID"


def test_learned_sparse_schema_accepts_server_none_and_rejects_idf() -> None:
    vectors, sparse_vectors = LearnedSparseIndex._expected_vectors(1024)

    assert set(vectors) == {"dense"}
    assert sparse_vectors["sparse"].modifier.value == "none"
    assert LearnedSparseIndex._sparse_params_match({"sparse": {"modifier": None}})
    assert LearnedSparseIndex._sparse_params_match({"sparse": {"modifier": "none"}})
    assert not LearnedSparseIndex._sparse_params_match({"sparse": {}})
    assert not LearnedSparseIndex._sparse_params_match({"sparse": {"modifier": "idf"}})
    assert not LearnedSparseIndex._sparse_params_match({"sparse": {"modifier": None}, "other": {}})


class _FakeScrollClient:
    def __init__(self, pages: list[tuple[list[Any], str | None]]) -> None:
        self._pages = pages
        self.offsets: list[str | None] = []
        self.vector_names: list[list[str]] = []

    def scroll(self, _collection: str, **kwargs: Any) -> tuple[list[Any], str | None]:
        self.offsets.append(kwargs["offset"])
        self.vector_names.append(kwargs["with_vectors"])
        page_index = (
            0 if kwargs["offset"] is None else int(str(kwargs["offset"]).removeprefix("page-"))
        )
        return self._pages[page_index]


def _source_index(client: _FakeScrollClient) -> SimpleNamespace:
    expected_scope = VectorScope("workspace-a", "kb-a", ("revision-a",))

    def scope_filter(scope: VectorScope) -> tuple[str, ...]:
        assert scope == expected_scope
        return scope.document_revision_ids

    return SimpleNamespace(
        _client=client,
        _scope_filter=scope_filter,
        collection_name="legacy-isolated",
        dense_vector_size=4,
    )


def test_dense_copy_iterator_pages_and_hides_qdrant_records() -> None:
    first = SimpleNamespace(
        id="point-1",
        vector={"dense": [0.1, 0.2, 0.3, 0.4]},
        payload={"chunk_id": "chunk-1"},
    )
    second = SimpleNamespace(
        id="point-2",
        vector={"dense": [0.5, 0.6, 0.7, 0.8]},
        payload={"chunk_id": "chunk-2"},
    )
    client = _FakeScrollClient([([first], "page-1"), ([second], None)])

    pages = tuple(
        iter_dense_records(
            _source_index(client),  # type: ignore[arg-type]
            scope=VectorScope("workspace-a", "kb-a", ("revision-a",)),
            batch_size=1,
        )
    )

    assert pages == (
        (
            DenseCopyRecord(
                point_id="point-1",
                chunk_id="chunk-1",
                dense=(0.1, 0.2, 0.3, 0.4),
                payload={"chunk_id": "chunk-1"},
            ),
        ),
        (
            DenseCopyRecord(
                point_id="point-2",
                chunk_id="chunk-2",
                dense=(0.5, 0.6, 0.7, 0.8),
                payload={"chunk_id": "chunk-2"},
            ),
        ),
    )
    assert client.offsets == [None, "page-1"]
    assert client.vector_names == [["dense"], ["dense"]]


@pytest.mark.parametrize(
    ("record", "code"),
    [
        (
            SimpleNamespace(
                id="point-1",
                vector={"dense": [0.1, 0.2]},
                payload={"chunk_id": "chunk-1"},
            ),
            "INVALID_DENSE_VECTOR",
        ),
        (
            SimpleNamespace(
                id="point-1",
                vector={"dense": [0.1, 0.2, float("nan"), 0.4]},
                payload={"chunk_id": "chunk-1"},
            ),
            "INVALID_DENSE_VECTOR",
        ),
        (
            SimpleNamespace(
                id="point-1",
                vector={"dense": [0.1, 0.2, 0.3, 0.4]},
                payload={},
            ),
            "INVALID_PROVIDER_RESULT",
        ),
    ],
)
def test_dense_copy_rejects_invalid_records(record: Any, code: str) -> None:
    client = _FakeScrollClient([([record], None)])

    with pytest.raises(KnowledgeProviderError) as error:
        tuple(
            iter_dense_records(
                _source_index(client),  # type: ignore[arg-type]
                scope=VectorScope("workspace-a", "kb-a", ("revision-a",)),
            )
        )

    assert error.value.code == code
