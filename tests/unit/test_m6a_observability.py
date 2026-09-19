from datetime import UTC, datetime
from uuid import UUID, uuid4

from packages.observability.runs import (
    classify_failure,
    decode_run_cursor,
    encode_run_cursor,
)


def test_m6a_failure_categories_preserve_presentation_boundaries() -> None:
    assert classify_failure("MODEL_PROVIDER_UNAVAILABLE") == "MODEL"
    assert classify_failure("QDRANT_UNAVAILABLE") == "KNOWLEDGE"
    assert classify_failure("APPROVAL_DECISION_CONFLICT") == "APPROVAL"
    assert classify_failure("TOOL_EXECUTION_FAILED") == "TOOL"
    assert classify_failure("UNKNOWN_OUTCOME") == "ACTION"
    assert classify_failure("FORBIDDEN") == "AUTH/TENANT"
    assert classify_failure("SOME_FUTURE_CODE") == "UNKNOWN"


def test_m6a_cursor_round_trip_is_opaque_and_tenant_neutral() -> None:
    created_at = datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=UTC)
    run_id = uuid4()

    encoded = encode_run_cursor(created_at, run_id)
    decoded = decode_run_cursor(encoded)

    assert encoded
    assert decoded.created_at == created_at
    assert decoded.run_id == UUID(str(run_id))
