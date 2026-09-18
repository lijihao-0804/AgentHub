from __future__ import annotations

import json
import logging
from decimal import Decimal
from uuid import uuid4

import pytest

from packages.agent_runtime.runtime import _aggregate_usage
from packages.core.config.settings import Settings
from packages.core.errors.exceptions import AgentHubError
from packages.core.logging.json_logging import JsonFormatter
from packages.model_gateway.contracts import CostEstimate, ModelResponse, ModelUsage
from packages.model_gateway.credentials import CredentialEncryptionError, ProviderCredentialCipher
from scripts.migrate_provider_credentials import migrate


def test_provider_credential_cipher_is_versioned_and_round_trips() -> None:
    settings = Settings(testing=True, credential_master_key="h2-test-master-key")
    cipher = ProviderCredentialCipher.from_settings(settings)

    ciphertext = cipher.encrypt("provider-secret")

    assert ciphertext.startswith("v1:")
    assert ciphertext != "provider-secret"
    assert cipher.decrypt(ciphertext) == "provider-secret"

    wrong = ProviderCredentialCipher.from_settings(
        Settings(testing=True, credential_master_key="wrong-master-key")
    )
    with pytest.raises(CredentialEncryptionError) as error:
        wrong.decrypt(ciphertext)
    assert "provider-secret" not in str(error.value)


def test_provider_credential_cipher_requires_key_in_production() -> None:
    with pytest.raises(CredentialEncryptionError):
        ProviderCredentialCipher.from_settings(
            Settings(environment="production", auth_jwt_secret="x" * 32)
        )


@pytest.mark.asyncio
async def test_credential_migration_requires_explicit_master_key(monkeypatch) -> None:
    monkeypatch.delenv("AGENTHUB_CREDENTIAL_MASTER_KEY", raising=False)

    assert await migrate(verify_only=True) == 2


def test_usage_aggregation_is_numeric_and_currency_safe() -> None:
    response = ModelResponse(
        content="ok",
        provider="fake",
        model="fake-model",
        usage=ModelUsage(input_tokens=3, output_tokens=2, total_tokens=5, cached_tokens=1),
        cost_estimate=CostEstimate(Decimal("0.12"), "usd", is_estimate=True),
    )
    record = {
        "usage": {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "total_tokens": response.usage.total_tokens,
            "cached_tokens": response.usage.cached_tokens,
        },
        "cost": {"amount": Decimal("0.12"), "currency": "USD", "is_estimate": True},
    }

    aggregate = _aggregate_usage([record, record])

    assert aggregate["total_input_tokens"] == 6
    assert aggregate["total_output_tokens"] == 4
    assert aggregate["total_tokens"] == 10
    assert aggregate["total_cached_tokens"] == 2
    assert aggregate["total_cost_amount"] == Decimal("0.24")
    assert aggregate["cost_currency"] == "USD"
    assert aggregate["cost_is_estimate"] is True
    assert _aggregate_usage([])["total_tokens"] is None


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ([False, False], False),
        ([True, True], True),
        ([False, True], True),
    ],
)
def test_usage_aggregation_marks_any_estimated_cost(
    flags: list[bool], expected: bool
) -> None:
    records = [
        {"cost": {"amount": "0.10", "currency": "USD", "is_estimate": flag}}
        for flag in flags
    ]

    assert _aggregate_usage(records)["cost_is_estimate"] is expected


def test_usage_aggregation_does_not_sum_mixed_currencies() -> None:
    records = [
        {"cost": {"amount": "0.10", "currency": "USD", "is_estimate": False}},
        {"cost": {"amount": "0.20", "currency": "EUR", "is_estimate": True}},
    ]

    aggregate = _aggregate_usage(records)

    assert aggregate["total_cost_amount"] is None
    assert aggregate["cost_currency"] is None
    assert aggregate["cost_is_estimate"] is None


def test_json_formatter_redacts_nested_sensitive_keys_only() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "hello", (), None)
    record.details = {
        "password": "hidden",
        "nested": {"api_key": "hidden", "ordinary": "visible"},
        "items": [{"authorization": "hidden"}],
    }

    payload = json.loads(formatter.format(record))

    assert payload["details"]["password"] == "***REDACTED***"
    assert payload["details"]["nested"]["api_key"] == "***REDACTED***"
    assert payload["details"]["nested"]["ordinary"] == "visible"
    assert payload["details"]["items"][0]["authorization"] == "***REDACTED***"


def test_missing_and_unknown_spec_schema_versions_are_safe_errors() -> None:
    for value in ({}, {"spec_schema_version": 999}):
        from packages.agent_runtime.frozen import parse_frozen_agent_spec

        with pytest.raises(AgentHubError) as error:
            parse_frozen_agent_spec(value, workspace_id=uuid4())
        assert error.value.code == "AGENT_VERSION_MODEL_BINDING_INVALID"
