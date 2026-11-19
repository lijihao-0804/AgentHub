"""No-egress verification of requested-fact delivery and historical protections."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from benchmarks.retrieval.experiments.first_refusal_contract.endpoint import original
from benchmarks.retrieval.experiments.requested_fact import billing, campaign
from benchmarks.retrieval.experiments.requested_fact.endpoint import (
    DryTransport,
    FactEndpoint,
    arm_case,
)
from packages.knowledge import citation_qa as qa


def test_only_system_prompt_changes_in_new_arm():
    messages = [
        dict(role="system", content=qa.citation_system_prompt("PROMPT_JSON")),
        dict(role="user", content="exact evidence"),
    ]
    c = dict(messages=messages, first_request_sha256="archive", registered_wire="baseline")
    assert arm_case(c, "baseline") == c
    projected = arm_case(c, "candidate")
    assert projected["messages"][1] == messages[1] and projected["messages"][0] != messages[0]
    assert "first_request_sha256" not in projected and "registered_wire" not in projected
    with pytest.raises(ValueError):
        arm_case(dict(c, messages=[dict(role="system", content="wrong"), messages[1]]), "candidate")


class ScriptedDry(DryTransport):
    def __init__(self, contents):
        super().__init__("candidate")
        self.contents = iter(contents)
        self.messages = []

    async def arun(self, key, qid, messages, cap, send):
        body = await super().arun(key, qid, messages, cap, send)
        self.messages.append(messages)
        body["choices"][0]["message"]["content"] = next(self.contents)
        return body


@pytest.mark.parametrize("empty", [False, True])
def test_real_product_observes_the_exact_attempt_before_correction(monkeypatch, empty):
    if not (campaign.EARLY_DIR / "cases.json").exists():
        pytest.skip("Private frozen evidence required")
    c = dict(campaign.load(campaign.EARLY_DIR / "cases.json")[0], mode="live")
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")

    def no_egress(*args, **kwargs):
        pytest.fail("Physical egress attempted")

    monkeypatch.setattr(original.GuardedProviderTransport, "handle_async_request", no_egress)
    response = (
        ""
        if empty
        else json.dumps(
            {"kind": "supported", "claims": [{"text": "A claim.", "citation_ids": [999]}]}
        )
    )
    dry = ScriptedDry([response, json.dumps({"kind": "insufficient"})])
    route = SimpleNamespace(
        base_url=original.BASE_URL,
        model=original.MODEL,
        provider="openai-compatible",
        api_key="inert",
    )

    async def run():
        async with FactEndpoint(route, "candidate") as endpoint:
            endpoint.meter = dry
            return await endpoint.measure(c)

    result = asyncio.run(run())
    assert not result["first"]["valid"]
    assert result["events"][0]["content"] == response
    assert all(e["provenance"] == "inert" for e in result["events"])
    if empty:
        assert result["first"]["reason"] == "empty_output" and result["http"] == 502
        assert len(result["events"]) == 1
    else:
        assert result["http"] == 200 and result["body"]["insufficient_evidence"]
        assert len(result["events"]) == 2 and result["events"][1]["parser"]["valid"]
        assert dry.messages[1][-1] == {"role": "user", "content": qa._REQUESTED_FACT_CORRECTION}


def test_all_success_and_failure_origins_receive_live_candidate():
    if not (billing.LAST / "development/account.json").exists():
        pytest.skip("Private sealed campaigns required")
    first, second, ledger = billing.opening()
    histories, obs = campaign.historical(first, second)
    assert len(histories) == 446 and len(ledger) == 836
    plan = campaign.schedule(histories)
    assert sum(a == "candidate" and src == "live" for _, a, src in plan) == 446
    assert sorted(c["qid"] for c in histories if not c["ref_id"]) == ["W06", "W07", "W12"]
    assert len({campaign.prev.input_id(c) for c in histories}) == len(histories)
    assert sum(c["false_release"] for c in histories) == 17
    assert sum(c["named"] for c in histories) == 7
    assert sum(c["gold_conflict"] for c in histories) == 2
    for c in histories:
        assert c["prior_strict"] == {
            s: any(o["strict"][s] for o in c["origins"]) for s in campaign.STAGES
        }
    assert campaign.forecast(histories, obs)["early_joint_six"] == dict(
        registered_arms=556, strict_arms=259, distinct_correct_inputs=250
    )


@pytest.mark.parametrize(
    "fault", [None, "development_overlap", "missing_source", "changed_product", "surplus"]
)
def test_confirmation_registration_checks_holdout_ancestry_and_exact_strata(monkeypatch, fault):
    # Private confirmatory questions never enter these tests.
    rows = []
    for i in range(49 if fault == "surplus" else 48):
        ident = str(i)
        rows.append(
            dict(
                id=ident,
                language="en" if i < 24 else "zh",
                gold={"answerable": i % 2 == 0},
                documents=[{"text": f"Synthetic source {i}"}],
                question=f"Synthetic query {i}?",
                messages=[
                    {"role": "system", "content": "test"},
                    {"role": "user", "content": ident},
                ],
                kb="kb-" + ident,
                snapshot="snap-" + ident,
            )
        )
    prep = campaign.OUT / "independent"
    fixture = campaign.FOLDER / "fixtures/confirmatory.jsonl"
    sources = campaign.preconfirmation_sources()
    source_shas = {
        p.relative_to(campaign.primary.ROOT).as_posix(): "synthetic-sha" for p in sources
    }
    dev_path = campaign.FOLDER / "fixtures/fresh.jsonl"
    if fault == "missing_source":
        source_shas.pop(dev_path.relative_to(campaign.primary.ROOT).as_posix())
    manifest = dict(
        product_commit="other" if fault == "changed_product" else campaign.PRODUCT_COMMIT,
        product_system_content_sha256=campaign.hashlib.sha256(
            qa.citation_system_prompt("PROMPT_JSON").encode()
        ).hexdigest(),
        records_relative_path=(prep / "fresh-records.json")
        .relative_to(campaign.primary.ROOT)
        .as_posix(),
        source_sha256="synthetic-sha",
        frozen_cases_sha256="synthetic-sha",
        records_sha256="synthetic-sha",
        history_sources_sha256=source_shas,
        questions=48,
        distinct_snapshots=48,
        complete_visible_source_sets=48,
        messages_canonical_sha256=campaign.digest(
            {r["id"]: campaign.digest(r["messages"]) for r in rows}
        ),
        exact_question_overlaps=0,
        exact_passage_overlaps=0,
        per_language_answerability_counts={
            "en_answerable": 12,
            "en_refusal": 12,
            "zh_answerable": 12,
            "zh_refusal": 12,
        },
    )

    def get(path):
        if path == fixture or path == prep / "fresh-cases.json":
            return rows
        if path == prep / "verification.json":
            return manifest
        if path == prep / "fresh-records.json":
            return {r["id"]: {"messages": r["messages"]} for r in rows}
        if path == dev_path and fault == "development_overlap":
            return [{"question": "Different example?", "documents": rows[0]["documents"]}]
        assert path in sources
        return []

    monkeypatch.setattr(campaign, "load", get)
    monkeypatch.setattr(campaign, "lines", get)
    monkeypatch.setattr(campaign, "file_hash", lambda p: "synthetic-sha")
    if fault:
        with pytest.raises(ValueError):
            campaign.independent([])
    else:
        assert len(campaign.independent([])) == 48
