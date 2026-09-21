import json
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from packages.core.errors.exceptions import AgentHubError
from packages.evaluation.judge import (
    DEFAULT_ANSWER_QUALITY_RUBRIC,
    JUDGE_DISABLED_VERSION,
    JUDGE_MANIFEST_KEY,
    JUDGE_OBSERVATION_KEY,
    AnswerQualityJudge,
    JudgeRequest,
    JudgeVerdictStatus,
    freeze_judge_profile,
    is_judge_evaluator_version,
    judge_profile_from_manifest,
)
from packages.evaluation.metrics import (
    EvaluatorRegistry,
    MetricStatus,
    aggregate_metric_values,
    evaluate_answer_quality,
)
from packages.evaluation.reproducibility import default_evaluator_manifest, frozen_judge_version
from packages.evaluation.runner import AgentRuntimeEvaluationDriver

_PROFILE_ID = UUID("11111111-1111-1111-1111-111111111111")
_CREDENTIAL_ID = UUID("22222222-2222-2222-2222-222222222222")


class StubJudgeClient:
    """Offline judge transport: the judge is never allowed to reach a network in tests."""

    def __init__(self, response: str | Exception) -> None:
        self.response = response
        self.calls: list[tuple[str, str]] = []

    async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _resolved_profile(model: str = "judge-model-a") -> SimpleNamespace:
    return SimpleNamespace(
        id=_PROFILE_ID,
        workspace_id=uuid4(),
        provider_credential_id=_CREDENTIAL_ID,
        model=model,
        temperature=Decimal("0"),
        max_tokens=512,
        timeout_seconds=Decimal("30"),
    )


def _frozen(model: str = "judge-model-a", provider: str = "openai"):
    return freeze_judge_profile(_resolved_profile(model), provider=provider)


def _perfect_scores() -> str:
    return json.dumps(
        {
            "scores": {
                dimension.name: 4 for dimension in DEFAULT_ANSWER_QUALITY_RUBRIC.dimensions
            },
            "rationale": "complete and grounded",
        }
    )


def test_judge_profile_freeze_is_stable_and_model_sensitive() -> None:
    first = _frozen()
    second = _frozen()
    other_model = _frozen(model="judge-model-b")
    other_provider = _frozen(provider="anthropic")

    assert first.evaluator_version == second.evaluator_version
    assert is_judge_evaluator_version(first.evaluator_version)
    assert len(first.evaluator_version) <= 32
    assert first.evaluator_version != other_model.evaluator_version
    assert first.evaluator_version != other_provider.evaluator_version


def test_frozen_judge_projection_carries_no_secret() -> None:
    projection = _frozen().projection()
    forbidden = {"secret", "encrypted_secret", "secret_ciphertext", "authorization", "headers"}

    assert forbidden.isdisjoint(projection)
    assert projection["credential_ref"] == str(_CREDENTIAL_ID)


@pytest.mark.asyncio
async def test_judge_scores_open_ended_output_deterministically() -> None:
    judge = AnswerQualityJudge(_frozen(), StubJudgeClient(_perfect_scores()))

    verdict = await judge.judge(JudgeRequest(question="summarise", answer="a grounded summary"))

    assert verdict["status"] == JudgeVerdictStatus.SCORED
    assert verdict["score"] == 1.0
    assert verdict["judge_evaluator_version"] == judge.profile.evaluator_version
    assert verdict["rubric_version"] == DEFAULT_ANSWER_QUALITY_RUBRIC.rubric_version
    assert "rationale" not in verdict
    assert verdict["rationale_hash"] is not None


@pytest.mark.asyncio
async def test_judge_weighting_is_the_rubric_weighted_mean() -> None:
    scores = {dimension.name: 2 for dimension in DEFAULT_ANSWER_QUALITY_RUBRIC.dimensions}
    scores["clarity"] = 4
    judge = AnswerQualityJudge(_frozen(), StubJudgeClient(json.dumps({"scores": scores})))

    verdict = await judge.judge(JudgeRequest(question="q", answer="a"))

    expected = ((2 * 9) + (4 * 1)) / (DEFAULT_ANSWER_QUALITY_RUBRIC.total_weight * 4)
    assert verdict["score"] == pytest.approx(expected)


