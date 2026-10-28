"""Pinned, offline DeepSeek V4.1 text request counter.

The encoder implements only the finite system/user plain-text template
validated by the M4 research calibration. Runtime admission additionally
requires the frozen provider, model, route allowlist, request mode, tokenizer
revision, and tokenizer digest to match. This is not a general token upper
bound for other routes or message forms.
"""

from __future__ import annotations

import hashlib
import os
import stat
from functools import lru_cache
from pathlib import Path
from typing import Any

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
from packages.core.errors.exceptions import AgentHubError
from packages.model_gateway.contracts import AliyunDeepSeekRequestMode, ModelRequest

MODEL = SUMMARY_NON_THINKING_MODEL
TOKENIZER_REVISION = SUMMARY_NON_THINKING_COUNTER_REVISION
TOKENIZERS_RUNTIME_VERSION = SUMMARY_NON_THINKING_COUNTER_RUNTIME_VERSION
TOKENIZER_SHA256 = SUMMARY_NON_THINKING_TOKENIZER_SHA256
MAX_TOKENIZER_BYTES = 8 * 1024 * 1024
MAX_MESSAGE_BYTES = 4 * 1024 * 1024
_RESERVED_MARKERS = ("<｜", "<think>", "</think>", "<tool_result>", "</tool_result>")


