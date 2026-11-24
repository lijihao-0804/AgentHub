"""Unit tests for sanitized actionable tool validation error hints."""

from __future__ import annotations

from jsonschema import Draft202012Validator, ValidationError

from packages.tools.error_hints import format_validation_hint

_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["query", "priority"],
    "properties": {
        "query": {"type": "string", "minLength": 2},
        "priority": {"type": "string", "enum": ["P0", "P1", "P2"]},
        "limit": {"type": "integer", "minimum": 1, "maximum": 10},
    },
}


def _validate_and_hint(args: dict) -> str:
    validator = Draft202012Validator(_SCHEMA)
    try:
        validator.validate(args)
    except ValidationError as exc:
        return format_validation_hint(exc, _SCHEMA)
    raise AssertionError("expected validation error")


def test_actionable_hints_cover_required_type_enum_bounds_and_extra_fields() -> None:
    h_req = _validate_and_hint({"query": "ok"})
    assert "missing required field(s) 'priority'" in h_req

    h_extra = _validate_and_hint({"query": "ok", "priority": "P1", "secret_key": "sk-live-12345"})
    assert "Unexpected field(s): 'secret_key'" in h_extra
    assert "Allowed fields:" in h_extra
    # Never leaks raw secret values!
    assert "sk-live-12345" not in h_extra

    h_type = _validate_and_hint({"query": "ok", "priority": "P1", "limit": "five"})
    assert "field 'limit' must be of type 'integer'" in h_type
    assert "five" not in h_type

    h_enum = _validate_and_hint({"query": "ok", "priority": "P9"})
    assert "field 'priority' must be one of ['P0', 'P1', 'P2']" in h_enum
    assert "P9" not in h_enum

    h_bound = _validate_and_hint({"query": "ok", "priority": "P1", "limit": 99})
    assert "field 'limit' violates maximum=10" in h_bound
    assert "99" not in h_bound
