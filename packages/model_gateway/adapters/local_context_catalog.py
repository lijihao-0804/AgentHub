"""Versioned, offline context metadata catalog.

The catalog is an adapter over an explicit local JSON artifact.  It contains
no provider discovery or network access and never guesses capabilities from a
model name.  Production may leave the catalog unconfigured.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from packages.model_gateway.context_metadata import (
    CONTEXT_METADATA_SCHEMA_VERSION,
    ContextMetadata,
    InputOutputSemantics,
    OutputReserveSemantics,
    context_metadata_from_mapping,
    context_metadata_to_mapping,
)

LOCAL_CONTEXT_CATALOG_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class LocalContextCatalogEntry:
    """One exact provider/model/deployment mapping from a fixed catalog."""

    provider: str
    model: str
    metadata: ContextMetadata
    max_context_tokens: int | None


@dataclass(frozen=True, slots=True)
class LocalContextCatalog:
    """Parsed immutable catalog with a content-derived identity."""

    schema_version: int
    catalog_id: str
    source_version: str
    source_label: str
    entries: Mapping[tuple[str, str, str], LocalContextCatalogEntry]
    content_sha256: str

    def resolve(
        self,
        *,
        provider: str,
        model: str,
        deployment_id: str,
    ) -> LocalContextCatalogEntry | None:
        """Resolve only an exact explicit mapping; aliases are never inferred."""

        provider = _text(provider, "provider")
        model = _text(model, "model")
        deployment_id = _text(deployment_id, "deployment_id")
        return self.entries.get((provider, model, deployment_id))

    def source_snapshot(self) -> dict[str, Any]:
        return {
            "adapter_version": "local_context_catalog_v1",
            "catalog_id": self.catalog_id,
            "source_version": self.source_version,
            "source_label": self.source_label,
            "content_sha256": self.content_sha256,
        }


def load_local_context_catalog(path: str | Path) -> LocalContextCatalog:
    """Load a strict, fixed local catalog.

    The catalog identity is the SHA-256 of the exact UTF-8 file bytes.  The
    catalog is intentionally read only at candidate construction/publish time;
    frozen runtime validation consumes the already embedded snapshot.
    """

    catalog_path = Path(path)
    try:
        raw_bytes = catalog_path.read_bytes()
        raw = json.loads(raw_bytes.decode("utf-8"), object_pairs_hook=_unique_json_object)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        raise ValueError("local context catalog could not be read") from None
    if not isinstance(raw, dict):
        raise ValueError("local context catalog must be an object")
    if set(raw) != {
        "schema_version",
        "catalog_id",
        "source_version",
        "source_label",
        "entries",
    }:
        raise ValueError("local context catalog has an invalid schema")
    if (
        isinstance(raw["schema_version"], bool)
        or not isinstance(raw["schema_version"], int)
        or raw["schema_version"] != LOCAL_CONTEXT_CATALOG_SCHEMA_VERSION
    ):
        raise ValueError("local context catalog schema_version must be 1")
    catalog_id = _text(raw["catalog_id"], "catalog_id")
    source_version = _text(raw["source_version"], "source_version")
    source_label = _text(raw["source_label"], "source_label")
    content_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    raw_entries = raw["entries"]
    if not isinstance(raw_entries, list):
        raise ValueError("local context catalog entries must be a list")
    entries: dict[tuple[str, str, str], LocalContextCatalogEntry] = {}
    for raw_entry in raw_entries:
        entry_canonical = json.dumps(
            raw_entry,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        entry_sha256 = hashlib.sha256(entry_canonical).hexdigest()
        catalog_ref = (
            f"{catalog_id}@{source_version}"
            f"#catalog-sha256:{content_sha256}"
            f";entry-sha256:{entry_sha256}"
        )
        entry = _parse_entry(raw_entry, catalog_ref=catalog_ref)
        key = (entry.provider, entry.model, entry.metadata.deployment_id)
        if key in entries:
            raise ValueError("local context catalog contains duplicate deployment mappings")
        entries[key] = entry
    return LocalContextCatalog(
        schema_version=LOCAL_CONTEXT_CATALOG_SCHEMA_VERSION,
        catalog_id=catalog_id,
        source_version=source_version,
        source_label=source_label,
        entries=entries,
        content_sha256=content_sha256,
    )


def apply_local_context_catalog(
    *,
    provider: str,
    model: str,
    deployment_id: str,
    catalog: LocalContextCatalog | None,
    declared_metadata: ContextMetadata | Mapping[str, Any] | None,
    declared_max_context_tokens: int | None,
) -> tuple[ContextMetadata | None, int | None, dict[str, Any] | None]:
    """Combine declared and fixed-catalog constraints by intersection.

    The returned metadata is the stricter explicit declaration.  When a
    catalog entry supplies ``T`` and the declaration does not, the catalog's
    explicit legacy total-window value is used.  A conflicting total window
    resolves to the smaller value and is recorded in the source trace.
    """

    provider = _text(provider, "provider")
    model = _text(model, "model")
    deployment_id = _text(deployment_id, "deployment_id")
    declared_t = _optional_positive_int(
        declared_max_context_tokens,
        "declared_max_context_tokens",
    )
    declared = context_metadata_from_mapping(declared_metadata)
    entry = (
        catalog.resolve(provider=provider, model=model, deployment_id=deployment_id)
        if catalog is not None
        else None
    )
    if entry is None and declared is None:
        return None, declared_t, None

    if entry is None:
        source = _declared_source(declared, declared_t)
        return _with_source_records(declared, source), declared_t, source
    if declared is None:
        source = _catalog_source(catalog, entry)
        return (
            _with_source_records(entry.metadata, source),
            entry.max_context_tokens,
            source,
        )

    catalog_t = entry.max_context_tokens
    total_context_tokens = _min_optional(catalog_t, declared_t)
    metadata = _intersect_metadata(declared, entry.metadata, total_context_tokens)
    declared_source = _declared_source(declared, declared_t)
    catalog_source = _catalog_source(catalog, entry)
    source = {
        "kind": "INTERSECTION",
        "sources": [declared_source, catalog_source],
        "conflicts": _intersection_conflicts(
            declared,
            declared_t,
            entry.metadata,
            catalog_t,
        ),
        "resolved_limits": {
            "catalog_max_context_tokens": catalog_t,
            "declared_max_context_tokens": declared_t,
            "max_context_tokens": total_context_tokens,
            "max_input_tokens": metadata.max_input_tokens,
            "max_output_tokens": metadata.max_output_tokens,
            "deployment_input_limit": metadata.deployment_input_limit,
            "input_output_semantics": (
                metadata.input_output_semantics.value
                if metadata.input_output_semantics is not None
                else None
            ),
            "output_reserve_semantics": (
                metadata.output_reserve_semantics.value
                if metadata.output_reserve_semantics is not None
                else None
            ),
            "resolved_model": metadata.resolved_model,
        },
    }
    return _with_source_records(metadata, source), total_context_tokens, source


def _parse_entry(value: Any, *, catalog_ref: str) -> LocalContextCatalogEntry:
    if not isinstance(value, Mapping) or set(value) != {
        "provider",
        "model",
        "deployment_id",
        "max_context_tokens",
        "metadata",
    }:
        raise ValueError("local context catalog entry has an invalid schema")
    provider = _text(value["provider"], "provider")
    model = _text(value["model"], "model")
    deployment_id = _text(value["deployment_id"], "deployment_id")
    max_context_tokens = _optional_positive_int(value["max_context_tokens"], "max_context_tokens")
    metadata_value = value["metadata"]
    if not isinstance(metadata_value, Mapping):
        raise ValueError("local context catalog metadata must be an object")
    metadata_dict = dict(metadata_value)
    if set(metadata_dict) - {
        "resolved_model",
        "source_version",
        "max_input_tokens",
        "max_output_tokens",
        "deployment_input_limit",
        "input_output_semantics",
        "output_reserve_semantics",
        "catalog_ref",
    }:
        raise ValueError("local context catalog metadata has an invalid schema")
    metadata_dict["schema_version"] = CONTEXT_METADATA_SCHEMA_VERSION
    metadata_dict["deployment_id"] = deployment_id
    metadata_dict["catalog_ref"] = catalog_ref
    metadata = context_metadata_from_mapping(metadata_dict)
    if metadata is None:
        raise ValueError("local context catalog metadata is required")
    if (
        metadata.max_output_tokens is not None
        and metadata.output_reserve_semantics is not OutputReserveSemantics.INCLUDES_REASONING
    ):
        raise ValueError("catalog output limits must declare INCLUDES_REASONING")
    if (
        metadata.input_output_semantics is InputOutputSemantics.TOTAL_WINDOW
        and max_context_tokens is None
    ):
        raise ValueError("TOTAL_WINDOW catalog entries require max_context_tokens")
    if max_context_tokens is None and (
        metadata.input_output_semantics is not InputOutputSemantics.INDEPENDENT_INPUT_OUTPUT
        or metadata.max_input_tokens is None
    ):
        raise ValueError(
            "catalog entries without a total window require explicit independent input capacity"
        )
    if (
        max_context_tokens is not None
        and metadata.max_input_tokens is not None
        and metadata.max_input_tokens > max_context_tokens
    ):
        raise ValueError("catalog max_input_tokens exceeds max_context_tokens")
    if (
        max_context_tokens is not None
        and metadata.max_output_tokens is not None
        and metadata.max_output_tokens > max_context_tokens
    ):
        raise ValueError("catalog max_output_tokens exceeds max_context_tokens")
    return LocalContextCatalogEntry(
        provider=provider,
        model=model,
        metadata=metadata,
        max_context_tokens=max_context_tokens,
    )


def _intersect_metadata(
    declared: ContextMetadata,
    catalog: ContextMetadata,
    total_context_tokens: int | None,
) -> ContextMetadata:
    """Keep every constraint at or below both explicit sources."""

    input_semantics = _intersect_semantics(
        declared.input_output_semantics,
        catalog.input_output_semantics,
    )
    has_output_limit = (
        declared.max_output_tokens is not None or catalog.max_output_tokens is not None
    )
    output_semantics = OutputReserveSemantics.INCLUDES_REASONING if has_output_limit else None
    max_input_tokens = _min_optional(
        declared.max_input_tokens,
        catalog.max_input_tokens,
    )
    if total_context_tokens is not None:
        max_input_tokens = _min_optional(max_input_tokens, total_context_tokens)
    return ContextMetadata(
        deployment_id=declared.deployment_id,
        resolved_model=_intersect_resolved_model(
            declared.resolved_model,
            catalog.resolved_model,
        ),
        source_version=f"{declared.source_version}+{catalog.source_version}",
        max_input_tokens=max_input_tokens,
        max_output_tokens=_min_optional(
            declared.max_output_tokens,
            catalog.max_output_tokens,
        ),
        deployment_input_limit=_min_optional(
            declared.deployment_input_limit,
            catalog.deployment_input_limit,
        ),
        input_output_semantics=input_semantics,
        output_reserve_semantics=output_semantics,
        catalog_ref=_intersect_catalog_ref(declared.catalog_ref, catalog.catalog_ref),
        source_records=declared.source_records,
    )


def _intersect_catalog_ref(
    declared_ref: str | None,
    catalog_ref: str | None,
) -> str | None:
    references = [value for value in (declared_ref, catalog_ref) if value is not None]
    if not references:
        return None
    if declared_ref == catalog_ref:
        return declared_ref
    return "|".join(dict.fromkeys(references))


def _with_source_records(
    metadata: ContextMetadata | None,
    source: Mapping[str, Any] | None,
) -> ContextMetadata | None:
    if metadata is None:
        return None
    if source is None:
        records = metadata.source_records
    else:
        candidates = (*(metadata.source_records or ()), _canonical_record(source))
        records_list: list[Mapping[str, Any]] = []
        for record in candidates:
            if record not in records_list:
                records_list.append(record)
        records = tuple(records_list)
    snapshot = context_metadata_to_mapping(metadata)
    if snapshot is None:
        raise ValueError("context metadata is required")
    snapshot["source_records"] = (
        [dict(record) for record in records] if records is not None else None
    )
    normalized = context_metadata_from_mapping(snapshot)
    if normalized is None:
        raise ValueError("context metadata is required")
    return normalized


def _canonical_record(source: Mapping[str, Any]) -> dict[str, Any]:
    canonical = json.loads(
        json.dumps(dict(source), allow_nan=False, separators=(",", ":"), sort_keys=True)
    )
    if not isinstance(canonical, dict):
        raise ValueError("context source record must be an object")
    return canonical


def _intersect_semantics(
    declared: InputOutputSemantics | None,
    catalog: InputOutputSemantics | None,
) -> InputOutputSemantics | None:
    if declared is None:
        return catalog
    if catalog is None:
        return declared
    if declared != catalog:
        raise ValueError("context metadata sources disagree on input/output semantics")
    return declared


def _declared_source(
    metadata: ContextMetadata | None,
    total_context_tokens: int | None,
) -> dict[str, Any] | None:
    if metadata is None:
        return None
    return {
        "kind": "DECLARED",
        "source_version": metadata.source_version,
        "deployment_id": metadata.deployment_id,
        "resolved_model": metadata.resolved_model,
        "catalog_ref": metadata.catalog_ref,
        "limits": _source_limits(metadata, total_context_tokens),
    }


def _catalog_source(
    catalog: LocalContextCatalog | None,
    entry: LocalContextCatalogEntry,
) -> dict[str, Any]:
    if catalog is None:
        raise ValueError("catalog source is unavailable")
    return {
        "kind": "LOCAL_CATALOG",
        "catalog_id": catalog.catalog_id,
        "source_version": catalog.source_version,
        "source_label": catalog.source_label,
        "content_sha256": catalog.content_sha256,
        "provider": entry.provider,
        "model": entry.model,
        "deployment_id": entry.metadata.deployment_id,
        "resolved_model": entry.metadata.resolved_model,
        "catalog_ref": entry.metadata.catalog_ref,
        "limits": _source_limits(entry.metadata, entry.max_context_tokens),
    }


def _source_limits(
    metadata: ContextMetadata,
    total_context_tokens: int | None,
) -> dict[str, Any]:
    return {
        "max_context_tokens": total_context_tokens,
        "max_input_tokens": metadata.max_input_tokens,
        "max_output_tokens": metadata.max_output_tokens,
        "deployment_input_limit": metadata.deployment_input_limit,
        "input_output_semantics": (
            metadata.input_output_semantics.value
            if metadata.input_output_semantics is not None
            else None
        ),
        "output_reserve_semantics": (
            metadata.output_reserve_semantics.value
            if metadata.output_reserve_semantics is not None
            else None
        ),
    }


def _intersection_conflicts(
    declared: ContextMetadata,
    declared_t: int | None,
    catalog: ContextMetadata,
    catalog_t: int | None,
) -> dict[str, Any]:
    conflicts: dict[str, Any] = {}
    for name, declared_value, catalog_value in (
        ("max_context_tokens", declared_t, catalog_t),
        ("max_input_tokens", declared.max_input_tokens, catalog.max_input_tokens),
        ("max_output_tokens", declared.max_output_tokens, catalog.max_output_tokens),
        (
            "deployment_input_limit",
            declared.deployment_input_limit,
            catalog.deployment_input_limit,
        ),
        (
            "input_output_semantics",
            declared.input_output_semantics,
            catalog.input_output_semantics,
        ),
        (
            "output_reserve_semantics",
            declared.output_reserve_semantics,
            catalog.output_reserve_semantics,
        ),
    ):
        if declared_value is None or catalog_value is None:
            continue
        declared_normalized = (
            declared_value.value if isinstance(declared_value, StrEnum) else declared_value
        )
        catalog_normalized = (
            catalog_value.value if isinstance(catalog_value, StrEnum) else catalog_value
        )
        if declared_normalized != catalog_normalized:
            conflicts[name] = {
                "declared": declared_normalized,
                "catalog": catalog_normalized,
            }
    return conflicts


def _intersect_resolved_model(declared: str, catalog: str) -> str:
    if declared == "UNKNOWN":
        return catalog
    if catalog == "UNKNOWN" or catalog == declared:
        return declared
    raise ValueError("context metadata sources disagree on resolved model")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{name} must be a non-empty trimmed string")
    return value


def _optional_positive_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer or None")
    return value


def _min_optional(*values: int | None) -> int | None:
    present = [value for value in values if value is not None]
    return min(present) if present else None


__all__ = [
    "LOCAL_CONTEXT_CATALOG_SCHEMA_VERSION",
    "LocalContextCatalog",
    "LocalContextCatalogEntry",
    "apply_local_context_catalog",
    "load_local_context_catalog",
]
