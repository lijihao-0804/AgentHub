"""The retrieval models are loaded at start, not inside a tool's timeout.

A `search_knowledge` call is given 30 seconds. Loading BGE-M3 and the
cross-encoder in a cold process took about 35, so the first retrieval after a
deploy failed with TOOL_TIMEOUT and the agent truthfully reported that it
could not find anything -- from a knowledge base that had the answer.

What is pinned here is the part that would silently regress: that warming
touches every model a query waits on, that one model failing to load does not
take the process down with it, and that it stays off unless a deployment asks
for it (turning it on downloads gigabytes).
"""

from __future__ import annotations

from typing import Any

import pytest

from packages.core.config.settings import Settings
from packages.knowledge import composition


class _Recorder:
    def __init__(self, explode: bool = False) -> None:
        self.warmed = 0
        self.explode = explode

    def warm(self) -> None:
        self.warmed += 1
        if self.explode:
            raise RuntimeError("no weights here")


class _NoWarm:
    """An adapter from before `warm` existed, or a test double."""


def _components(**parts: Any) -> composition.RetrievalComponents:
    return composition.RetrievalComponents(
        dense=parts.get("dense") or _Recorder(),
        sparse=parts.get("sparse") or _Recorder(),
        reranker=parts.get("reranker") or _Recorder(),
        index=object(),
    )


def test_warming_is_off_unless_a_deployment_asks_for_it() -> None:
    # It downloads ~3.3GB the first time. Someone running the API to look at a
    # page they never retrieve from should not pay that.
    assert Settings(testing=True).knowledge_warm_models_on_start is False


def test_every_model_a_query_waits_on_is_warmed(monkeypatch: pytest.MonkeyPatch) -> None:
    parts = {"dense": _Recorder(), "sparse": _Recorder(), "reranker": _Recorder()}
    monkeypatch.setattr(
        composition, "production_retrieval_components", lambda _settings: _components(**parts)
    )

    composition.warm_retrieval_components(Settings(testing=True))

    assert [part.warmed for part in parts.values()] == [1, 1, 1]


def test_a_model_that_will_not_load_does_not_stop_the_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The route that needs it will fail at the point of use, with a request id.
    # Refusing to start would take down every route that needs no retrieval.
    reranker = _Recorder()
    monkeypatch.setattr(
        composition,
        "production_retrieval_components",
        lambda _settings: _components(dense=_Recorder(explode=True), reranker=reranker),
    )

    composition.warm_retrieval_components(Settings(testing=True))

    assert reranker.warmed == 1


def test_an_adapter_without_a_warm_hook_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    reranker = _Recorder()
    monkeypatch.setattr(
        composition,
        "production_retrieval_components",
        lambda _settings: _components(dense=_NoWarm(), sparse=_NoWarm(), reranker=reranker),
    )

    composition.warm_retrieval_components(Settings(testing=True))

    assert reranker.warmed == 1


@pytest.mark.parametrize(
    "adapter",
    [
        "packages.knowledge.adapters.embeddings:BgeM3DenseEmbedder",
        "packages.knowledge.adapters.sparse:BgeM3SparseEncoder",
        "packages.knowledge.adapters.reranker:BgeReranker",
    ],
)
def test_the_real_adapters_expose_the_hook(adapter: str) -> None:
    module_name, class_name = adapter.split(":")
    module = __import__(module_name, fromlist=[class_name])

    assert callable(getattr(getattr(module, class_name), "warm", None))
