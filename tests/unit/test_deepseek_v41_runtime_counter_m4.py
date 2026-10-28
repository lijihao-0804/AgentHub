from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import tokenizers

from packages.agent_runtime import deepseek_v41_counter as counter_module
from packages.agent_runtime import runtime_service
from packages.agent_runtime.deepseek_v41_counter import (
    DeepseekV41TextCounter,
    load_configured_deepseek_v41_counter,
)
from packages.agent_runtime.frozen import (
    SUMMARY_NON_THINKING_COUNTER_ID,
    SUMMARY_NON_THINKING_COUNTER_REVISION,
    SUMMARY_NON_THINKING_COUNTER_RUNTIME_VERSION,
    SUMMARY_NON_THINKING_MODE,
    SUMMARY_NON_THINKING_MODEL,
    SUMMARY_NON_THINKING_PROVIDER,
    SUMMARY_NON_THINKING_ROUTE_ALLOWLIST,
    SUMMARY_NON_THINKING_STRATEGY_VERSION,
    SUMMARY_NON_THINKING_TOKENIZER_SHA256,
    frozen_summary_request_identity,
)
from packages.model_gateway.contracts import (
    AliyunDeepSeekRequestMode,
    ModelMessage,
    ModelRequest,
    RetryPolicy,
)


def _identity() -> dict[str, object]:
    return {
        "request_strategy_version": SUMMARY_NON_THINKING_STRATEGY_VERSION,
        "request_mode": SUMMARY_NON_THINKING_MODE,
        "request_route_allowlist": SUMMARY_NON_THINKING_ROUTE_ALLOWLIST,
        "request_counter_id": SUMMARY_NON_THINKING_COUNTER_ID,
        "request_counter_revision": SUMMARY_NON_THINKING_COUNTER_REVISION,
        "request_counter_runtime_version": SUMMARY_NON_THINKING_COUNTER_RUNTIME_VERSION,
        "request_counter_sha256": SUMMARY_NON_THINKING_TOKENIZER_SHA256,
        "request_counter_thinking": False,
    }


def _verified_test_counter(monkeypatch) -> DeepseekV41TextCounter:
    path = Path(__file__).resolve()
    data = path.read_bytes()
    import hashlib

    monkeypatch.setattr(counter_module, "TOKENIZER_SHA256", hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(
        tokenizers,
        "Tokenizer",
        SimpleNamespace(from_str=lambda _value: _RecordingTokenizer()),
    )
    return DeepseekV41TextCounter.from_file(path)


def test_local_asset_loader_requires_existing_digest_pinned_file(monkeypatch):
    assert load_configured_deepseek_v41_counter(None) is None
    assert (
        load_configured_deepseek_v41_counter(Path(__file__).with_name("missing-tokenizer.json"))
        is None
    )

    assert load_configured_deepseek_v41_counter(Path(__file__).resolve()) is None

    counter = _verified_test_counter(monkeypatch)
    assert isinstance(counter, DeepseekV41TextCounter)
    assert counter.verified_source is True


def test_runtime_count_binds_complete_plain_text_request_and_frozen_identity(
    monkeypatch,
):
    counter = _verified_test_counter(monkeypatch)
    request = ModelRequest(
        (
            ModelMessage("system", "fixed summary system"),
            ModelMessage("user", "full serialized user envelope"),
        ),
        retry_policy=RetryPolicy(max_attempts=1),
        request_mode=AliyunDeepSeekRequestMode.NON_THINKING,
    )

    count = counter.count_frozen_summary_request(
        request,
        request_identity=_identity(),
        provider=SUMMARY_NON_THINKING_PROVIDER,
        model=SUMMARY_NON_THINKING_MODEL,
    )
    assert count > 0
    assert counter._tokenizer.text == (
        "<｜begin▁of▁sentence｜><｜System｜>fixed summary system"
        "<｜User｜>full serialized user envelope<｜Assistant｜></think>"
    )

    for provider, model, identity in (
        ("deepseek", SUMMARY_NON_THINKING_MODEL, _identity()),
        (SUMMARY_NON_THINKING_PROVIDER, "deepseek-v4.1-flash", _identity()),
        (
            SUMMARY_NON_THINKING_PROVIDER,
            SUMMARY_NON_THINKING_MODEL,
            {**_identity(), "request_route_allowlist": "other_route"},
        ),
        (
            SUMMARY_NON_THINKING_PROVIDER,
            SUMMARY_NON_THINKING_MODEL,
            {**_identity(), "request_counter_sha256": "0" * 64},
        ),
    ):
        with pytest.raises(ValueError, match="IDENTITY_MISMATCH"):
            counter.count_frozen_summary_request(
                request,
                request_identity=identity,
                provider=provider,
                model=model,
            )


def test_provider_default_and_unverified_source_never_enter_v2_counter(monkeypatch):
    request = ModelRequest((ModelMessage("user", "full request"),))
    unverified = DeepseekV41TextCounter(_RecordingTokenizer())
    with pytest.raises(ValueError, match="SOURCE_UNVERIFIED"):
        unverified.count_frozen_summary_request(
            request,
            request_identity=_identity(),
            provider=SUMMARY_NON_THINKING_PROVIDER,
            model=SUMMARY_NON_THINKING_MODEL,
        )

    verified = _verified_test_counter(monkeypatch)
    with pytest.raises(ValueError, match="IDENTITY_MISMATCH"):
        verified.count_frozen_summary_request(
            request,
            request_identity=_identity(),
            provider=SUMMARY_NON_THINKING_PROVIDER,
            model=SUMMARY_NON_THINKING_MODEL,
        )


def test_frozen_strategy_contains_tokenizer_revision_and_digest():
    assert frozen_summary_request_identity(_identity()) == _identity()


def test_loader_rejects_a_different_tokenizers_runtime(monkeypatch):
    monkeypatch.setattr(tokenizers, "__version__", "0.22.3")
    with pytest.raises(ValueError, match="RUNTIME_VERSION_MISMATCH"):
        DeepseekV41TextCounter.from_file(Path(__file__).resolve())


def test_agent_run_service_composes_only_the_explicit_local_counter_path(monkeypatch):
    counter = _verified_test_counter(monkeypatch)
    requested_paths = []
    settings = SimpleNamespace(summary_deepseek_v41_tokenizer_path=Path("C:/local/tokenizer.json"))
    monkeypatch.setattr(runtime_service, "get_settings", lambda: settings)
    monkeypatch.setattr(
        runtime_service,
        "load_configured_deepseek_v41_counter",
        lambda path: requested_paths.append(path) or counter,
    )

    service = runtime_service.AgentRunService(object())

    assert requested_paths == [settings.summary_deepseek_v41_tokenizer_path]
    assert service.summary_request_counter is counter


class _RecordingTokenizer:
    def encode(self, text: str, *, add_special_tokens: bool):
        assert add_special_tokens is False
        self.text = text
        return SimpleNamespace(ids=list(range(max(1, len(text.split())))))
