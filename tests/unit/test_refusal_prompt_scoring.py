from copy import deepcopy

import pytest

from benchmarks.retrieval.experiments.refusal_prompt import scoring as sc


def one_pair(monkeypatch):
    monkeypatch.setattr(sc, "frozen_retrieval", lambda *args: None)
    case = dict(
        id="case",
        qid="case",
        cohort="fresh",
        snapshot="s",
        selected=[],
        question="When?",
        language="en",
        gold={"answerable": False},
        arm_wires={},
        messages=[
            {"role": "system", "content": sc.citation_qa.citation_system_prompt("PROMPT_JSON")},
            {"role": "user", "content": "When? Evidence: no date."},
        ],
    )
    rows = []
    for arm, answer in zip(
        sc.ARMS, ["The date is unknown.", "The evidence gives no date."], strict=True
    ):
        messages = sc.arm_case(case, arm)["messages"]
        proof = dict(
            messages_sha256=sc.digest(messages), parameters_sha256=sc.PARAMETERS, request_sha256=arm
        )
        case["arm_wires"][arm] = proof
        payload = {"answer": answer, "insufficient_evidence": True, "citation_ids": []}
        rows.append(
            dict(
                id="case",
                qid="case",
                cohort="fresh",
                mode="live",
                arm=arm,
                cap=2048,
                http=200,
                events=[dict(proof, id=arm, content=sc.json.dumps(payload))],
                first=dict(valid=True, refused=True, answer=answer, cited=[]),
                body=dict(answer=answer, insufficient_evidence=True, citations=[]),
            )
        )
    return case, rows


def test_distinct_model_generations_blind_scored_by_exact_material(monkeypatch):
    case, rows = one_pair(monkeypatch)
    blind, keys = sc.packets([case], rows)
    assert len(keys) == 4 and len(blind) == 2
    assert all("arm" not in b and "stage" not in b for b in blind.values())


@pytest.mark.parametrize(
    "field", ["messages_sha256", "request_sha256", "parameters_sha256", "cap", "id"]
)
def test_mismatched_treatment_or_reused_provider_receipt_fails(monkeypatch, field):
    case, rows = one_pair(monkeypatch)
    if field == "cap":
        rows[1]["cap"] = 256
    else:
        rows[1]["events"][0][field] = "baseline"
    with pytest.raises(ValueError):
        sc.packets([case], rows)


def pair(ident, answerable):
    state = dict(
        strict=True,
        value=True,
        language=True,
        valid=True,
        refused=not answerable,
        support=answerable,
        reason=None,
    )
    return [
        dict(
            id=ident,
            qid=ident,
            arm=arm,
            language="en",
            invocations=1,
            cost_units=1,
            answerable=answerable,
            model_declared_refusal=not answerable,
            model_cited_a_declared_refusal=False,
            first=state.copy(),
            final=state.copy(),
        )
        for arm in sc.ARMS
    ]


def development():
    rows, cases = [], []
    for lang in ("en", "zh"):
        for answerable in (True, False):
            for i in range(12):
                ident = f"{lang}/{answerable}/{i}"
                ps = pair(ident, answerable)
                for r in ps:
                    r["language"] = lang
                if not answerable and i == 0:
                    ps[0]["first"].update(strict=False, valid=False, refused=False)
                cases.append(dict(id=ident, cohort="fresh"))
                rows.extend(ps)
    for i, q in enumerate([*sorted(sc.NAMED), "W13"]):
        ident = f"target/{i}"
        ps = pair(ident, q not in sc.FAILURES)
        for r in ps:
            r["qid"] = q
        cases.append(dict(id=ident, cohort="target"))
        rows.extend(ps)
    return cases, rows


def test_development_gate_requires_net_gains_and_each_supported_answer():
    cases, rows = development()
    assert all(sc.summarize(cases, rows, "development")[1].values())
    supported = next(
        r for r in rows if r["arm"] == "candidate" and r["language"] == "zh" and r["answerable"]
    )
    supported["final"].update(support=False, strict=False)
    checks = sc.summarize(cases, rows, "development")[1]
    assert not checks["zh_final_prior_correct_preserved"]
    assert not checks["fresh_previously_supported_answerables_remain_supported"]
    assert not checks["fresh_citation_support_rate_preserved"]


def test_protection_requires_original_success_even_if_both_new_answers_fail():
    cases, rows = [], []
    for i in range(390):
        ident = f"g/{i}"
        succeeded = {"first": i == 0, "final": True}
        cases.append(
            dict(
                id=ident,
                cohort="protection",
                messages=[{}, {"content": ident}],
                origins=[dict(strict=succeeded.copy())],
                prior_strict=succeeded.copy(),
            )
        )
        ps = pair(ident, True)
        for r in ps:
            if i == 0:
                r["first"].update(strict=False, support=False)
        rows.extend(ps)
    groups, checks = sc.summarize(cases, rows, "protection")
    assert groups["historical_union"]["first"]["losses"] == ["g/0"]
    assert not checks["historical_correct_stage_union_preserved"]
    bad = deepcopy(cases)
    bad[0]["prior_strict"]["first"] = False
    with pytest.raises(ValueError, match="union"):
        sc.summarize(bad, rows, "protection")
