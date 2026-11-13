from math import ceil
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import torch
from tokenizers import Tokenizer
from tokenizers.models import WordLevel
from tokenizers.pre_tokenizers import Whitespace
from tokenizers.processors import TemplateProcessing
from transformers import PreTrainedTokenizerFast

from benchmarks.public_rag.experimental.length_batched_reranker import (
    BgeReranker as LengthBatchedReranker,
)
from packages.knowledge.adapters.reranker import BgeReranker
from packages.knowledge.contracts import RerankCandidate


def _tokenizer() -> PreTrainedTokenizerFast:
    words = ("[UNK]", "[PAD]", "[CLS]", "[SEP]", "where", "a", "b", "c", "d", "e")
    backend = Tokenizer(WordLevel({word: i for i, word in enumerate(words)}, unk_token="[UNK]"))
    backend.pre_tokenizer = Whitespace()
    backend.post_processor = TemplateProcessing(
        single="[CLS] $A [SEP]",
        pair="[CLS] $A [SEP] $B:1 [SEP]:1",
        special_tokens=[("[CLS]", 2), ("[SEP]", 3)],
    )
    return PreTrainedTokenizerFast(tokenizer_object=backend, unk_token="[UNK]", pad_token="[PAD]")


@pytest.mark.parametrize("count", [1, 2, 5])
@pytest.mark.parametrize("adapter_class", [BgeReranker, LengthBatchedReranker])
def test_reranker_preserves_truncated_pairs_and_original_score_order(adapter_class, count) -> None:
    tokenizer = _tokenizer()
    inputs = ["a " * 40, "b", "c " * 9, "d", "e " * 25][:count]
    query = "where where"
    candidates = [RerankCandidate(str(i), text) for i, text in enumerate(inputs)]
    reference = tokenizer([query] * count, inputs, truncation=True, max_length=32)
    lengths = [len(ids) for ids in reference["input_ids"]]
    originals = [tuple(reference[field][i] for field in reference) for i in range(count)]
    expected_order = list(range(count))
    buckets = adapter_class is LengthBatchedReranker and count > 2
    if buckets:
        expected_order.sort(key=lengths.__getitem__)
    batches = []
    seen_pairs = []

    def model(**batch):
        assert torch.is_inference_mode_enabled()
        batches.append(tuple(batch["input_ids"].shape))
        for row, mask in enumerate(batch["attention_mask"].bool()):
            seen_pairs.append(tuple(batch[field][row][mask].tolist() for field in reference))
        logits = (batch["input_ids"] * batch["attention_mask"]).sum(dim=1)
        return SimpleNamespace(logits=logits)

    reranker = adapter_class(device="cpu", batch_size=2, max_length=32)
    reranker._tokenizer = Mock(wraps=tokenizer)
    reranker._model = model
    reranker._torch = torch
    reranker._device = "cpu"

    assert reranker.rerank(query, candidates) == tuple(map(sum, reference["input_ids"]))
    assert seen_pairs == [originals[i] for i in expected_order]
    expected_shapes = []
    for offset in range(0, count, 2):
        indices = expected_order[offset : offset + 2]
        expected_shapes.append((len(indices), max(lengths[i] for i in indices)))
    assert batches == expected_shapes
    assert reranker._tokenizer.call_count == (1 if buckets else ceil(count / 2))
    assert reranker._tokenizer.pad.call_count == (len(batches) if buckets else 0)


@pytest.mark.parametrize("adapter_class", [BgeReranker, LengthBatchedReranker])
def test_empty_rerank_needs_no_model(adapter_class) -> None:
    assert adapter_class().rerank("where", []) == ()
