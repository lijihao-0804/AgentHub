from __future__ import annotations

import pytest

from benchmarks.retrieval.runner import RealRetrievalEnvironmentError, _raise_environment_blocker
from packages.knowledge.contracts import KnowledgeProviderError


@pytest.mark.parametrize(
    "code",
    ["QDRANT_UNAVAILABLE", "EMBEDDER_LOAD_FAILED", "RERANKER_LOAD_FAILED"],
)
def test_known_real_environment_provider_errors_are_blocked(code: str) -> None:
    with pytest.raises(RealRetrievalEnvironmentError) as raised:
        _raise_environment_blocker(
            KnowledgeProviderError(code, "safe provider error"), stage="smoke"
        )

    assert raised.value.reason_code == code
    assert raised.value.stage == "smoke"
    assert raised.value.safe_message == "safe provider error"


def test_terminal_provider_error_is_not_hidden_as_environment_blocker() -> None:
    error = KnowledgeProviderError("INVALID_PROVIDER_RESULT", "invalid result")
    with pytest.raises(KnowledgeProviderError) as raised:
        _raise_environment_blocker(error, stage="smoke")
    assert raised.value is error


@pytest.mark.parametrize("error", [KeyError("unexpected"), AssertionError("unexpected")])
def test_unexpected_benchmark_errors_are_reraised(error: Exception) -> None:
    with pytest.raises(type(error)) as raised:
        _raise_environment_blocker(error, stage="smoke")
    assert raised.value is error
