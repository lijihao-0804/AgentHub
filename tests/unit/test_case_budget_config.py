import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from packages.evaluation.case_budget_config import load_case_budget_registry
from packages.evaluation.case_budget_pricing import CaseBudgetPricingError
from packages.model_gateway.contracts import ModelMessage, ModelRequest, StructuredOutputSchema


def contract(**changes):
    return {
        "workspace_id": "10000000-0000-4000-8000-000000000001",
        "pricing_snapshot_id": "20000000-0000-4000-8000-000000000002",
        "pricing_snapshot_hash": "a" * 64,
        "provider": "openai-compatible",
        "model": "synthetic-text-v1",
        "base_url": "https://billing-fixture.invalid/v1",
        "currency": "CNY",
        "input_price_per_1m": "2.0",
        "output_price_per_1m": "8.0",
        "cached_input_price_per_1m": None,
        "review_reference": "Synthetic tariff and framing contract for tests",
        "valid_until": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        "billing_terms": "text_tokens_only_including_failed_requests_v1",
        "request_mode": "provider_default",
        "max_billable_input_tokens": 14,
        "max_billable_output_tokens": 8,
        "input_bound": "utf8_bytes_plus_attested_framing_v1",
        "tokens_per_utf8_byte": 1,
        "tokens_per_message": 2,
        "tokens_per_request": 3,
        **changes,
    }


def load(tmp_path, *entries):
    path = tmp_path / "billing.json"
    path.write_text(json.dumps({"schema_version": 1, "contracts": entries}), encoding="utf-8")
    return load_case_budget_registry(path)


def test_binding_and_actual_request_envelope(tmp_path):
    registry = load(tmp_path, contract())
    proof = registry.get(registry.attestation_ids[0])
    assert proof is not None
    binding = dict(
        workspace_id=proof.workspace_id,
        pricing_snapshot_id=proof.pricing_snapshot_id,
        pricing_snapshot_hash=proof.pricing_snapshot_hash,
        route=proof.route,
    )
    assert registry.for_binding(**binding) == proof
    assert registry.for_binding(**(binding | {"workspace_id": UUID(int=0)})) is None
    validator = registry.get_validator(proof)
    assert validator is not None
    messages = (ModelMessage("assistant", "a"), ModelMessage("user", "中abc"))
    assert validator.validate(ModelRequest(messages), max_billable_input_tokens=14)
    assert not validator.validate(
        ModelRequest(messages + (ModelMessage("user", ""),)), max_billable_input_tokens=14
    )
    assert not validator.validate(ModelRequest(messages), max_billable_input_tokens=15)
    assert not validator.validate(
        ModelRequest(messages, response_schema=StructuredOutputSchema("schema", {})),
        max_billable_input_tokens=14,
    )


def test_missing_expired_or_ambiguous_proof_never_matches(tmp_path):
    assert not load_case_budget_registry(None).attestation_ids
    first = contract()
    expired = first | {"valid_until": "2001-01-01T00:00:00Z"}
    registry = load(tmp_path, expired)
    assert registry.get(registry.attestation_ids[0]) is None
    registry = load(tmp_path, first, first | {"tokens_per_message": 4})
    proof = registry.get(registry.attestation_ids[0])
    assert proof is not None
    assert (
        registry.for_binding(
            workspace_id=proof.workspace_id,
            route=proof.route,
            pricing_snapshot_id=proof.pricing_snapshot_id,
            pricing_snapshot_hash=proof.pricing_snapshot_hash,
        )
        is None
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"tokens_per_utf8_byte": False},
        {"max_billable_input_tokens": -1},
        {"currency": "USD"},
        {"api_key": "secret-sentinel"},
        {"input_price_per_1m": 1.2},
        {"billing_terms": "successful_responses_only"},
        {"valid_until": "2099-01-01"},
        {"base_url": "http://example.invalid/v1"},
    ],
)
def test_malformed_contract_has_safe_error(tmp_path, bad):
    with pytest.raises(CaseBudgetPricingError, match="deployment billing contracts") as error:
        load(tmp_path, contract(**bad))
    assert error.value.code == "CASE_BUDGET_CONFIG_INVALID"
    assert "secret-sentinel" not in str(error.value)


def test_repeated_json_keys_are_rejected(tmp_path):
    path = tmp_path / "billing.json"
    path.write_text('{"schema_version":1,"contracts":[],"contracts":[]}', encoding="utf-8")
    with pytest.raises(CaseBudgetPricingError):
        load_case_budget_registry(path)
