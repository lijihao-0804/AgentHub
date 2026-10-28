"""Safety checks for summary job budgets before any provider dispatch."""

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from packages.agent_runtime.summary_service import (
    NewSummaryJob,
    NewSummarySource,
    SummaryServiceError,
    _decode_raw_response,
    _encode_raw_response,
    _reservation_cost,
    _validate_new_job,
)


def _job() -> NewSummaryJob:
    identity = "a" * 64
    source = NewSummarySource(
        source_id="source-1",
        turn_id=uuid4(),
        run_id=uuid4(),
        sequence=1,
        original_ref="thread:source-1",
        source_identity_hash=identity,
        revision_sha256=identity,
        user_input_sha256=identity,
        final_output_sha256=identity,
        summary_eligibility="ELIGIBLE",
        eligibility_reason="REVIEWED",
    )
    return NewSummaryJob(
        workspace_id=uuid4(),
        thread_id=uuid4(),
        trigger_run_id=uuid4(),
        source_watermark=2,
        source_scope={},
        policy_spec={},
        policy_version="v1",
        prompt_version="v1",
        budget_version="v1",
        model_spec={},
        tokenizer_id="test",
        tokenizer_version="v1",
        target_budget_tokens=100,
        permission_snapshot_hash=identity,
        created_by=uuid4(),
        sources=(source,),
        checkpoint_id="checkpoint-1",
        checkpoint_ns="",
        model_round=1,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_watermark", 0),
        ("source_watermark", True),
        ("target_budget_tokens", 0),
        ("target_budget_tokens", True),
    ],
)
def test_invalid_job_bounds_fail_before_database_access(field, value):
    with pytest.raises(ValueError, match="positive integer"):
        _validate_new_job(replace(_job(), **{field: value}))


def test_boolean_source_sequence_cannot_masquerade_as_a_valid_integer():
    job = _job()
    with pytest.raises(SummaryServiceError, match="before the frozen watermark"):
        _validate_new_job(replace(job, sources=(replace(job.sources[0], sequence=True),)))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("checkpoint_id", None),
        ("checkpoint_id", ""),
        ("checkpoint_ns", None),
        ("checkpoint_ns", "x" * 1025),
    ],
)
def test_summary_job_requires_bounded_exact_checkpoint_selectors(field, value):
    with pytest.raises(ValueError, match="checkpoint"):
        _validate_new_job(replace(_job(), **{field: value}))


def test_fractional_cost_reservation_rounds_up_to_database_precision():
    pricing = SimpleNamespace(
        input_price_per_1m=Decimal("0.001"),
        output_price_per_1m=Decimal("0"),
        currency="CNY",
    )
    assert _reservation_cost(pricing, input_tokens=1, output_tokens=0) == Decimal("0.00000001")


@pytest.mark.parametrize("raw", ["invalid\x00json", b"\xff", '{"x":1,"x":2}'])
def test_response_storage_preserves_original_bytes_without_parsing(raw):
    envelope, identity = _encode_raw_response(raw)
    stored = SimpleNamespace(raw_response=envelope, response_sha256=identity)
    expected = raw if isinstance(raw, bytes) else raw.encode("utf-8")
    assert _decode_raw_response(stored) == expected
