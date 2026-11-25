"""Unit tests for fail-closed semantic hybrid retrieval in Memory and Thread History."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from packages.core.semantic_scoring import SemanticTextScorer
from packages.memory.store import ACTIVE, SqlAlchemyMemoryStore
from packages.threads.context import SqlAlchemyThreadContextProvider


class _SemanticStubEmbedder:
    """Deterministic stub mapping seating/budget/other topics to orthogonal vectors."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.doc_calls = 0

    def _vec(self, text: str) -> tuple[float, ...]:
        lower = text.lower()
        if any(k in lower for k in ("window", "seat", "靠窗", "选座")):
            return (1.0, 0.0, 0.0)
        if any(k in lower for k in ("1200", "预算", "价格上限", "budget", "price cap")):
            return (0.0, 1.0, 0.0)
        return (0.0, 0.0, 1.0)

    def embed_documents(self, texts):  # noqa: ANN001
        if self.fail:
            raise RuntimeError("embedder unavailable")
        self.doc_calls += len(texts)
        return tuple(self._vec(t) for t in texts)

    def embed_query(self, text: str) -> tuple[float, ...]:
        if self.fail:
            raise RuntimeError("embedder unavailable")
        return self._vec(text)


def test_semantic_text_scorer_caches_vectors_and_fails_closed() -> None:
    emb = _SemanticStubEmbedder()
    scorer = SemanticTextScorer(emb, max_cache_entries=32)

    s1 = scorer.score_documents("订机票选座", ["Prefer window seats", "Likes green tea"])
    assert s1 == (1.0, 0.0)
    assert emb.doc_calls == 2

    # Second call with same docs reuses content-hash cache (0 new embed_documents calls)
    s2 = scorer.score_documents("window seat", ["Prefer window seats", "Likes green tea"])
    assert s2 == (1.0, 0.0)
    assert emb.doc_calls == 2

    failing = SemanticTextScorer(_SemanticStubEmbedder(fail=True))
    assert failing.score_documents("test", ["doc"]) is None


@pytest.mark.asyncio
async def test_memory_store_semantic_synonym_recall_isolation_and_fallback() -> None:
    ws_id, agent_id, user_id = uuid4(), uuid4(), uuid4()
    now = datetime.now(UTC)
    rows = [
        SimpleNamespace(
            id=uuid4(),
            kind="FACT",
            content="Likes green tea in the afternoon.",
            salience=5,
            content_hash="h1",
            created_at=now - timedelta(hours=1),
            status=ACTIVE,
        ),
        SimpleNamespace(
            id=uuid4(),
            kind="PREFERENCE",
            content="Prefer window seats on flights.",
            salience=1,
            content_hash="h2",
            created_at=now - timedelta(hours=2),
            status=ACTIVE,
        ),
    ]
    captured_params: list[dict] = []

    class _FakeScalarResult:
        def all(self):
            return list(rows)

    class _FakeSession:
        async def scalars(self, stmt):  # noqa: ANN001
            captured_params.append(dict(stmt.compile().params))
            return _FakeScalarResult()

    @asynccontextmanager
    async def sf():
        yield _FakeSession()

    # With semantic embedder: zero lexical overlap ("订机票选座" vs "window seats") still ranks #1!
    store = SqlAlchemyMemoryStore(sf, embedder=_SemanticStubEmbedder())
    selected = await store.select(
        workspace_id=ws_id,
        agent_id=agent_id,
        owner_user_id=user_id,
        query="订机票注意一下选座",
        limit=2,
    )
    assert selected[0].content == "Prefer window seats on flights."
    # Verify strict SQL tenant + agent + user isolation parameters were bound
    param_values = set(captured_params[-1].values())
    assert ws_id in param_values
    assert agent_id in param_values
    assert user_id in param_values
    assert ACTIVE in param_values

    # Fail-closed fallback: when embedder raises, degrades to legacy salience ordering without error
    failing_store = SqlAlchemyMemoryStore(sf, embedder=_SemanticStubEmbedder(fail=True))
    degraded = await failing_store.select(
        workspace_id=ws_id,
        agent_id=agent_id,
        owner_user_id=user_id,
        query="订机票注意一下选座",
        limit=2,
    )
    assert degraded[0].content == "Likes green tea in the afternoon."


@pytest.mark.asyncio
async def test_thread_history_semantic_synonym_recall_and_fallback() -> None:
    ws_id, thread_id = uuid4(), uuid4()
    turn_budget = (
        SimpleNamespace(sequence=1, user_input="这次出差预算卡在 1200 元以内"),
        SimpleNamespace(final_output="好的，我会优先筛选 1200 元以内的航班。"),
    )
    turn_other = (
        SimpleNamespace(sequence=2, user_input="帮我看一下今天天气"),
        SimpleNamespace(final_output="今天晴，22 度。"),
    )
    captured_sql: list[tuple[str, dict]] = []

    class _FakeResult:
        def __init__(self, data) -> None:
            self._data = data

        def all(self):
            return list(self._data)

    class _FakeSession:
        async def execute(self, stmt):  # noqa: ANN001
            compiled = stmt.compile()
            captured_sql.append((str(compiled), dict(compiled.params)))
            # Simulate ILIKE path returning empty when no lexical match
            if "ILIKE" in str(compiled).upper():
                return _FakeResult([])
            return _FakeResult([turn_other, turn_budget])

    @asynccontextmanager
    async def sf():
        yield _FakeSession()

    # Semantic hybrid path recalls turn 1 despite zero lexical overlap ("价格上限" vs "1200/预算")
    provider = SqlAlchemyThreadContextProvider(sf, embedder=_SemanticStubEmbedder())
    hits = await provider.search_before_sequence(
        workspace_id=ws_id,
        thread_id=thread_id,
        sequence=5,
        query="上次定的价格上限是多少",
        limit=2,
    )
    assert len(hits) == 1
    assert hits[0].sequence == 1
    assert "1200" in hits[0].user_input

    # Strict workspace_id, thread_id, and sequence < 5 isolation verified on SQL query
    sql_text, params = captured_sql[-1]
    assert ws_id in params.values()
    assert thread_id in params.values()
    assert 5 in params.values()
    assert "<" in sql_text

    # Fail-closed fallback: when embedder raises, cleanly falls back to legacy ILIKE query
    failing_provider = SqlAlchemyThreadContextProvider(
        sf, embedder=_SemanticStubEmbedder(fail=True)
    )
    fallback_hits = await failing_provider.search_before_sequence(
        workspace_id=ws_id,
        thread_id=thread_id,
        sequence=5,
        query="上次定的价格上限是多少",
        limit=2,
    )
    assert fallback_hits == ()
