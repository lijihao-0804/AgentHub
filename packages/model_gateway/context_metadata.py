"""Strict, provider-neutral context metadata for model deployments.

The legacy ``max_context_tokens`` field is one explicit total-window
declaration.  This module keeps deployment-level input/output semantics and
source identity separate from that legacy field.  It never infers limits from
model names and never fetches provider data.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

CONTEXT_METADATA_SCHEMA_VERSION = 1
UNKNOWN = "UNKNOWN"


class InputOutputSemantics(StrEnum):
    """How a deployment accounts for input and output capacity."""

    TOTAL_WINDOW = "TOTAL_WINDOW"
    INDEPENDENT_INPUT_OUTPUT = "INDEPENDENT_INPUT_OUTPUT"


class OutputReserveSemantics(StrEnum):
    """Where reasoning tokens are accounted for in the output allowance."""

    INCLUDES_REASONING = "INCLUDES_REASONING"


@dataclass(frozen=True, slots=True)
class ContextMetadata(Mapping[str, Any]):
    """Auditable context limits declared for one deployment.

    ``max_input_tokens`` and ``max_output_tokens`` are provider limits.
    ``deployment_input_limit`` is an optional stricter deployment limit.
    ``max_context_tokens`` on :class:`ModelCapabilities` remains the explicit
    legacy total-window declaration and is never derived from this object.
    """

    deployment_id: str
    resolved_model: str
    source_version: str
    schema_version: int = CONTEXT_METADATA_SCHEMA_VERSION
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    deployment_input_limit: int | None = None
    input_output_semantics: InputOutputSemantics | None = None
    output_reserve_semantics: OutputReserveSemantics | None = None
    catalog_ref: str | None = None
    source_records: tuple[Mapping[str, Any], ...] | None = None

    def __post_init__(self) -> None:
        for name in ("deployment_id", "resolved_model", "source_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
            if value != value.strip():
                raise ValueError(f"{name} must not contain surrounding whitespace")
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != CONTEXT_METADATA_SCHEMA_VERSION
        ):
            raise ValueError("schema_version must be 1")
        for name in (
            "max_input_tokens",
            "max_output_tokens",
            "deployment_input_limit",
        ):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
            ):
                raise ValueError(f"{name} must be a positive integer or None")
        for name, enum_type in (
            ("input_output_semantics", InputOutputSemantics),
            ("output_reserve_semantics", OutputReserveSemantics),
        ):
            value = getattr(self, name)
            if value is None:
                continue
            try:
                normalized = enum_type(value)
            except (TypeError, ValueError):
                raise ValueError(f"{name} is invalid") from None
            object.__setattr__(self, name, normalized)
        if self.catalog_ref is not None:
            if not isinstance(self.catalog_ref, str) or not self.catalog_ref.strip():
                raise ValueError("catalog_ref must be a non-empty string or None")
            if self.catalog_ref != self.catalog_ref.strip():
                raise ValueError("catalog_ref must not contain surrounding whitespace")
        if self.source_records is not None:
            records = tuple(self.source_records)
            if any(not isinstance(record, Mapping) for record in records):
                raise ValueError("source_records must contain mappings or be None")
            object.__setattr__(
                self,
                "source_records",
                tuple(_canonical_source_record(record) for record in records),
            )
        _validate_declared_consistency(self, total_context_tokens=None)

    @property
    def total_context_tokens(self) -> int | None:
        """Return UNKNOWN; the capability owns the explicit total-window T."""

        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "deployment_id": self.deployment_id,
            "resolved_model": self.resolved_model,
            "source_version": self.source_version,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "deployment_input_limit": self.deployment_input_limit,
            "input_output_semantics": (
                self.input_output_semantics.value
                if self.input_output_semantics is not None
                else None
            ),
            "output_reserve_semantics": (
                self.output_reserve_semantics.value
                if self.output_reserve_semantics is not None
                else None
            ),
            "catalog_ref": self.catalog_ref,
            "source_records": (
                [dict(record) for record in self.source_records]
                if self.source_records is not None
                else None
            ),
        }

    def __getitem__(self, key: str) -> Any:
        if key not in _ALLOWED_METADATA_KEYS:
            raise KeyError(key)
        return getattr(self, key)

    def __iter__(self):
        return iter(self.as_dict())

    def __len__(self) -> int:
        return len(_ALLOWED_METADATA_KEYS)


_ALLOWED_METADATA_KEYS = frozenset(
    {
        "schema_version",
        "deployment_id",
        "resolved_model",
        "source_version",
        "max_input_tokens",
        "max_output_tokens",
        "deployment_input_limit",
        "input_output_semantics",
        "output_reserve_semantics",
        "catalog_ref",
        "source_records",
    }
)
_REQUIRED_METADATA_KEYS = frozenset(
    {"schema_version", "deployment_id", "resolved_model", "source_version"}
)


def context_metadata_from_mapping(value: Any) -> ContextMetadata | None:
    """Parse metadata without coercing any field.

    ``None`` means the legacy absence of metadata.  Unknown keys, booleans in
    integer fields, missing schema/version/source identity, and incompatible
    input/output declarations all fail closed.  A ``TOTAL_WINDOW`` declaration
    is validated against ``ModelCapabilities.max_context_tokens`` rather than
    this metadata parser, because metadata does not duplicate the total window.
    """

    if value is None:
        return None
    if isinstance(value, ContextMetadata):
        return value
    if not isinstance(value, Mapping):
        raise ValueError("context_metadata must be a mapping or None")
    unknown = set(value) - _ALLOWED_METADATA_KEYS
    missing = _REQUIRED_METADATA_KEYS - set(value)
    if unknown or missing:
        raise ValueError("context_metadata has an invalid schema")
    schema_version = value["schema_version"]
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != CONTEXT_METADATA_SCHEMA_VERSION
    ):
        raise ValueError("context_metadata.schema_version must be 1")
    metadata = ContextMetadata(
        deployment_id=_required_text(value["deployment_id"], "deployment_id"),
        resolved_model=_resolved_model(value["resolved_model"]),
        source_version=_required_text(value["source_version"], "source_version"),
        schema_version=schema_version,
        max_input_tokens=_optional_positive_int(value.get("max_input_tokens"), "max_input_tokens"),
        max_output_tokens=_optional_positive_int(
            value.get("max_output_tokens"), "max_output_tokens"
        ),
        deployment_input_limit=_optional_positive_int(
            value.get("deployment_input_limit"), "deployment_input_limit"
        ),
        input_output_semantics=_semantics(
            value.get("input_output_semantics"),
            InputOutputSemantics,
            "input_output_semantics",
        ),
        output_reserve_semantics=_semantics(
            value.get("output_reserve_semantics"),
            OutputReserveSemantics,
            "output_reserve_semantics",
        ),
        catalog_ref=_optional_text(value.get("catalog_ref"), "catalog_ref"),
        source_records=_source_records(value.get("source_records")),
    )
    _validate_declared_consistency(metadata, total_context_tokens=None)
    return metadata


def context_metadata_to_mapping(
    metadata: ContextMetadata | Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Return a detached JSON-compatible snapshot."""

    parsed = context_metadata_from_mapping(metadata)
    return None if parsed is None else parsed.as_dict()


