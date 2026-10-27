"""Pure validation for compact RAG evidence restored from completed history."""

from __future__ import annotations

import copy
import json
from collections.abc import Collection, Mapping, Sequence
from typing import Any

from packages.agent_runtime.evidence_registry import (
    RAG_EVIDENCE_TOOL_IDENTITIES,
    visible_evidence_registry,
)
from packages.core.errors.exceptions import AgentHubError
from packages.knowledge.evidence_plan import EVIDENCE_VIEW_FORMAT, validate_evidence_view
from packages.model_gateway.contracts import ModelMessage
from packages.wiki.evidence_envelope import (
    EVIDENCE_ORIGIN_WIKI_DERIVED,
    envelope_violation,
)

HISTORY_EVIDENCE_INVALID = "HISTORY_EVIDENCE_INVALID"
FORBIDDEN = "FORBIDDEN"

_REQUIRED_PERMISSIONS = frozenset({"knowledge_run", "tool_run"})
_SNAPSHOT_HASH_FIELDS = ("snapshot_hash", "snapshot_content_hash")


def validate_history_evidence(
    messages: Sequence[ModelMessage],
    registry: Mapping[str, Any],
    source_snapshots: Sequence[Mapping[str, Any]],
    current_snapshots: Sequence[Mapping[str, Any]],
    source_wiki: Sequence[Mapping[str, Any]],
    current_wiki: Sequence[Mapping[str, Any]],
    permissions: Collection[str],
) -> dict[str, dict[str, Any]]:
    """Return only compact historical evidence proven safe for this Run.

    ``source_snapshots`` and ``current_snapshots`` are the frozen knowledge
    bindings from the source and current Runs. Each binding names a
    ``(knowledge_base_id, snapshot_id)`` pair and one of ``snapshot_hash`` or
    ``snapshot_content_hash``. Wiki bindings name a knowledge base, publication
    id, and publication hash.

    Every historical RAG tool call must have one complete compact result.
    Error results remain valid history but contribute no evidence. For every
    visible registry entry, the same knowledge snapshot must be frozen in both
    Runs. A ``WIKI_DERIVED`` record additionally needs the same publication id
    and hash in both Wiki binding sets. Any missing, ambiguous, malformed, or
    mismatched input fails closed. The return value is a detached copy of the
    already visible registry; this function neither repairs nor adds evidence.
    """

    _require_permissions(permissions)
    try:
        values = _messages(messages)
        if not isinstance(registry, Mapping):
            raise _invalid()
        visible = _validate_compact_exchange(values, registry)
        source_snapshot_index = _snapshot_index(source_snapshots)
        current_snapshot_index = _snapshot_index(current_snapshots)
        source_wiki_index = _wiki_index(source_wiki)
        current_wiki_index = _wiki_index(current_wiki)
        _validate_bindings(
            visible,
            source_snapshot_index=source_snapshot_index,
            current_snapshot_index=current_snapshot_index,
            source_wiki_index=source_wiki_index,
            current_wiki_index=current_wiki_index,
        )
        verified = copy.deepcopy(visible)
        json.dumps(
            verified,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return verified
    except AgentHubError as error:
        if error.code == FORBIDDEN:
            raise
        raise _invalid() from None
    except (KeyError, RecursionError, TypeError, ValueError):
        raise _invalid() from None


def _require_permissions(permissions: Collection[str]) -> None:
    if isinstance(permissions, (str, bytes, bytearray, Mapping)) or not isinstance(
        permissions, Collection
    ):
        raise _forbidden()
    try:
        granted = set(permissions)
    except TypeError:
        raise _forbidden() from None
    if any(not isinstance(permission, str) for permission in granted):
        raise _forbidden()
    if not _REQUIRED_PERMISSIONS <= granted:
        raise _forbidden()


def _messages(messages: Sequence[ModelMessage]) -> tuple[ModelMessage, ...]:
    if isinstance(messages, (str, bytes, bytearray)) or not isinstance(messages, Sequence):
        raise _invalid()
    values = tuple(messages)
    if any(not isinstance(message, ModelMessage) for message in values):
        raise _invalid()
    return values


def _validate_compact_exchange(
    messages: tuple[ModelMessage, ...], registry: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    rag_calls: dict[str, str] = {}
    seen_call_ids: set[str] = set()
    for message in messages:
        if message.role != "assistant":
            continue
        for call in message.tool_calls:
            call_id = call.provider_tool_call_id
            if call_id is not None:
                if not isinstance(call_id, str) or not call_id or call_id in seen_call_ids:
                    raise _invalid()
                seen_call_ids.add(call_id)
            if call.name not in RAG_EVIDENCE_TOOL_IDENTITIES:
                continue
            if not isinstance(call_id, str) or not call_id:
                raise _invalid()
            rag_calls[call_id] = call.name

    seen_results: set[str] = set()
    for message in messages:
        if message.role != "tool":
            continue
        call_id = message.tool_call_id
        if message.name not in RAG_EVIDENCE_TOOL_IDENTITIES and call_id not in rag_calls:
            continue
        if (
            not isinstance(call_id, str)
            or call_id not in rag_calls
            or message.name != rag_calls[call_id]
            or call_id in seen_results
        ):
            raise _invalid()
        seen_results.add(call_id)
        try:
            payload = json.loads(message.content or "null")
        except (TypeError, ValueError):
            raise _invalid() from None
        if not isinstance(payload, Mapping):
            raise _invalid()
        if payload.get("status") == "ERROR":
            if payload.get("evidence_format") is not None or "results" in payload:
                raise _invalid()
            continue
        if payload.get("evidence_format") != EVIDENCE_VIEW_FORMAT:
            raise _invalid()
        if not validate_evidence_view(payload):
            raise _invalid()

    if seen_results != set(rag_calls):
        raise _invalid()
    try:
        visible = visible_evidence_registry(messages, registry)
    except AgentHubError:
        raise _invalid() from None
    if set(visible) != set(registry):
        raise _invalid()
    for entry in visible.values():
        if not isinstance(entry, Mapping) or set(entry) != {"reference", "record"}:
            raise _invalid()
        record = entry["record"]
        if envelope_violation(record) is not None:
            raise _invalid()
    return visible


def _snapshot_index(values: Sequence[Mapping[str, Any]]) -> dict[tuple[str, str], str]:
    result: dict[tuple[str, str], str] = {}
    for binding in _bindings(values):
        knowledge_base_id = _text(binding.get("knowledge_base_id"))
        snapshot_id = _text(binding.get("snapshot_id"))
        snapshot_hash = _hash_value(binding, _SNAPSHOT_HASH_FIELDS)
        key = (knowledge_base_id, snapshot_id)
        if key in result:
            raise _invalid()
        result[key] = snapshot_hash
    return result


def _wiki_index(values: Sequence[Mapping[str, Any]]) -> dict[tuple[str, str], str]:
    result: dict[tuple[str, str], str] = {}
    knowledge_bases: set[str] = set()
    for binding in _bindings(values):
        knowledge_base_id = _text(binding.get("knowledge_base_id"))
        publication_id = _text(binding.get("wiki_publication_id"))
        publication_hash = _text(binding.get("publication_hash"))
        key = (knowledge_base_id, publication_id)
        if key in result or knowledge_base_id in knowledge_bases:
            raise _invalid()
        knowledge_bases.add(knowledge_base_id)
        result[key] = publication_hash
    return result


def _bindings(values: Sequence[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
    if isinstance(values, (str, bytes, bytearray, Mapping)) or not isinstance(values, Sequence):
        raise _invalid()
    result = tuple(values)
    if any(not isinstance(value, Mapping) for value in result):
        raise _invalid()
    return result


def _hash_value(binding: Mapping[str, Any], fields: Sequence[str]) -> str:
    values = [_text(binding[field]) for field in fields if field in binding]
    if not values or len(set(values)) != 1:
        raise _invalid()
    return values[0]


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid()
    return value


def _validate_bindings(
    visible: Mapping[str, Mapping[str, Any]],
    *,
    source_snapshot_index: Mapping[tuple[str, str], str],
    current_snapshot_index: Mapping[tuple[str, str], str],
    source_wiki_index: Mapping[tuple[str, str], str],
    current_wiki_index: Mapping[tuple[str, str], str],
) -> None:
    for entry in visible.values():
        reference = entry["reference"]
        record = entry["record"]
        snapshot_key = (
            _text(reference.get("knowledge_base_id")),
            _text(reference.get("snapshot_id")),
        )
        if snapshot_key not in source_snapshot_index or snapshot_key not in current_snapshot_index:
            raise _invalid()
        if source_snapshot_index[snapshot_key] != current_snapshot_index[snapshot_key]:
            raise _invalid()
        if record.get("origin") != EVIDENCE_ORIGIN_WIKI_DERIVED:
            continue
        publication_id = _text(record.get("publication_id"))
        publication_hash = _text(record.get("publication_hash"))
        wiki_key = (snapshot_key[0], publication_id)
        if (
            source_wiki_index.get(wiki_key) != publication_hash
            or current_wiki_index.get(wiki_key) != publication_hash
        ):
            raise _invalid()


def _invalid() -> AgentHubError:
    return AgentHubError(
        HISTORY_EVIDENCE_INVALID,
        "Historical RAG evidence is unavailable.",
        422,
    )


def _forbidden() -> AgentHubError:
    return AgentHubError(
        FORBIDDEN,
        "Historical RAG evidence is not authorized.",
        403,
    )


__all__ = [
    "FORBIDDEN",
    "HISTORY_EVIDENCE_INVALID",
    "validate_history_evidence",
]
