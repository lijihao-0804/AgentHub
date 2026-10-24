from types import SimpleNamespace

import pytest

from benchmarks.public_rag import parent_rerank_audit as audit


class FakeTokenizer:
    def __init__(self, *, body_limit: int = 8, malformed: bool = False) -> None:
        self.body_limit = body_limit
        self.malformed = malformed
        self.calls: list[tuple[int, bool]] = []

    def __call__(
        self,
        queries: list[str],
        texts: list[str],
        *,
        padding: bool,
        truncation: bool,
        verbose: bool,
    ) -> dict[str, list[list[int]]]:
        assert padding is False
        assert verbose is False
        self.calls.append((len(texts), truncation))
        limit = self.body_limit + 2 if truncation else max(map(len, texts)) + 2
        assert len(queries) == len(texts)
        assert all(isinstance(text, str) for text in texts)
        ids = [[0, *range(1, len(text) + 1), 1][:limit] for text in texts]
        if self.malformed:
            ids.pop()
        return {"input_ids": ids}


def _child(chunk_id: str, revision: str, text: str) -> SimpleNamespace:
    return SimpleNamespace(chunk_id=chunk_id, document_revision_id=revision, text=text)


def _row(query_id: str, hits: list[tuple[str, str]], compute: dict | None = None) -> dict:
    return {
        "query_id": query_id,
        "_query_text": "query",
        "stages": {
            "selection": {
                "ranking": [
                    {"chunk_id": chunk_id, "document_id": source_id} for chunk_id, source_id in hits
                ]
            }
        },
        "compute": compute,
    }


def test_parent_order_and_first_occurrence_are_preserved() -> None:
    children = {
        "a": _child("a", "rev-1", "aa"),
        "b": _child("b", "rev-2", "bbbb"),
        "c": _child("c", "rev-1", "ccc"),
    }
    parents = {
        "a": ("rev-1", "revision one"),
        "b": ("rev-2", "revision two"),
        "c": ("rev-1", "revision one"),
    }
    pairs, revisions, expanded = audit.reconstruct_pairs(
        _row("q", [("a", "doc-1"), ("b", "doc-2"), ("c", "doc-1")]),
        "parent",
        children,
        parents,
        {"a": "doc-1", "b": "doc-2", "c": "doc-1"},
    )

    assert [(pair.pair_id, pair.source_document_id) for pair in pairs] == [
        ("rev-1", "doc-1"),
        ("rev-2", "doc-2"),
    ]
    assert revisions == {"rev-1", "rev-2"}
    assert expanded == 2


def test_baseline_uses_authorized_child_text() -> None:
    pairs, revisions, expanded = audit.reconstruct_pairs(
        _row("q", [("a", "doc-1")]),
        "baseline",
        {"a": _child("a", "rev-1", "child text")},
        {"a": ("rev-1", "full parent")},
        {"a": "doc-1"},
    )

    assert pairs == [audit.Pair("q", "baseline", "a", "doc-1", "child text")]
    assert revisions == {"rev-1"}
    assert expanded == 0


def test_source_mapping_drift_is_rejected() -> None:
    with pytest.raises(ValueError, match="source mapping"):
        audit.reconstruct_pairs(
            _row("q", [("a", "wrong")]),
            "baseline",
            {"a": _child("a", "rev-1", "child text")},
            {"a": ("rev-1", "parent")},
            {"a": "doc"},
        )


def test_pair_identity_drift_is_rejected() -> None:
    with pytest.raises(ValueError, match="pair identity"):
        audit.audit_counts(
            _row("q", [("a", "doc")], {}),
            [audit.Pair("other", "baseline", "a", "doc", "text")],
            0,
            FakeTokenizer(),
        )


