"""Exercise the real loaders offline using a tiny local transformer."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from packages.knowledge.adapters.embeddings import BgeM3DenseEmbedder
from packages.knowledge.adapters.sparse import BgeM3SparseEncoder
from packages.knowledge.vector_cache import encoder_fingerprint


def test_optional_adapter_identity_failure_disables_reuse() -> None:
    class UnidentifiedEncoder:
        def document_cache_fingerprint(self) -> str:
            raise RuntimeError("runtime identity unavailable")

    assert encoder_fingerprint(UnidentifiedEncoder()) is None


def _save_tokenizer(directory: Path, max_length: int) -> None:
    from transformers import BertTokenizerFast

    vocab = directory / "vocab.txt"
    vocab.write_text("[PAD]\n[UNK]\n[CLS]\n[SEP]\n[MASK]\na\nb\n", encoding="utf-8")
    tokenizer = BertTokenizerFast(vocab_file=str(vocab), model_max_length=max_length)
    tokenizer.save_pretrained(directory)


def test_loaded_weights_tokenizer_and_config_invalidate_only_affected_vectors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import torch
    from transformers import BertConfig, BertModel

    config = BertConfig(
        vocab_size=7,
        hidden_size=8,
        num_hidden_layers=1,
        num_attention_heads=2,
        intermediate_size=12,
    )
    model = BertModel(config)
    model.save_pretrained(tmp_path)
    _save_tokenizer(tmp_path, 16)

    def dense() -> BgeM3DenseEmbedder:
        return BgeM3DenseEmbedder(model_name=str(tmp_path), device="cpu", expected_dimension=8)

    def sparse() -> BgeM3SparseEncoder:
        return BgeM3SparseEncoder(model_name=str(tmp_path))

    original_dense, original_sparse = dense(), sparse()
    dense_id = original_dense.document_cache_fingerprint()
    sparse_id = original_sparse.document_cache_fingerprint()
    assert dense_id and re.fullmatch("[a-f0-9]{64}", dense_id)
    assert sparse_id and re.fullmatch("[a-f0-9]{64}", sparse_id)
    assert dense_id != sparse_id
    assert dense().document_cache_fingerprint() == dense_id
    after_use = sparse()
    after_use.encode_query("a b a")
    assert after_use.document_cache_fingerprint() == sparse_id
    assert len(original_dense.embed_query("a b a")) == 8

    with torch.no_grad():
        next(model.parameters()).add_(0.25)
    model.save_pretrained(tmp_path)
    changed_dense_id = dense().document_cache_fingerprint()
    assert changed_dense_id and changed_dense_id != dense_id
    assert sparse().document_cache_fingerprint() == sparse_id

    _save_tokenizer(tmp_path, 8)
    assert dense().document_cache_fingerprint() not in (None, changed_dense_id)
    assert sparse().document_cache_fingerprint() not in (None, sparse_id)
    # The old adapters still use their resident objects after the path changes.
    monkeypatch.setattr(
        "packages.knowledge.adapters.embeddings.dense_model_identity",
        lambda model: pytest.fail("resident weights hashed twice"),
    )
    assert original_dense.document_cache_fingerprint() == dense_id
    original_dense.batch_size += 1
    assert original_dense.document_cache_fingerprint() not in (None, dense_id)
    assert original_sparse.document_cache_fingerprint() == sparse_id


def test_opaque_model_does_not_trust_a_hub_name() -> None:
    dense = BgeM3DenseEmbedder()
    sparse = BgeM3SparseEncoder()
    dense._model = object()
    sparse._tokenizer = object()
    assert dense.document_cache_fingerprint() is None
    assert sparse.document_cache_fingerprint() is None
