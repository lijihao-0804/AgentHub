"""Deciding what, if anything, one finished turn is worth remembering.

This runs after the user already has their answer, in a different process, and
it is allowed to fail. Nothing here may raise into a run: a turn that succeeded
and then failed to produce a memory is a turn that succeeded.

The prompt asks for durable statements only and the code does not trust the
answer. Everything that comes back goes through the write gate in
``packages.memory.store``; this module's job is to get a small JSON array out
of a model and turn it into candidates.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from packages.memory.contracts import (
    MAX_MEMORIES_PER_TURN,
    MAX_MEMORY_LENGTH,
    MemoryCandidate,
)
from packages.memory.models import MEMORY_KINDS
from packages.model_gateway.contracts import ModelMessage, ModelRequest, ModelResponse

logger = logging.getLogger(__name__)

# Long turns are truncated before extraction. A memory is a sentence about the
# turn, not a compression of it, so the first part of each side is enough and
# sending the whole thing would make a background job cost as much as the run.
MAX_PROMPT_INPUT = 2_000
MAX_PROMPT_OUTPUT = 4_000

EXTRACTION_SYSTEM_PROMPT = (
    "You extract durable memories from one finished exchange between a user and "
    "an assistant.\n"
    "\n"
    "Remember ONLY statements that will still be true and useful in a different "
    "conversation next week: stable user preferences, project constraints, "
    "decisions that were made, and durable facts about the user's situation.\n"
    "\n"
    "Do NOT remember: anything specific to this one question, the assistant's "
    "own answer or reasoning, greetings, temporary state, anything you are "
    "guessing at, or anything the user did not actually assert.\n"
    "\n"
    "Most exchanges contain nothing worth remembering. Returning an empty array "
    "is the correct and expected answer, and is much better than inventing "
    "something.\n"
    "\n"
    "Reply with JSON only, no prose and no code fence, in exactly this shape:\n"
    '{"memories": [{"content": "<one short standalone sentence>", '
    '"kind": "FACT|PREFERENCE|DECISION|CONSTRAINT"}]}\n'
    f"At most {MAX_MEMORIES_PER_TURN} entries. Each content under "
    f"{MAX_MEMORY_LENGTH} characters, written so it makes sense on its own "
    "without this conversation. Write each memory in the language the user used."
)


@dataclass(frozen=True, slots=True)
class TurnForExtraction:
    user_input: str
    final_output: str


def _excerpt(value: str, limit: int) -> str:
    value = (value or "").strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def extraction_request(turn: TurnForExtraction) -> ModelRequest:
    return ModelRequest(
        messages=(
            ModelMessage(role="system", content=EXTRACTION_SYSTEM_PROMPT),
            ModelMessage(
                role="user",
                content=json.dumps(
                    {
                        "user": _excerpt(turn.user_input, MAX_PROMPT_INPUT),
                        "assistant": _excerpt(turn.final_output, MAX_PROMPT_OUTPUT),
                    },
                    ensure_ascii=False,
                ),
            ),
        )
    )


def _strip_fence(content: str) -> str:
    """Take the JSON object out of whatever the model wrapped it in.

    Asking for "JSON only" is a request, not a guarantee. Rather than fail the
    whole extraction on a stray code fence or a sentence of preamble, find the
    outermost braces and parse that.
    """

    text = (content or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return ""
    return text[start : end + 1]


def parse_candidates(response: ModelResponse) -> tuple[MemoryCandidate, ...]:
    payload = _strip_fence(response.content)
    if not payload:
        return ()
    try:
        parsed = json.loads(payload)
    except ValueError:
        logger.info("memory_extraction_unparseable")
        return ()
    if not isinstance(parsed, dict):
        return ()
    entries = parsed.get("memories")
    if not isinstance(entries, list):
        return ()
    candidates: list[MemoryCandidate] = []
    for entry in entries[: MAX_MEMORIES_PER_TURN * 2]:
        if not isinstance(entry, dict):
            continue
        content = entry.get("content")
        kind = entry.get("kind", "FACT")
        if not isinstance(content, str) or not isinstance(kind, str):
            continue
        if kind.strip().upper() not in MEMORY_KINDS:
            kind = "FACT"
        candidates.append(MemoryCandidate(content=content, kind=kind.strip().upper()))
    return tuple(candidates)


__all__ = [
    "EXTRACTION_SYSTEM_PROMPT",
    "TurnForExtraction",
    "extraction_request",
    "parse_candidates",
]
