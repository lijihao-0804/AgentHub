"""KV-cache-friendly prompt prefix helpers for the Agent runtime.

Follows the invariant-prefix / dynamic-suffix layout used by modern agent
runtimes (e.g. Claude Code's tail ``<system-reminder>`` pattern):
  1. Static tool definitions (deterministically ordered by identity)
  2. Static runtime policy & agent system prompt (invariant across turns)
  3. Conversation history (append-only prefix)
  4. Current user task + per-turn dynamic reminders (tail only)
"""

from __future__ import annotations

from collections.abc import Sequence

from packages.core.canonical.json_hash import canonical_json_hash
from packages.model_gateway.contracts import ModelMessage, ModelToolDefinition


def append_turn_reminders(user_text: str, reminders: Sequence[str]) -> str:
    """Attach per-turn dynamic reminders to the tail user message without mutating the prefix."""

    cleaned = tuple(item.strip() for item in reminders if isinstance(item, str) and item.strip())
    if not cleaned:
        return user_text
    block = "\n\n".join(cleaned)
    return f"{user_text}\n\n<turn_context>\n{block}\n</turn_context>"


def static_prefix_hash(
    tools: Sequence[ModelToolDefinition],
    system_messages: Sequence[ModelMessage],
) -> str:
    """Return a deterministic SHA-256 over the static tool + system prefix."""

    return canonical_json_hash(
        {
            "tools": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                }
                for tool in tools
            ],
            "system": [
                {"role": message.role, "content": message.content}
                for message in system_messages
            ],
        }
    )


__all__ = ["append_turn_reminders", "static_prefix_hash"]
