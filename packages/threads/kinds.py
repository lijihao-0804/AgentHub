"""Which application surface a thread belongs to.

A kind is a routing label and nothing more. No runtime behaviour branches on
it: an incident thread and a research thread execute through the same graph,
the same policy gate and the same approval flow. What the kind decides is which
page reads the thread and what that page draws beside the conversation.

It is stored rather than inferred from the agent because the agent can be
renamed, re-pointed or replaced, and because a workspace may reasonably run two
different incident agents. The thread is the durable unit of work, so the label
belongs on the thread.
"""

from __future__ import annotations

from packages.core.errors.exceptions import AgentHubError

GENERAL = "general"
RESEARCH = "research"
INCIDENT = "incident"
ANALYSIS = "analysis"
SUPPORT = "support"

THREAD_KINDS = frozenset({GENERAL, RESEARCH, INCIDENT, ANALYSIS, SUPPORT})

THREAD_KIND_UNKNOWN = "THREAD_KIND_UNKNOWN"


def validate_kind(kind: str) -> str:
    if kind not in THREAD_KINDS:
        raise AgentHubError(THREAD_KIND_UNKNOWN, "The thread kind is not supported.", 422)
    return kind


__all__ = [
    "ANALYSIS",
    "GENERAL",
    "INCIDENT",
    "RESEARCH",
    "SUPPORT",
    "THREAD_KINDS",
    "THREAD_KIND_UNKNOWN",
    "validate_kind",
]