def test_padding_is_assigned_per_actual_batch_and_truncation_is_counted() -> None:
    pairs = [audit.Pair("q", "baseline", str(index), "doc", "x" * index) for index in range(1, 12)]
    lengths = audit.pair_lengths("query", pairs, FakeTokenizer(), batch_size=8)
    assert lengths[:8] == [(index + 2, index + 2, 10) for index in range(1, 9)]
    assert lengths[8:] == [(11, 10, 10), (12, 10, 10), (13, 10, 10)]

    compute = {
        "neural_pairs": 11,
        "input_chunks": 11,
        "input_characters": sum(range(1, 12)),
        "expanded_parent_count": 0,
        "encoded_tokens": sum(item[1] for item in lengths),
        "padded_tokens": sum(item[2] for item in lengths),
        "truncated_pairs": sum(a > b for a, b, _ in lengths),
        "max_untruncated_tokens": 13,
    }
    tokenizer = FakeTokenizer()

    audited = audit.audit_counts(
        {
            **_row("q", [(str(index), "doc") for index in range(1, 12)], compute),
        },
        pairs,
        0,
        tokenizer,
        batch_size=8,
    )

    assert audited == lengths
    assert tokenizer.calls[:4] == [(8, False), (8, True), (3, False), (3, True)]


def test_malformed_tokenizer_batch_is_rejected() -> None:
    with pytest.raises(ValueError, match="malformed batch"):
        audit.audit_counts(
            _row("q", [("a", "doc")], {}),
            [audit.Pair("q", "baseline", "a", "doc", "text")],
            0,
            FakeTokenizer(malformed=True),
            batch_size=8,
        )


@pytest.mark.parametrize("field", audit.COUNT_FIELDS)
def test_historical_compute_drift_is_rejected(field: str) -> None:
    compute = {
        "neural_pairs": 1,
        "input_chunks": 1,
        "input_characters": 4,
        "expanded_parent_count": 0,
        "encoded_tokens": 4,
        "padded_tokens": 4,
        "truncated_pairs": 0,
        "max_untruncated_tokens": 4,
    }
    compute[field] += 1

    with pytest.raises(ValueError, match=field):
        audit.audit_counts(
            _row("q", [("a", "doc")], compute),
            [audit.Pair("q", "baseline", "a", "doc", "text")],
            0,
            FakeTokenizer(body_limit=8),
            8,
        )


def test_summary_distribution_uses_linear_percentiles() -> None:
    distribution = audit.distribution([1, 2, 3, 4])

    assert distribution == {
        "total": 10,
        "mean": 2.5,
        "p50": 2.5,
        "p95": 3.8499999999999996,
        "max": 4,
    }


def test_pair_lengths_returns_counts_only() -> None:
    pairs = [audit.Pair("q", "parent", "pair", "doc", "private source text")]
    lengths = audit.pair_lengths("query", pairs, FakeTokenizer(), batch_size=8)

    assert lengths == [(21, 10, 10)]


def test_persisted_pair_shape_excludes_source_text() -> None:
    record = {
        "query_id": "q",
        "arm": "parent",
        "pair_id": "rev",
        "source_document_id": "doc",
        "input_characters": 7,
        "untruncated_tokens": 9,
        "encoded_tokens": 7,
        "padded_tokens": 8,
    }
    assert "private" not in record.values()
    assert set(record) == {
        "query_id",
        "arm",
        "pair_id",
        "source_document_id",
        "input_characters",
        "untruncated_tokens",
        "encoded_tokens",
        "padded_tokens",
    }


def test_parent_qualification_requires_zero_truncation() -> None:
    queries = {f"q{index}" for index in range(2)}
    rows = []
    for query_id in queries:
        for arm in audit.ARMS:
            truncated = query_id == "q0" and arm == "parent"
            rows.append(
                {
                    "query_id": query_id,
                    "arm": arm,
                    "pair_id": f"{query_id}-{arm}",
                    "source_document_id": "doc",
                    "input_characters": 4,
                    "untruncated_tokens": 6,
                    "encoded_tokens": 5 if truncated else 6,
                    "padded_tokens": 6,
                }
            )

    summary = audit.summarize(rows, {"baseline": {"rev"}, "parent": {"rev"}}, queries)

    assert summary["arms"]["parent"]["parent_truncation_zero"] is False
    assert summary["arms"]["parent"]["full_context_qualified"] is False
