"""Research-only text request counter for the evaluated DeepSeek V4.1 route.

The restricted prompt layout follows DeepSeek's MIT-licensed V4.1 reference
encoder, revision 9f9818127fdfbb60feba204799d617a874b32a86. It supports only
the plain system/user messages used in the M4 study. Tools, images, assistant
turns, alternative providers, and future server template changes are not
covered. This module is not wired into AgentHub's frozen runtime policy.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from packages.model_gateway.contracts import ModelRequest

MODEL = "deepseek-v4.1-flash-1017"
TOKENIZER_REVISION = "9f9818127fdfbb60feba204799d617a874b32a86"
TOKENIZER_SHA256 = "c90dfa01249db1be4245780a052ede752e1361c612ac6d08e2bdada7d599476b"
MAX_TOKENIZER_BYTES = 8 * 1024 * 1024
MAX_MESSAGE_BYTES = 4 * 1024 * 1024
_RESERVED_MARKERS = ("<｜", "<think>", "</think>", "<tool_result>", "</tool_result>")


class DeepseekV41TextCounter:
    """Count one known text-only encoding, not a provider-independent bound."""

    def __init__(self, tokenizer: Any) -> None:
        if not callable(getattr(tokenizer, "encode", None)):
            raise TypeError("tokenizer must implement encode")
        self._tokenizer = tokenizer
        self._verified_source = False

    @classmethod
    def from_file(cls, path: Path) -> DeepseekV41TextCounter:
        """Load the pinned upstream tokenizer data without executing remote code."""
        from tokenizers import Tokenizer

        with path.open("rb") as source:
            data = source.read(MAX_TOKENIZER_BYTES + 1)
        if len(data) > MAX_TOKENIZER_BYTES or hashlib.sha256(data).hexdigest() != TOKENIZER_SHA256:
            raise ValueError("DEEPSEEK_TOKENIZER_IDENTITY_MISMATCH")
        counter = cls(Tokenizer.from_str(data.decode("utf-8")))
        counter._verified_source = True
        return counter

    @property
    def verified_source(self) -> bool:
        """Whether this instance loaded the pinned local tokenizer file."""
        return self._verified_source

    def count(
        self,
        request: ModelRequest,
        *,
        thinking: bool,
        reasoning_effort: int | None = None,
    ) -> int:
        """Count the restricted format; callers must bind route and mode separately."""
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
        ):
            raise ValueError("DEEPSEEK_TEXT_REQUEST_UNSUPPORTED")
        if not 1 <= len(request.messages) <= 128:
            raise ValueError("a bounded sequence of messages is required")
        items = request.messages
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
        return len(self._tokenizer.encode(prompt, add_special_tokens=False).ids)