@pytest.mark.asyncio
async def test_unusable_judge_output_never_becomes_a_score() -> None:
    judge = AnswerQualityJudge(_frozen(), StubJudgeClient("I think it was pretty good!"))
    out_of_range = AnswerQualityJudge(
        _frozen(), StubJudgeClient(json.dumps({"scores": {"clarity": 9}}))
    )
    failing = AnswerQualityJudge(_frozen(), StubJudgeClient(RuntimeError("provider down")))

    unparseable = await judge.judge(JudgeRequest(question="q", answer="a"))
    invalid = await out_of_range.judge(JudgeRequest(question="q", answer="a"))
    failed = await failing.judge(JudgeRequest(question="q", answer="a"))

    assert unparseable["status"] == JudgeVerdictStatus.UNPARSEABLE
    assert unparseable["score"] is None
    assert invalid["status"] == JudgeVerdictStatus.UNPARSEABLE
    assert failed["status"] == JudgeVerdictStatus.FAILED
    assert failed["reason"] == "judge_call_failed"


def test_manifest_freezes_the_judge_identity() -> None:
    profile = _frozen()
    manifest = default_evaluator_manifest(profile)
    without_judge = default_evaluator_manifest()

    assert manifest["evaluator_versions"][JUDGE_MANIFEST_KEY] == profile.evaluator_version
    assert without_judge["evaluator_versions"][JUDGE_MANIFEST_KEY] == JUDGE_DISABLED_VERSION
    assert frozen_judge_version(manifest) == profile.evaluator_version

    registry = EvaluatorRegistry()
    registry.validate_manifest(manifest)
    registry.validate_manifest(without_judge)


def test_manifest_with_a_malformed_judge_entry_is_rejected() -> None:
    manifest = default_evaluator_manifest()
    manifest["evaluator_versions"][JUDGE_MANIFEST_KEY] = "gpt-whatever"

    with pytest.raises(ValueError):
        EvaluatorRegistry().validate_manifest(manifest)


@pytest.mark.asyncio
async def test_registry_scores_answer_quality_only_for_the_frozen_judge() -> None:
    profile = _frozen()
    judge = AnswerQualityJudge(profile, StubJudgeClient(_perfect_scores()))
    verdict = await judge.judge(JudgeRequest(question="q", answer="a"))
    observation = {"answer": "ok", "citation_ids": [], JUDGE_OBSERVATION_KEY: verdict}
    expected = {"answer": "ok", "citations": []}

    bound = EvaluatorRegistry()
    bound.bind_judge_manifest(default_evaluator_manifest(profile))
    scored = bound.evaluate("KNOWLEDGE_QA", expected, observation)

    unbound = EvaluatorRegistry()
    unpinned = unbound.evaluate("KNOWLEDGE_QA", expected, observation)

    other = EvaluatorRegistry()
    other.bind_judge_manifest(default_evaluator_manifest(_frozen(model="judge-model-b")))
    mismatched = other.evaluate("KNOWLEDGE_QA", expected, observation)

    assert scored["answer_quality"].status == MetricStatus.AVAILABLE
    assert scored["answer_quality"].value == 1.0
    assert scored["answer_quality"].evaluator_version == profile.evaluator_version
    assert bound.version_for_metric("answer_quality") == profile.evaluator_version
    assert unpinned["answer_quality"].status == MetricStatus.NOT_AVAILABLE
    assert unpinned["answer_quality"].reason == "judge_not_frozen_in_manifest"
    assert mismatched["answer_quality"].status == MetricStatus.NOT_AVAILABLE
    assert mismatched["answer_quality"].reason == "judge_identity_mismatch"


@pytest.mark.asyncio
async def test_judge_never_alters_deterministic_metrics() -> None:
    profile = _frozen()
    judge = AnswerQualityJudge(profile, StubJudgeClient(_perfect_scores()))
    verdict = await judge.judge(JudgeRequest(question="q", answer="a"))
    expected = {"answer": "right", "citations": ["chunk-1"]}
    registry = EvaluatorRegistry()
    registry.bind_judge_manifest(default_evaluator_manifest(profile))

    without_judge = registry.evaluate(
        "KNOWLEDGE_QA", expected, {"answer": "wrong", "citation_ids": ["chunk-1"]}
    )
    with_judge = registry.evaluate(
        "KNOWLEDGE_QA",
        expected,
        {"answer": "wrong", "citation_ids": ["chunk-1"], JUDGE_OBSERVATION_KEY: verdict},
    )

    assert "answer_quality" not in without_judge
    assert with_judge["answer_quality"].value == 1.0
    for name, metric in without_judge.items():
        assert with_judge[name].status == metric.status
        assert with_judge[name].value == metric.value
    assert with_judge["task_success"].value == 0


