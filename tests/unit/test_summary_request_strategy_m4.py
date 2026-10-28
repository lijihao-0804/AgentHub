from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from packages.agent_runtime.frozen import (
    SUMMARY_NON_THINKING_COUNTER_ID,
    SUMMARY_NON_THINKING_COUNTER_REVISION,
    SUMMARY_NON_THINKING_COUNTER_RUNTIME_VERSION,
    SUMMARY_NON_THINKING_MODE,
    SUMMARY_NON_THINKING_MODEL,
    SUMMARY_NON_THINKING_PROVIDER,
    SUMMARY_NON_THINKING_ROUTE_ALLOWLIST,
    SUMMARY_NON_THINKING_STRATEGY_VERSION,
    SUMMARY_NON_THINKING_TOKENIZER_SHA256,
    _parse_summary_policy,
)
from packages.agent_runtime.publish import _normalize_summary_policy_request
from packages.agent_runtime.summary_service import (
    SummaryJobKey,
    SummaryJobService,
    SummaryServiceError,
    _summary_request_mode,
)
from packages.core.errors.exceptions import AgentHubError
from tests.unit.test_context_policy_m2_runtime import _freeze, _profile


def _base_policy() -> dict[str, object]:
    return {
        "enabled": True,
        "policy_version": "thread_summary_policy_v1",
        "prompt_version": "summary_candidate_zh_v1",
        "validator_version": "summary_mechanical_v1",
        "tokenizer_id": "approx_language_v1",
        "tokenizer_version": 1,
        "soft_trigger_percent": 70,
        "hard_trigger_percent": 85,
        "target_percent": 55,
    }


def _candidate_identity() -> dict[str, object]:
    return {
        "request_strategy_version": SUMMARY_NON_THINKING_STRATEGY_VERSION,
        "request_mode": SUMMARY_NON_THINKING_MODE,
        "request_route_allowlist": SUMMARY_NON_THINKING_ROUTE_ALLOWLIST,
        "request_counter_id": SUMMARY_NON_THINKING_COUNTER_ID,
        "request_counter_revision": SUMMARY_NON_THINKING_COUNTER_REVISION,
        "request_counter_runtime_version": SUMMARY_NON_THINKING_COUNTER_RUNTIME_VERSION,
        "request_counter_sha256": SUMMARY_NON_THINKING_TOKENIZER_SHA256,
        "request_counter_thinking": False,
    }


def test_frozen_non_thinking_candidate_requires_the_allowlisted_provider_and_model():
    frozen = _freeze((_profile(),))
    candidate_profile = replace(
        frozen.model_plan.primary,
        provider=SUMMARY_NON_THINKING_PROVIDER,
        model=SUMMARY_NON_THINKING_MODEL,
    )
    parsed = _parse_summary_policy(
        {**_base_policy(), **_candidate_identity()}, primary=candidate_profile
    )
    assert {key: parsed[key] for key in _candidate_identity()} == _candidate_identity()

    for unsupported in (
        replace(candidate_profile, provider="deepseek"),
        replace(candidate_profile, model="deepseek-v4.1-flash"),
    ):
        with pytest.raises(AgentHubError):
            _parse_summary_policy(
                {**_base_policy(), **_candidate_identity()}, primary=unsupported
            )


def test_external_summary_request_cannot_select_non_thinking_or_change_the_allowlist():
    with pytest.raises(AgentHubError):
        _normalize_summary_policy_request(
            {
                **_base_policy(),
                "request_strategy_version": SUMMARY_NON_THINKING_STRATEGY_VERSION,
                "request_mode": SUMMARY_NON_THINKING_MODE,
                "request_route_allowlist": SUMMARY_NON_THINKING_ROUTE_ALLOWLIST,
                "request_counter_id": SUMMARY_NON_THINKING_COUNTER_ID,
                "request_counter_revision": SUMMARY_NON_THINKING_COUNTER_REVISION,
                "request_counter_runtime_version": SUMMARY_NON_THINKING_COUNTER_RUNTIME_VERSION,
                "request_counter_sha256": SUMMARY_NON_THINKING_TOKENIZER_SHA256,
                "request_counter_thinking": False,
            }
        )


def test_replay_mode_requires_policy_model_and_counter_identity_to_match():
    identity = _candidate_identity()
    policy = {**_base_policy(), **identity}
    model_spec = {
        "provider": SUMMARY_NON_THINKING_PROVIDER,
        "model": SUMMARY_NON_THINKING_MODEL,
        **identity,
    }
    job = SimpleNamespace(policy_spec=policy, model_spec=model_spec)
    assert _summary_request_mode(job) == SUMMARY_NON_THINKING_MODE

    assert _summary_request_mode(
        SimpleNamespace(
            policy_spec=policy,
            model_spec={**model_spec, "model": "deepseek-v4.1-flash"},
        )
    ) == ""
    assert _summary_request_mode(
        SimpleNamespace(
            policy_spec={**policy, "request_counter_thinking": True},
            model_spec=model_spec,
        )
    ) == ""
    assert _summary_request_mode(
        SimpleNamespace(
            policy_spec={**_base_policy(), "request_mode": SUMMARY_NON_THINKING_MODE},
            model_spec=model_spec,
        )
    ) == ""


@pytest.mark.asyncio
async def test_non_thinking_revision_is_not_reused_without_production_counter():
    service = object.__new__(SummaryJobService)
    revision = await service.find_verified_summary_prefix(
        workspace_id=uuid4(),
        thread_id=uuid4(),
        sources=(object(),),
        policy_version="thread_summary_policy_v1",
        prompt_version="summary_candidate_zh_v1",
        tokenizer_id="approx_language_v1",
        tokenizer_version="1",
        model_spec={},
        permission_snapshot_hash="0" * 64,
        source_scope={},
        request_mode=SUMMARY_NON_THINKING_MODE,
    )
    assert revision is None


@pytest.mark.asyncio
async def test_unverified_non_thinking_revision_cannot_be_consumed_by_fixed_identity():
    identity = _candidate_identity()
    job = SimpleNamespace(
        policy_spec={**_base_policy(), **identity},
        model_spec={
            "provider": SUMMARY_NON_THINKING_PROVIDER,
            "model": SUMMARY_NON_THINKING_MODEL,
            **identity,
        },
    )

    class Result:
        def one_or_none(self):
            return (object(), job, object())

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _statement):
            return Result()

    service = object.__new__(SummaryJobService)
    service._session_factory = Session
    with pytest.raises(SummaryServiceError) as failure:
        await service.get_published_revision(SummaryJobKey(uuid4(), uuid4(), uuid4()))
    assert failure.value.code == "SUMMARY_REQUEST_COUNTER_UNAVAILABLE"
