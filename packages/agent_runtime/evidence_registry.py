"""Resolve only model-visible evidence against the run's private source registry."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from packages.core.errors.exceptions import AgentHubError
from packages.knowledge.evidence_plan import (
    EVIDENCE_VIEW_FORMAT,
    assemble_evidence_plan,
    model_evidence_locator,
    validate_evidence_view,
)
from packages.model_gateway.contracts import ModelMessage

# Tool identities whose successful payloads are bound evidence views. Both are
# projected by the observation layer onto the same private registry, so
# admission re-validates them identically.
RAG_EVIDENCE_TOOL_IDENTITIES = frozenset({"search_knowledge", "query_wiki"})

_READING_FIELDS = (
    "snippet",
    "document_name",
    "effective_date",
    "superseded",
    "locator",
    "origin",
    "wiki_page_id",
)


def visible_evidence_registry(
    messages: Sequence[ModelMessage], registry: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    """Retain exact visible source fragments; fail closed on a broken mapping.

    Full source identities stay in checkpoint state, outside provider messages.
    This function is also run after final context admission, which may remove
    individual records or an entire old tool exchange.
    """
    visible: dict[str, dict[str, Any]] = {}
    for message in messages:
        if message.role != "tool" or message.name not in RAG_EVIDENCE_TOOL_IDENTITIES:
            continue
        try:
            payload = json.loads(message.content or "null")
        except (TypeError, ValueError):
            raise _mapping_error() from None
        if not isinstance(payload, dict) or payload.get("evidence_format") != EVIDENCE_VIEW_FORMAT:
            if isinstance(payload, dict) and "results" in payload:
                raise _mapping_error()
            continue
        if not validate_evidence_view(payload):
            raise _mapping_error()
        for item in payload["results"]:
            identifier = item["id"]
            entry = registry.get(identifier)
            if identifier in visible or not isinstance(entry, Mapping):
                raise _mapping_error()
            reference, record = entry.get("reference"), entry.get("record")
            if not isinstance(reference, Mapping) or not isinstance(record, Mapping):
                raise _mapping_error()
            plan = assemble_evidence_plan(
                [record],
                [reference],
                budget=64_000,
                estimator=lambda text: len(text.encode("utf-8")),
            )
            if plan is None or not plan.registry:
                raise _mapping_error()
            expected = {"id": identifier}
            expected.update({key: record[key] for key in _READING_FIELDS if key in record})
            if "locator" in expected:
                expected["locator"] = model_evidence_locator(expected["locator"])
            if item != expected:
                raise _mapping_error()
            visible[identifier] = {"reference": dict(reference), "record": dict(record)}
    return visible


def _mapping_error() -> AgentHubError:
    return AgentHubError(
        "RAG_EVIDENCE_MAPPING_INVALID", "The visible evidence mapping is invalid.", 422
    )
