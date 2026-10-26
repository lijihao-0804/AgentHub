from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from packages.agent_runtime.frozen import parse_frozen_agent_spec
from packages.evaluation.experiments import ExperimentService
from packages.evaluation.release_gate import EvaluationReleaseGateService
from packages.evaluation.runner import CaseExecutionObservation, ExperimentRunner
from packages.evaluation.validation import dataset_content_hash

WORKSPACE_ID = UUID("00000000-0000-0000-0000-000000000001")


def _profile(model: str) -> dict:
    return {
        "profile_id": str(uuid4()),
        "credential_ref": str(uuid4()),
        "provider": "offline-fake",
        "model": model,
        "temperature": 0,
        "max_tokens": 128,
        "timeout_seconds": 5,
        "capabilities": {"tool_calling": True, "max_context_tokens": 8192},
    }


def _resolved_spec() -> dict:
    fallback = _profile("fallback-model")
    primary = {
        **_profile("primary-model"),
        "retry_policy": {"max_attempts": 1},
        "fallback_chain": [fallback["profile_id"]],
        "fallback_profiles": [fallback],
    }
    return {
        "spec_schema_version": 1,
        "model": primary,
        "prompt": {"system_prompt": "Offline probe", "prompt_version": 1},
        "retrieval": {"knowledge_binding_mode": "PINNED", "knowledge_snapshots": []},
        "tools": [],
        "runtime": {},
    }


def _item(expected_chunk: str = "chunk-original") -> dict:
    return {
        "case_key": "case-1",
        "split": "DEV",
        "category": "RETRIEVAL",
        "input": {"query": "agent runtime"},
        "expected": {"relevant_chunk_ids": [expected_chunk]},
        "tags": ["probe"],
        "source_provenance": {"source_kind": "probe", "source_id": "case-1"},
        "ordinal": 0,
    }


def probe_safety_optional_unavailable() -> dict:
    service = EvaluationReleaseGateService()
    policy = service._validate_policy(
        {
            "rules": [
                {
                    "metric": "task_success",
                    "rule": "NO_REGRESSION",
                    "safety": True,
                    "required": False,
                }
            ]
        }
    )
    comparison = SimpleNamespace(
        status="COMPLETE",
        metrics={
            "task_success": {
                "status": "COMPLETE",
                "baseline": {"status": "AVAILABLE", "value": 0.9},
                "candidate": {"status": "NOT_AVAILABLE", "reason": "probe_missing"},
            }
        },
    )
    status, results, reasons = service._evaluate(comparison, policy)
    return {
        "normalized_policy": policy,
        "decision": status,
        "rule_results": results,
        "reasons": reasons,
        "reproduced": status == "PASS" and results[0]["status"] == "INCONCLUSIVE",
    }


def probe_dataset_cached_hash() -> dict:
    original = _item()
    original_hash = dataset_content_hash([original])
    mutated = deepcopy(original)
    mutated["expected"]["relevant_chunk_ids"] = ["chunk-mutated"]
    recomputed_hash = dataset_content_hash([mutated])
    return {
        "stored_hash_after_item_mutation": original_hash,
        "recomputed_items_hash": recomputed_hash,
        "hashes_differ": original_hash != recomputed_hash,
        "observation": (
            "finalize_experiment and runner compare only the cached version hash; "
            "they do not recompute dataset_content_hash from item rows"
        ),
        "database_probe_executed": False,
    }


class _FakeResult:
    rowcount = 1


class _FakeSession:
    def __init__(self, variant: object, pricing: object) -> None:
        self._scalar_results = [uuid4(), variant, pricing]
        self.executed_update = None

    async def scalar(self, _statement: object):
        return self._scalar_results.pop(0)

    async def execute(self, statement: object):
        self.executed_update = statement
        return _FakeResult()

    async def commit(self) -> None:
        return None


async def probe_fallback_primary_pricing() -> dict:
    spec = _resolved_spec()
    parsed = parse_frozen_agent_spec(spec, workspace_id=WORKSPACE_ID)
    pricing = SimpleNamespace(
        provider="offline-fake",
        model="primary-model",
        currency="USD",
        input_price_per_1m=Decimal("10"),
        output_price_per_1m=Decimal("30"),
        cached_input_price_per_1m=Decimal("1"),
    )

    ExperimentService._validate_pricing_model(spec, pricing)

    variant = SimpleNamespace(workspace_id=WORKSPACE_ID, pricing_snapshot_id=uuid4())
    session = _FakeSession(variant, pricing)
    observation = CaseExecutionObservation(
        observation={"status": "SUCCEEDED"},
        latency_ms=1,
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        total_tokens=2_000_000,
        cached_tokens=0,
    )
    completed = await ExperimentRunner(None).complete_case(
        session,
        case_id=uuid4(),
        observation=observation,
    )
    values = session.executed_update.compile().params
    return {
        "fallback_profiles": len(parsed.model_plan.fallbacks),
        "fallback_provider_model": (
            parsed.model_plan.fallbacks[0].provider,
            parsed.model_plan.fallbacks[0].model,
        ),
        "primary_only_pricing_accepted": True,
        "case_update_succeeded": completed,
        "aggregated_tokens": [observation.input_tokens, observation.output_tokens],
        "cost_amount_from_primary_snapshot": str(values["cost_amount"]),
        "cost_currency_from_primary_snapshot": values["cost_currency"],
        "expected_usd_primary_math": "40.00000000",
        "observation_has_attempt_models": False,
    }


async def main() -> None:
    output = {
        "safety_optional_unavailable": probe_safety_optional_unavailable(),
        "dataset_cached_hash": probe_dataset_cached_hash(),
        "fallback_primary_pricing": await probe_fallback_primary_pricing(),
    }
    print(json.dumps(output, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    asyncio.run(main())


def test_optional_safety_unknown_still_passes():
    result = probe_safety_optional_unavailable()
    assert result["decision"] == "PASS"
    assert result["rule_results"][0]["status"] == "INCONCLUSIVE"


@pytest.mark.asyncio
async def test_fallback_chain_accepted_with_primary_only_pricing():
    result = await probe_fallback_primary_pricing()
    assert result["fallback_profiles"] == 1
    assert result["primary_only_pricing_accepted"] is True
    assert result["cost_amount_from_primary_snapshot"] == "40.00000000"
