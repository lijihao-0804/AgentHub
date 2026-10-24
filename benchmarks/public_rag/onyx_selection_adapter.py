"""Benchmark shim: the Onyx selection logic lives in the production adapter.

Kept as a module so existing benchmark imports and result provenance stay
stable; every name re-exports the single production copy in
``packages/knowledge/adapters/onyx_selection.py`` so research and production
cannot drift apart.
"""

from __future__ import annotations

from packages.knowledge.adapters.onyx_selection import (
    MAX_CHUNKS_PER_SECTION,
    MAX_SELECTED_SECTIONS,
    SYSTEM_PROMPT,
    build_sections,
    model_sections,
    parse_selection,
    promote_evidence,
    promote_selection,
    sections_from_evidence,
    selection_messages,
)

__all__ = [
    "MAX_CHUNKS_PER_SECTION",
    "MAX_SELECTED_SECTIONS",
    "SYSTEM_PROMPT",
    "build_sections",
    "model_sections",
    "parse_selection",
    "promote_evidence",
    "promote_selection",
    "sections_from_evidence",
    "selection_messages",
]
