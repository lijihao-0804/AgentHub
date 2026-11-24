"""Sanitized, actionable JSON-Schema validation hints for tool calls.

Never echoes raw argument values (preventing accidental secret/PII reflection),
while telling the model the exact offending field name and schema constraint so
it can self-correct in one turn instead of hitting repeat-call circuit breakers.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jsonschema import SchemaError, ValidationError

_DEFAULT_MESSAGE = "The tool arguments are invalid."


def _field_path(error: ValidationError) -> str:
    parts = [str(segment) for segment in error.absolute_path]
    return ".".join(parts) if parts else "<root>"


def format_validation_hint(
    error: ValidationError | SchemaError,
    schema: Mapping[str, Any] | None = None,
) -> str:
    """Produce a safe, value-free, actionable validation hint for the model."""
    if isinstance(error, SchemaError):
        return _DEFAULT_MESSAGE

    validator = error.validator
    field = _field_path(error)

    if validator == "required":
        missing = None
        if isinstance(error.validator_value, list) and isinstance(error.instance, Mapping):
            missing_fields = sorted(
                str(key) for key in error.validator_value if key not in error.instance
            )
            if missing_fields:
                missing = ", ".join(f"'{name}'" for name in missing_fields[:5])
        if missing:
            return f"The tool arguments are invalid: missing required field(s) {missing}."
        return "The tool arguments are invalid: a required field is missing."

    if validator == "additionalProperties":
        allowed = ""
        props = (
            error.schema.get("properties")
            if isinstance(error.schema, Mapping)
            else (schema.get("properties") if isinstance(schema, Mapping) else None)
        )
        if isinstance(props, Mapping):
            allowed_keys = sorted(str(k) for k in props)
            allowed = " Allowed fields: " + ", ".join(f"'{k}'" for k in allowed_keys[:10]) + "."
        extra = ""
        if isinstance(error.instance, Mapping) and isinstance(props, Mapping):
            unknown = sorted(str(k) for k in error.instance if k not in props)
            if unknown:
                extra = " Unexpected field(s): " + ", ".join(f"'{k}'" for k in unknown[:5]) + "."
        return f"The tool arguments are invalid:{extra}{allowed}".strip()

    if validator == "type":
        expected = error.validator_value
        return f"The tool arguments are invalid: field '{field}' must be of type '{expected}'."

    if validator == "enum" and isinstance(error.validator_value, list):
        choices = [repr(v) for v in error.validator_value[:10]]
        return (
            f"The tool arguments are invalid: field '{field}' must be one of "
            f"[{', '.join(choices)}]."
        )

    if validator in {"minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems"}:
        return (
            f"The tool arguments are invalid: field '{field}' violates "
            f"{validator}={error.validator_value}."
        )

    return f"The tool arguments are invalid: field '{field}' failed '{validator}' constraint."


__all__ = ["format_validation_hint"]