def validate_context_metadata(
    metadata: ContextMetadata | Mapping[str, Any] | None,
    *,
    max_context_tokens: int | None,
    output_allowance: int | None = None,
) -> ContextMetadata | None:
    """Validate metadata against the explicit legacy total window.

    A total-window deployment must have T from ``max_context_tokens``.  An
    independent-input deployment may use I alone, but if T exists, input must
    still fit that declared total window.
    """

    if max_context_tokens is not None and (
        isinstance(max_context_tokens, bool)
        or not isinstance(max_context_tokens, int)
        or max_context_tokens <= 0
    ):
        raise ValueError("max_context_tokens must be a positive integer or None")
    if output_allowance is not None and (
        isinstance(output_allowance, bool)
        or not isinstance(output_allowance, int)
        or output_allowance <= 0
    ):
        raise ValueError("output_allowance must be a positive integer or None")
    parsed = context_metadata_from_mapping(metadata)
    if parsed is None:
        return None
    _validate_declared_consistency(parsed, total_context_tokens=max_context_tokens)
    if (
        output_allowance is not None
        and parsed.max_output_tokens is not None
        and output_allowance > parsed.max_output_tokens
    ):
        raise ValueError("output reserve exceeds declared max_output_tokens")
    if (
        max_context_tokens is not None
        and parsed.max_input_tokens is not None
        and parsed.max_input_tokens > max_context_tokens
    ):
        raise ValueError("max_input_tokens exceeds the declared total context window")
    return parsed