def test_aggregate_answer_quality_carries_the_judge_evaluator_version() -> None:
    profile = _frozen()
    registry = EvaluatorRegistry()
    registry.bind_judge_manifest(default_evaluator_manifest(profile))
    values = [
        evaluate_answer_quality(
            {JUDGE_OBSERVATION_KEY: _verdict(profile, score)},
            judge_version=profile.evaluator_version,
        )
        for score in (1.0, 0.5)
    ]

    aggregated = aggregate_metric_values(
        "answer_quality", values, version=registry.version_for_metric("answer_quality")
    )

    assert aggregated.value == pytest.approx(0.75)
    assert aggregated.evaluator_version == profile.evaluator_version
    assert aggregated.to_dict()["evaluator_version"] == profile.evaluator_version


def test_unscored_verdicts_are_not_available_rather_than_zero() -> None:
    profile = _frozen()
    verdict = {
        **_verdict(profile, 1.0),
        "status": JudgeVerdictStatus.FAILED,
        "reason": "judge_call_failed",
        "score": None,
    }

    metric = evaluate_answer_quality(
        {JUDGE_OBSERVATION_KEY: verdict}, judge_version=profile.evaluator_version
    )

    assert metric is not None
    assert metric.status == MetricStatus.NOT_AVAILABLE
    assert metric.reason == "judge_call_failed"
    assert evaluate_answer_quality({}, judge_version=profile.evaluator_version) is None


@pytest.mark.asyncio
async def test_driver_only_judges_items_that_opt_into_the_open_ended_slice() -> None:
    driver = AgentRuntimeEvaluationDriver(
        agent_run_service=SimpleNamespace(),
        context_factory=None,
        judge=AnswerQualityJudge(_frozen(), StubJudgeClient(_perfect_scores())),
    )
    open_ended = SimpleNamespace(
        category="KNOWLEDGE_QA", input={"question": "why"}, expected={"open_ended": True}
    )
    closed = SimpleNamespace(
        category="KNOWLEDGE_QA", input={"question": "why"}, expected={"answer": "42"}
    )

    assert driver.judge_request(closed, "some prose") is None
    assert driver.judge_request(open_ended, "   ") is None
    assert driver.judge_request(open_ended, "some prose") is not None
    assert (await driver.judge_answer(closed, "some prose")) is None
    judged = await driver.judge_answer(open_ended, "some prose")
    assert judged is not None
    assert judged["status"] == JudgeVerdictStatus.SCORED


@pytest.mark.asyncio
async def test_driver_without_a_judge_produces_no_verdict() -> None:
    driver = AgentRuntimeEvaluationDriver(
        agent_run_service=SimpleNamespace(), context_factory=None
    )
    item = SimpleNamespace(
        category="KNOWLEDGE_QA", input={"question": "why"}, expected={"open_ended": True}
    )

    assert (await driver.judge_answer(item, "some prose")) is None


def _verdict(profile, score: float) -> dict:
    return {
        "status": JudgeVerdictStatus.SCORED,
        "reason": None,
        "score": score,
        "dimension_scores": {},
        "rationale_hash": None,
        "rubric_id": DEFAULT_ANSWER_QUALITY_RUBRIC.rubric_id,
        "rubric_version": DEFAULT_ANSWER_QUALITY_RUBRIC.rubric_version,
        "judge_identity_hash": profile.identity_hash,
        "judge_evaluator_version": profile.evaluator_version,
    }


def test_the_worker_rebuilds_exactly_the_frozen_judge() -> None:
    """The worker scores with the manifest's judge, not with a re-resolved profile."""

    profile = _frozen()
    rebuilt = judge_profile_from_manifest(default_evaluator_manifest(profile))

    assert rebuilt == profile
    assert rebuilt.evaluator_version == profile.evaluator_version
    assert judge_profile_from_manifest(default_evaluator_manifest()) is None
    assert judge_profile_from_manifest(None) is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda manifest: manifest.pop("judge_profile"),
        lambda manifest: manifest["judge_profile"].update(model="a-cheaper-model"),
        lambda manifest: manifest["judge_profile"]["rubric"]["dimensions"][0].update(weight=9),
    ],
)
def test_a_judge_projection_that_no_longer_matches_its_version_is_refused(mutate) -> None:
    manifest = default_evaluator_manifest(_frozen())
    mutate(manifest)

    with pytest.raises(AgentHubError):
        judge_profile_from_manifest(manifest)
