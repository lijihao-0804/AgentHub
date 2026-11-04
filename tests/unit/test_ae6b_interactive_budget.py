from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.errors.exceptions import AgentHubError
from packages.evaluation.interactive_driver import InteractiveScenarioDriver
from packages.evaluation.interactive_lifecycle import CaseExecutionIdentity
from packages.evaluation.interactive_observation import (
    observation_envelope,
    observation_from_envelope,
    with_cleanup,
)
from packages.evaluation.runner import CaseExecutionObservation


def _case():
    workspace_id, run_id, variant_id, item_id = (uuid4() for _ in range(4))
    identity = CaseExecutionIdentity(
        workspace_id=workspace_id,
        run_id=run_id,
        case_id=uuid4(),
        variant_id=variant_id,
        item_id=item_id,
        repetition_index=0,
        owner="budget-test",
        generation=1,
    )
    return (
        identity,
        SimpleNamespace(id=run_id, workspace_id=workspace_id),
        SimpleNamespace(id=variant_id),
        SimpleNamespace(
            id=item_id,
            category="INTERACTIVE",
            expected={"assertions": {"ticket_count": 0}},
            input={
                "task": "help",
                "environment_ref": "support_customer",
                "simulator": {"private_facts": {}, "triggers": {}, "max_turns": 3},
            },
        ),
    )


def _driver(identity):
    async def context_factory(run):
        return SimpleNamespace(
            workspace_id=str(identity.workspace_id), permissions={"evaluation_run"}
        )

    def environment_factory(*args):
        raise AssertionError("Preflight rejection must not allocate an environment")

    return InteractiveScenarioDriver(
        SimpleNamespace(),
        context_factory=context_factory,
        environment_factory=environment_factory,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 0.2, "1.00"])
async def test_finite_cost_without_full_reservation_rejects_before_resource_or_dispatch(limit):
    identity, run, variant, item = _case()
    item.input["max_cost_cny"] = limit
    with pytest.raises(AgentHubError) as rejected:
        await _driver(identity).prepare(run=run, variant=variant, item=item, case_identity=identity)
    assert rejected.value.code == "INTERACTIVE_COST_RESERVATION_UNAVAILABLE"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("max_turns", 0),
        ("max_turns", True),
        ("max_turns", 11),
        ("max_tokens", 0),
        ("max_tokens", True),
        ("max_tokens", -1),
        ("max_cost_cny", "NaN"),
        ("max_cost_cny", "Infinity"),
        ("max_cost_cny", -1),
        ("environment_ref", "https://unregistered.example/tool"),
        ("seed", True),
    ],
)
async def test_invalid_execution_limits_fail_before_environment_allocation(field, value):
    identity, run, variant, item = _case()
    item.input[field] = value
    with pytest.raises(ValueError):
        await _driver(identity).prepare(run=run, variant=variant, item=item, case_identity=identity)


def test_retained_observation_roundtrip_preserves_unknown_usage_and_decimal_cost():
    result = CaseExecutionObservation(
        observation={"category": "INTERACTIVE", "turns": []},
        agent_run_id=uuid4(),
        input_tokens=None,
        output_tokens=None,
        cached_tokens=None,
        total_tokens=None,
        cost_amount=Decimal("0.12345678"),
        cost_currency="CNY",
        aggregate_billing_complete=False,
    )
    envelope = observation_envelope(result)
    restored = observation_from_envelope(envelope)
    assert restored == result
    assert envelope["input_tokens"] is None
    assert envelope["cost_amount"] == "0.12345678"
    unknown = observation_envelope(CaseExecutionObservation(observation={}, cost_amount=None))
    assert unknown["cost_amount"] is None


def test_cleanup_projection_keeps_retained_execution_hash_and_unknown_turn_evidence():
    envelope = observation_envelope(
        CaseExecutionObservation(
            observation={
                "category": "INTERACTIVE",
                "stop_reason": "agent_needs_attention",
                "turns": [{"status": "NEEDS_ATTENTION", "failure_code": "ACTION_OUTCOME_UNKNOWN"}],
                "environment": {"assertion": {"passed": False}},
            }
        )
    )
    before = canonical_json_hash(envelope)
    projected = with_cleanup(envelope, "CLEANUP_BLOCKED", "UNKNOWN_OUTCOME")
    assert canonical_json_hash(envelope) == before
    assert projected["observation"]["turns"] == envelope["observation"]["turns"]
    assert projected["observation"]["execution_complete"] is False
    assert projected["observation"]["stop_reason"] == "agent_needs_attention"
    assert projected["observation"]["environment_lifecycle_status"] == "CLEANUP_BLOCKED"
