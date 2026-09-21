import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from packages.agent_runtime.models import RunStep
from packages.observability.runs import (
    _map_step,
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


# ---------------------------------------------------------------------------
# PREPARE and its thread_context block
# ---------------------------------------------------------------------------
#
# PREPARE is the only step that knows how much earlier conversation was
# replayed into a run. It used to be dropped from the timeline entirely, and
# its counters were nested one level down where the scalar-only metadata
# filter could never have let them through anyway. Both halves are asserted
# here because fixing either one alone still leaves the reader with nothing.


def _step(kind: str, status: str, metadata: dict) -> RunStep:
    return RunStep(
        id=uuid4(),
        workspace_id=uuid4(),
        agent_run_id=uuid4(),
        sequence_number=1,
        kind=kind,
        status=status,
        safe_metadata=metadata,
        created_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
    )


_THREAD_CONTEXT = {
    "thread_id": "1ac0ffee-0000-0000-0000-000000000000",
    "turns_available": 12,
    "turns_included": 10,
    "turns_dropped_by_window": 2,
    "max_turns": 10,
}


def test_prepare_reaches_the_timeline_with_its_history_counters() -> None:
    mapped = _map_step(
        _step("PREPARE", "SUCCEEDED", {"tool_count": 4, "thread_context": _THREAD_CONTEXT})
    )

    assert mapped is not None
    assert mapped.kind == "PREPARE"
    assert mapped.metadata["thread_context"] == _THREAD_CONTEXT
    assert mapped.metadata["tool_count"] == 4


def test_a_failed_prepare_still_reads_as_a_failure() -> None:
    mapped = _map_step(_step("PREPARE", "FAILED", {"error_code": "AGENT_PREPARE_FAILED"}))

    assert mapped is not None
    assert mapped.kind == "FAILURE"
    assert mapped.failure_code == "AGENT_PREPARE_FAILED"


def test_the_nested_allowance_is_a_whitelist_not_an_opening() -> None:
    # A producer that starts putting content next to the counters must not be
    # able to widen the projection by doing so.
    mapped = _map_step(
        _step(
            "PREPARE",
            "SUCCEEDED",
            {
                "thread_context": {**_THREAD_CONTEXT, "user_input": "my bank password is hunter2"},
                "some_other_block": {"turns_included": 10},
            },
        )
    )

    assert mapped is not None
    assert mapped.metadata["thread_context"] == _THREAD_CONTEXT
    assert "some_other_block" not in mapped.metadata
    assert "hunter2" not in json.dumps(mapped.metadata)


def test_an_empty_thread_context_is_omitted_rather_than_shown_as_a_blank() -> None:
    mapped = _map_step(_step("PREPARE", "SUCCEEDED", {"thread_context": {"user_input": "x"}}))

    assert mapped is not None
    assert "thread_context" not in mapped.metadata
