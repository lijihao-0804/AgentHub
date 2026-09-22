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
    MAX_EVIDENCE_LENGTH,
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

EXTRACTION_SYSTEM_PROMPT = (
    "You extract durable shared workspace memories from one user's message.\n"
    "\n"
    "Remember ONLY team, project, or workflow statements that will still be true "
    "and useful in a different conversation next week: shared preferences, "
    "project constraints, decisions, and durable operational facts. This is "
    "workspace-scoped memory, not personal user memory.\n"
    "\n"
    "Do NOT remember: anything specific to this one question, any assistant "
    "answer or reasoning, greetings, temporary state, guesses, personal profile "
    "facts, or anything the user did not actually assert.\n"
    "\n"
    "Most exchanges contain nothing worth remembering. Returning an empty array "
    "is the correct and expected answer, and is much better than inventing "
    "something.\n"
    "\n"
    "Reply with JSON only, no prose and no code fence, in exactly this shape:\n"
    '{"memories": [{"content": "<one short standalone sentence>", '
    '"kind": "FACT|PREFERENCE|DECISION|CONSTRAINT", '
    '"evidence": "<exact non-empty quote from the user message>"}]}\n'
    f"At most {MAX_MEMORIES_PER_TURN} entries. Each content under "
    f"{MAX_MEMORY_LENGTH} characters, written so it makes sense on its own "
    "without this conversation. The evidence must be an exact quote from the "
    "user message, no longer than "
    f"{MAX_EVIDENCE_LENGTH} characters. Write each memory in the language the "
    "user used."
)


@dataclass(frozen=True, slots=True)
class TurnForExtraction:
    user_input: str
    # Kept as an optional source-compatible field for callers that still build
    # the old DTO. It is deliberately never sent to the extractor.
    final_output: str | None = None


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
                    {"user": _excerpt(turn.user_input, MAX_PROMPT_INPUT)},
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


def parse_candidates(
    response: ModelResponse, *, user_input: str | None = None
) -> tuple[MemoryCandidate, ...]:
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
        evidence = entry.get("evidence")
        if not isinstance(content, str) or not isinstance(kind, str):
            continue
        if (
            not isinstance(user_input, str)
            or not isinstance(evidence, str)
            or not evidence
            or len(evidence) > MAX_EVIDENCE_LENGTH
            or evidence not in user_input
        ):
            # One malformed candidate must not discard valid siblings.
            continue
        if kind.strip().upper() not in MEMORY_KINDS:
            kind = "FACT"
        candidates.append(
            MemoryCandidate(
                content=content,
                kind=kind.strip().upper(),
                evidence=evidence,
            )
        )
    return tuple(candidates)


__all__ = [
    "EXTRACTION_SYSTEM_PROMPT",
    "TurnForExtraction",
    "extraction_request",
    "parse_candidates",
]
