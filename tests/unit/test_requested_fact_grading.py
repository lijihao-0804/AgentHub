import pytest

from benchmarks.retrieval.experiments.first_refusal_contract.report import FAILURES, NAMED
from benchmarks.retrieval.experiments.requested_fact import scoring


@pytest.mark.parametrize(
    "answerable,expected_language,value,language",
    [(False, "en", True, True), (True, "en", False, True), (False, "zh", True, False)],
)
def test_only_exact_localized_refusal_can_skip_new_semantic_review(
    answerable, expected_language, value, language
):
    case = dict(
        uid="uid",
        answer=next(iter(scoring.REFUSALS)),
        refused=True,
        cited=[],
        language=expected_language,
        gold=dict(answerable=answerable),
    )
    label = scoring.canonical_label(case)
    assert (label["value"], label["support"], label["language"]) == (value, False, language)
    assert scoring.canonical_label(dict(case, refused=False)) is None
    assert scoring.canonical_label(dict(case, cited=[1])) is None
    assert scoring.canonical_label(dict(case, answer=case["answer"] + " But also ...")) is None


def test_correct_old_stage_is_protected_even_if_archived_baseline_was_wrong():
    cases, rows = [], []

    def add(key, qid, language, a, old_first, new_first, old_final=True, new_final=True, **tags):
        flags = dict(named=False, false_release=False, protected=False)
        flags.update(tags)
        cohort = "historical" if any(flags.values()) else "fresh"
        cases.append(
            dict(
                id=key,
                cohort=cohort,
                fact_type="evidenced_negative" if a else "missing_exact_value",
                prior_strict={"first": cohort == "historical", "final": cohort == "historical"},
                **flags,
            )
        )
        for arm, f, e in zip(
            scoring.ARMS, [old_first, new_first], [old_final, new_final], strict=True
        ):

            def answer(correct):
                return dict(
                    strict=correct,
                    value=correct,
                    language=True,
                    support=a and correct,
                    valid=True,
                    refused=(not a and correct),
                    reason=None,
                )

            rows.append(
                dict(
                    id=key,
                    qid=qid,
                    arm=arm,
                    language=language,
                    answerable=a,
                    invocations=1,
                    model_declared_refusal=not a,
                    model_cited_a_declared_refusal=False,
                    cost_units=0,
                    first=answer(f),
                    final=answer(e),
                )
            )

    for lang in ("en", "zh"):
        for a in (True, False):
            for i in range(12):
                ident = f"{lang}/{a}/{i}"
                add(ident, ident, lang, a, a or i > 0, True)
    for i, qid in enumerate([*sorted(NAMED), "W13"]):
        add(
            "named" + str(i), qid, "en", qid not in FAILURES, True, True, named=True, protected=True
        )
    for i in range(17):
        add("old" + str(i), str(i), "zh", False, False, i != 0, false_release=True, protected=True)
    _, union, checks = scoring.summarize(cases, rows)
    assert checks["independent_net_gain_strict"]
    assert checks["prior_false_releases_reduced"]
    assert checks["named_first_and_final_correct"]
    assert union["first"]["losses"] == ["old0"]
    assert not checks["historical_union_preserved"]