class DeepseekV41TextCounter:
    """Count the pinned plain-text message template with a local tokenizer."""

    def __init__(self, tokenizer: Any) -> None:
        if not callable(getattr(tokenizer, "encode", None)):
            raise TypeError("tokenizer must implement encode")
        self._tokenizer = tokenizer
        self._verified_source = False

    @classmethod
    def from_file(cls, path: Path) -> DeepseekV41TextCounter:
        """Load only the fixed tokenizer bytes from a local regular file."""
        if not isinstance(path, Path):
            raise TypeError("path must be a pathlib.Path")
        import tokenizers

        if tokenizers.__version__ != TOKENIZERS_RUNTIME_VERSION:
            raise ValueError("DEEPSEEK_TOKENIZERS_RUNTIME_VERSION_MISMATCH")

        with path.open("rb") as source:
            metadata = os.fstat(source.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_TOKENIZER_BYTES:
                raise ValueError("DEEPSEEK_TOKENIZER_ASSET_INVALID")
            data = source.read(MAX_TOKENIZER_BYTES + 1)
        if len(data) > MAX_TOKENIZER_BYTES or hashlib.sha256(data).hexdigest() != TOKENIZER_SHA256:
            raise ValueError("DEEPSEEK_TOKENIZER_IDENTITY_MISMATCH")
        tokenizer = tokenizers.Tokenizer.from_str(data.decode("utf-8"))
        counter = cls(tokenizer)
        counter._verified_source = True
        return counter

    @property
    def verified_source(self) -> bool:
        """Whether this instance loaded the digest-pinned local tokenizer."""
        return self._verified_source

    def count(
        self,
        request: ModelRequest,
        *,
        thinking: bool,
        reasoning_effort: int | None = None,
    ) -> int:
        """Count a benchmark format; use ``count_frozen_summary_request`` at runtime."""
        if type(thinking) is not bool:
            raise TypeError("thinking must be a bool")
        if reasoning_effort is not None and (
            type(reasoning_effort) is not int or not 1 <= reasoning_effort <= 100
        ):
            raise ValueError("reasoning effort must be an integer from 1 to 100")
        if not thinking and reasoning_effort is not None:
            raise ValueError("reasoning effort requires thinking mode")
        if not isinstance(request, ModelRequest):
            raise TypeError("request must be a ModelRequest")
        if (
            request.tools
            or request.response_schema is not None
            or request.required_capabilities.required
            or request.required_capabilities.max_context_tokens is not None
        ):
            raise ValueError("DEEPSEEK_TEXT_REQUEST_UNSUPPORTED")
        items = request.messages
        if not 1 <= len(items) <= 128:
            raise ValueError("a bounded sequence of messages is required")
        if items[-1].role != "user" or any(
            message.role not in {"system", "user"}
            or (message.role == "system" and index != 0)
            or not isinstance(message.content, str)
            or message.name is not None
            or message.tool_call_id is not None
            or bool(message.tool_calls)
            or any(marker in message.content for marker in _RESERVED_MARKERS)
            for index, message in enumerate(items)
        ):
            raise ValueError("DEEPSEEK_TEXT_MESSAGE_UNSUPPORTED")
        if sum(len(message.content.encode("utf-8")) for message in items) > MAX_MESSAGE_BYTES:
            raise ValueError("DEEPSEEK_TEXT_REQUEST_TOO_LARGE")

        prompt = "<｜begin▁of▁sentence｜>"
        if items[0].role == "system" or thinking:
            prompt += "<｜System｜>"
        if thinking:
            effort = 75 if reasoning_effort is None else reasoning_effort
            prompt += (
                f"Reasoning Effort: {effort} "
                "(range 1-100, the higher the value, the more thorough the reasoning)\n\n"
            )
        for message in items:
            if message.role == "user":
                prompt += "<｜User｜>"
            prompt += message.content
        prompt += "<｜Assistant｜>" + ("<think>" if thinking else "</think>")
        result = self._tokenizer.encode(prompt, add_special_tokens=False)
        tokens = getattr(result, "ids", None)
        if not isinstance(tokens, (list, tuple)):
            raise ValueError("DEEPSEEK_TOKENIZER_RESULT_INVALID")
        return len(tokens)

    def count_frozen_summary_request(
        self,
        request: ModelRequest,
        *,
        request_identity: Any,
        provider: str,
        model: str,
    ) -> int:
        """Count only the frozen M4 NON_THINKING route and complete text request."""
        if not self.verified_source:
            raise ValueError("DEEPSEEK_TOKENIZER_SOURCE_UNVERIFIED")
        if (
            not isinstance(request, ModelRequest)
            or request.request_mode is not AliyunDeepSeekRequestMode.NON_THINKING
            or request.retry_policy.max_attempts != 1
            or provider != SUMMARY_NON_THINKING_PROVIDER
            or model != SUMMARY_NON_THINKING_MODEL
        ):
            raise ValueError("DEEPSEEK_FROZEN_REQUEST_IDENTITY_MISMATCH")
        try:
            identity = frozen_summary_request_identity(request_identity)
        except (AgentHubError, TypeError, ValueError):
            raise ValueError("DEEPSEEK_FROZEN_REQUEST_IDENTITY_MISMATCH") from None
        expected = {
            "request_strategy_version": SUMMARY_NON_THINKING_STRATEGY_VERSION,
            "request_mode": SUMMARY_NON_THINKING_MODE,
            "request_route_allowlist": SUMMARY_NON_THINKING_ROUTE_ALLOWLIST,
            "request_counter_id": SUMMARY_NON_THINKING_COUNTER_ID,
            "request_counter_revision": SUMMARY_NON_THINKING_COUNTER_REVISION,
            "request_counter_runtime_version": SUMMARY_NON_THINKING_COUNTER_RUNTIME_VERSION,
            "request_counter_sha256": SUMMARY_NON_THINKING_TOKENIZER_SHA256,
            "request_counter_thinking": False,
        }
        if dict(request_identity) != expected or identity != expected:
            raise ValueError("DEEPSEEK_FROZEN_REQUEST_IDENTITY_MISMATCH")
        return self.count(request, thinking=False)


@lru_cache(maxsize=4)
def _load_cached(path: str) -> DeepseekV41TextCounter:
    return DeepseekV41TextCounter.from_file(Path(path))


def load_configured_deepseek_v41_counter(path: str | Path | None) -> DeepseekV41TextCounter | None:
    """Load a deployment-provided local asset; never resolve or download it remotely."""
    if path is None:
        return None
    try:
        resolved = Path(path).resolve(strict=True)
        if not resolved.is_absolute():
            return None
        return _load_cached(str(resolved))
    except Exception:
        return None


__all__ = [
    "DeepseekV41TextCounter",
    "MAX_MESSAGE_BYTES",
    "MAX_TOKENIZER_BYTES",
    "MODEL",
    "TOKENIZER_REVISION",
    "TOKENIZER_SHA256",
    "TOKENIZERS_RUNTIME_VERSION",
    "load_configured_deepseek_v41_counter",
]
