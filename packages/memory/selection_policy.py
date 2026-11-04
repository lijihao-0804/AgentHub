"""Explicit, versioned post-selection policies for long-term memory.

These policies only filter the candidates returned by the existing selector.
They do not change ranking, persistence, or snapshot replay, and they are not
evidence that a surviving memory is semantically relevant to the query.
"""

from __future__ import annotations

from collections.abc import Sequence

from packages.core.text import search_terms
from packages.memory.contracts import SelectedMemory

LEXICAL_OVERLAP_POLICY_V1 = "lexical_overlap_v1"
SUPPORTED_MEMORY_SELECTION_POLICIES = frozenset({LEXICAL_OVERLAP_POLICY_V1})
MEMORY_SELECTION_LIMITATION_CODES = (
    "SYNONYMS_OR_PARAPHRASES_MAY_BE_MISSED",
    "CROSS_LANGUAGE_EQUIVALENTS_MAY_BE_MISSED",
)


class InvalidMemorySelectionPolicy(ValueError):
    """The frozen spec named a policy this runtime cannot apply safely."""


def apply_memory_selection_policy(
    selected: Sequence[SelectedMemory],
    *,
    query: str,
    policy: str | None,
) -> tuple[SelectedMemory, ...]:
    """Filter selected memories using the shared search-term rules.

    The legacy path returns the original sequence unchanged. The lexical
    policy keeps only memories whose case-folded content contains at least one
    query term. An empty or term-free query has no lexical overlap and
    therefore selects no memories.
    """

    candidates = tuple(selected)
    if policy is None:
        return candidates
    if not isinstance(policy, str) or policy not in SUPPORTED_MEMORY_SELECTION_POLICIES:
        raise InvalidMemorySelectionPolicy(f"Unsupported memory selection policy: {policy!r}")
    if not isinstance(query, str):
        raise TypeError("memory selection query must be a string")
    terms = search_terms(query)
    if not terms:
        return ()
    return tuple(
        item for item in candidates if any(term in item.content.casefold() for term in terms)
    )


__all__ = [
    "InvalidMemorySelectionPolicy",
    "LEXICAL_OVERLAP_POLICY_V1",
    "MEMORY_SELECTION_LIMITATION_CODES",
    "SUPPORTED_MEMORY_SELECTION_POLICIES",
    "apply_memory_selection_policy",
]
