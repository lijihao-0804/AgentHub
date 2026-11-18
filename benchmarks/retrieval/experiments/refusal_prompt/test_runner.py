"""Offline tests for treatment attribution and the two-stage physical budget."""

import asyncio
import json
from collections import Counter
from types import SimpleNamespace

import pytest

from benchmarks.retrieval.experiments.first_refusal_contract import billing as first
from benchmarks.retrieval.experiments.output_limit.meter import MODEL
from benchmarks.retrieval.experiments.output_limit.report import money
from benchmarks.retrieval.experiments.refusal_prompt import billing, campaign
from benchmarks.retrieval.experiments.refusal_prompt.endpoint import (
    PromptEndpoint,
    arm_case,
    original,
)


def test_candidate_has_only_the_registered_system_delta():
    messages = [
        dict(role="system", content=original.qa.citation_system_prompt("PROMPT_JSON")),
        dict(role="user", content="exact question and evidence"),
    ]
    case = dict(messages=messages, first_request_sha256="old_wire", registered_wire="old_cap")
    new = arm_case(case, "candidate")
    assert arm_case(case, "baseline") == case
    assert new["messages"][0] != messages[0]
    assert new["messages"][1] == messages[1]
    assert "first_request_sha256" not in new and "registered_wire" not in new
    with pytest.raises(ValueError):
        arm_case(
            dict(case, messages=[dict(role="system", content="wrong"), messages[1]]), "candidate"
        )


def test_pair_uses_real_prompt_but_same_parser(monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    route = SimpleNamespace(
        base_url=original.BASE_URL,
        provider="openai-compatible",
        model=MODEL,
        api_key="offline-only",
    )
    if not billing.PREDECESSOR.joinpath("cases.json").is_file():
        pytest.skip("Requires private frozen evidence")
    case = dict(campaign.load(billing.PREDECESSOR / "cases.json")[0], mode="live")

    def fail(*args, **kwargs):
        pytest.fail("An inert preflight tried to dispatch")

    monkeypatch.setattr(original.GuardedProviderTransport, "handle_async_request", fail)

    async def dry():
        events = []
        for arm in campaign.ARMS:
            async with PromptEndpoint(route, arm) as ep:
                obs = await ep.measure(case)
            assert obs["arm"] == arm and obs["first"]["valid"]
            assert obs["http"] == 200 and len(obs["events"]) == 1
            events.append(obs["events"][0])
        assert events[0]["parameters_sha256"] == events[1]["parameters_sha256"]
        assert events[0]["request_sha256"] != events[1]["request_sha256"]

    asyncio.run(dry())


def test_guard_population_deduplicates_inputs_but_keeps_all_correct_stages():
    if not billing.PREDECESSOR.joinpath("ledger.sqlite").is_file():
        pytest.skip("Requires prior sealed local receipts")
    cases, rows, grades, ledger = billing.previous()
    targets, guards, _ = campaign.plan_history(cases, rows, grades)
    target_keys = {campaign.input_id(c) for c in targets}
    assert len(ledger) + len(first.early_ids()) == 1760
    assert len(guards) == len({campaign.input_id(c) for c in guards}) == 390
    origins = [o for c in guards for o in c["origins"]]
    assert len(origins) == 335 + 61
    for c in guards:
        assert c["prior_strict"] == {
            s: any(o["strict"][s] for o in c["origins"]) for s in ("first", "final")
        }
    assert Counter(src for _, _, src in campaign.physical_plan(guards, target_keys)) == {
        "historical_baseline": 390,
        "live": 383,
        "development_candidate_replay": 7,
    }


def test_stage_account_distinguishes_historical_development_and_new_receipts():
    body = dict(
        id="receipt",
        created=1,
        model=MODEL,
        usage=dict(
            prompt_tokens=12,
            completion_tokens=4,
            total_tokens=16,
            prompt_tokens_details={"cached_tokens": 2},
        ),
        choices=[dict(message=dict(content="a"), finish_reason="stop")],
    )
    event = dict(
        id="receipt",
        created=1,
        usage=body["usage"],
        finish="stop",
        content="a",
        physical_key="paired/development/T/2048/1/wire",
        messages_sha256="message",
        request_sha256="wire",
        provenance="provider",
    )
    q = dict(
        id="development/T",
        qid="T",
        cap=2048,
        mode="live",
        arm="candidate",
        source="live",
        events=[event],
    )
    cost = first.charge(body, 2048)
    ledger = {
        "receipt": dict(
            key=event["physical_key"],
            qid="T",
            cap=2048,
            digest="message",
            cost=cost,
            response=json.dumps(body),
        )
    }
    a = billing.close([q], {}, ledger, billing.OPENING)
    assert a["new_physical"] == 1 and a["cumulative_cny"] == money(billing.OPENING + cost)
    replay = dict(
        q,
        id="protection/G",
        source="development_candidate_replay",
        events=[dict(event, provenance="development_candidate_replay")],
    )
    b = billing.close([replay], ledger, ledger, billing.OPENING + cost)
    assert b["new_physical"] == 0 and b["cumulative_cny"] == a["cumulative_cny"]
    assert b["prior_stage_live_candidate_invocations_replayed"] == 1
    with pytest.raises(ValueError):
        billing.close([dict(replay, events=[event])], ledger, ledger, billing.OPENING + cost)


@pytest.mark.parametrize("checks", [{}, {"fresh": False}, {"fresh": True, "named": False}])
def test_failed_development_gate_prevents_protection_before_any_reservation(monkeypatch, checks):
    from benchmarks.retrieval.experiments.refusal_prompt import scoring

    reg = {"cases": {}}
    monkeypatch.setattr(campaign, "read_registered", lambda stage, registration: [])
    monkeypatch.setattr(campaign, "read_ledger", lambda path: {})
    monkeypatch.setattr(campaign, "lines", lambda path: [])
    previous_account = billing.close([], {}, {}, billing.OPENING)
    previous_account["freeze_sha"] = campaign.digest(reg)
    monkeypatch.setattr(campaign, "load", lambda path: previous_account)
    monkeypatch.setattr(scoring, "development_gate", lambda directory: checks)
    with pytest.raises(ValueError, match="quality gate"):
        campaign.stage_material("protection", reg, {}, {})
