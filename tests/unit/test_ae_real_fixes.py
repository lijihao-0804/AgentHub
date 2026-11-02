"""Regression tests for defects found by the AE-Real-1 runtime acceptance run."""

from __future__ import annotations

from decimal import Decimal

from packages.agent_runtime.runtime_projection import _safe_step_metadata
from packages.evaluation.metrics_service import _decimal_or_none


def test_safe_step_metadata_keeps_argument_hashes_and_per_call_results() -> None:
    metadata = _safe_step_metadata(
        {
            "tool_identities": ["query_customer"],
            "arguments_hashes": ["a" * 64],
            "tool_results": [
                {
                    "tool_call_id": "call-1",
                    "tool_identity": "query_customer",
                    "status": "SUCCESS",
                    "error_code": None,
                }
            ],
            "raw_secret": "must-not-survive",
        }
    )

    assert metadata["arguments_hashes"] == ["a" * 64]
    assert metadata["tool_results"] == [
        {
            "tool_call_id": "call-1",
            "tool_identity": "query_customer",
            "status": "SUCCESS",
            "error_code": None,
        }
    ]
    assert "raw_secret" not in metadata


def test_decimal_persistence_quantizes_to_numeric_scale() -> None:
    value = _decimal_or_none(Decimal("3682.7604079307443"))

    assert value == Decimal("3682.760407930744")
    assert _decimal_or_none(None) is None