def _validate_declared_consistency(
    metadata: ContextMetadata,
    *,
    total_context_tokens: int | None,
) -> None:
    if metadata.input_output_semantics is InputOutputSemantics.TOTAL_WINDOW:
        if total_context_tokens is not None and (
            metadata.max_input_tokens is not None
            and metadata.max_input_tokens > total_context_tokens
        ):
            raise ValueError("max_input_tokens exceeds the declared total context window")
        if total_context_tokens is not None and (
            metadata.max_output_tokens is not None
            and metadata.max_output_tokens > total_context_tokens
        ):
            raise ValueError("max_output_tokens exceeds the declared total context window")
    if (
        metadata.input_output_semantics is InputOutputSemantics.INDEPENDENT_INPUT_OUTPUT
        and metadata.max_input_tokens is None
    ):
        raise ValueError("INDEPENDENT_INPUT_OUTPUT requires max_input_tokens")
    if metadata.max_output_tokens is not None and metadata.output_reserve_semantics is None:
        raise ValueError(
            "max_output_tokens requires output_reserve_semantics to define reasoning accounting"
        )
    if total_context_tokens is not None:
        if (
            metadata.max_input_tokens is not None
            and metadata.max_input_tokens > total_context_tokens
        ):
            raise ValueError("max_input_tokens exceeds the declared total context window")
        if (
            metadata.max_output_tokens is not None
            and metadata.max_output_tokens > total_context_tokens
        ):
            raise ValueError("max_output_tokens exceeds the declared total context window")
    if (
        metadata.deployment_input_limit is not None
        and metadata.max_input_tokens is not None
        and metadata.deployment_input_limit > metadata.max_input_tokens
    ):
        raise ValueError("deployment_input_limit cannot exceed max_input_tokens")


def _resolved_model(value: Any) -> str:
    if value == UNKNOWN:
        return UNKNOWN
    return _required_text(value, "resolved_model")


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{name} must be a non-empty trimmed string")
    return value


def _optional_text(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, name)


def _optional_positive_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer or None")
    return value


def _semantics(
    value: Any,
    enum_type: type[StrEnum],
    name: str,
) -> Any:
    if value is None:
        return None
    try:
        return enum_type(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} is invalid") from None


def _source_records(value: Any) -> tuple[dict[str, Any], ...] | None:
    if value is None:
        return None
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ValueError("source_records must be a list of mappings or None")
    return tuple(_canonical_source_record(record) for record in value)


def _canonical_source_record(record: Mapping[str, Any]) -> dict[str, Any]:
    try:
        encoded = json.dumps(
            dict(record),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        normalized = json.loads(encoded, object_pairs_hook=_unique_json_object)
    except (TypeError, ValueError):
        raise ValueError("source_records must contain finite JSON objects") from None
    if not isinstance(normalized, dict) or not normalized:
        raise ValueError("source_records entries must be non-empty JSON objects")
    return normalized


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate source record key")
        result[key] = value
    return result


__all__ = [
    "CONTEXT_METADATA_SCHEMA_VERSION",
    "ContextMetadata",
    "InputOutputSemantics",
    "OutputReserveSemantics",
    "UNKNOWN",
    "context_metadata_from_mapping",
    "context_metadata_to_mapping",
    "validate_context_metadata",
]
