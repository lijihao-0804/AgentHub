"""Tools the runtime itself provides, rather than the workspace catalog.

``search_thread_history`` is not a published ToolRevision and is not bound to
an agent through the tool catalog, and that is a decision rather than a
shortcut. A catalog tool is governed because it reaches outside the run:
another system, another tenant's data, a side effect someone must approve.
This tool reaches nowhere. It reads finished turns of the run's own thread, in
the run's own workspace — data the runtime was already allowed to replay
verbatim into the prompt a moment earlier, and would have replayed if the
window had been one turn longer. Governing it would mean asking an operator to
approve the agent reading its own conversation.

What it is gated on instead is the published ``runtime.memory`` block, so the
capability still belongs to a specific agent version and still shows up in the
spec hash. An agent that never turned it on never sees the tool, and a run
outside a thread never sees it either, which is what keeps single-shot
Playground runs byte-identical to what they were.
"""

from __future__ import annotations

from typing import Any
from uuid import NAMESPACE_URL, uuid5

from packages.core.canonical.json_hash import canonical_json_hash
from packages.tools.contracts import (
    ToolApprovalPolicy,
    ToolDefinition,
    ToolEffect,
    ToolRisk,
)

THREAD_HISTORY_SEARCH = "search_thread_history"

MAX_SEARCH_RESULTS = 5

_DESCRIPTION = (
    "Search earlier turns of the current conversation that are no longer in "
    "your context. Only the most recent turns are replayed to you; if the user "
    "refers to something decided earlier, or you are about to say you do not "
    "remember, call this first with the keywords you are looking for. Returns "
    "the matching questions and answers verbatim, or an empty list if the "
    "conversation never contained them."
)

_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200,
            "description": "Keywords to look for in earlier turns.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_SEARCH_RESULTS,
            "description": f"How many turns to return, at most {MAX_SEARCH_RESULTS}.",
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}

# Stable identifiers, derived rather than stored. The runtime provides this
# tool from code, so there is no revision row to point at; a name-derived UUID
# and a content hash give the audit trail the same two things a catalog tool
# gives it — something to group calls by, and something that changes when the
# contract changes.
_REVISION_ID = uuid5(NAMESPACE_URL, f"agenthub:runtime-tool:{THREAD_HISTORY_SEARCH}")


def thread_history_search_definition() -> ToolDefinition:
    spec_hash = canonical_json_hash(
        {
            "identity": THREAD_HISTORY_SEARCH,
            "description": _DESCRIPTION,
            "input_schema": _INPUT_SCHEMA,
            "effect": ToolEffect.READ.value,
        }
    )
    return ToolDefinition(
        identity=THREAD_HISTORY_SEARCH,
        revision_id=_REVISION_ID,
        spec_hash=spec_hash,
        description=_DESCRIPTION,
        input_schema=dict(_INPUT_SCHEMA),
        effect=ToolEffect.READ,
        risk_level=ToolRisk.LOW,
        # Never. Approving an agent to read the conversation it is already
        # holding would be a dialog with only one sensible answer, and a dialog
        # with only one sensible answer trains people to stop reading dialogs.
        approval_policy=ToolApprovalPolicy.NEVER,
        timeout_seconds=10,
    )


def history_hint(*, turns_dropped_by_window: int) -> str:
    """The line that tells the model the tool is worth reaching for.

    A tool the model does not know it needs is a tool the model does not call.
    The count is the one from PREPARE, so the hint states a fact about this run
    rather than a general possibility.
    """

    if turns_dropped_by_window > 0:
        return (
            f"{turns_dropped_by_window} earlier turn(s) of this conversation are not "
            f"shown above. Use the `{THREAD_HISTORY_SEARCH}` tool to look them up "
            "instead of answering that you do not remember."
        )
    return (
        "Earlier turns of this conversation may be trimmed from your context as it "
        f"grows. Use the `{THREAD_HISTORY_SEARCH}` tool to look them up instead of "
        "answering that you do not remember."
    )


__all__ = [
    "MAX_SEARCH_RESULTS",
    "THREAD_HISTORY_SEARCH",
    "history_hint",
    "thread_history_search_definition",
]
