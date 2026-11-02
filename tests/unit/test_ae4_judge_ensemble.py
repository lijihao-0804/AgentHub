"""AE-4 judge ensemble contracts."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from packages.evaluation.judge import (
    JUDGE_VERSION_PREFIX,
    AnswerQualityJudge,
    FrozenJudgeEnsemble,
    FrozenJudgeProfile,
    JudgeEnsemble,
    judge_ensemble_from_manifest,
)
from packages.evaluation.reproducibility import default_evaluator_manifest

_DIMENSIONS = (
    "instruction_coverage",
    "evidence_grounding",
    "factual_consistency",
    "actionability",
    "clarity",
)


class _FixedClient:
    def __init__(self, score: int) -> None:
        self.score = score

    async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        return json.dumps({"scores": {name: self.score for name in _DIMENSIONS}})


class _BrokenClient:
    async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        raise RuntimeError("judge unavailable")


def _profile(model: str) -> FrozenJudgeProfile:
    return FrozenJudgeProfile(
        provider="fake",
        model=model,
        temperature=0.0,
        max_tokens=128,
        timeout_seconds=10.0,
        profile_id=str(uuid4()),
        credential_ref=str(uuid4()),
    )


def _judge(score: int, model: str = "model") -> AnswerQualityJudge:
    return AnswerQualityJudge(_profile(model), _FixedClient(score))


def _broken(model: str = "broken") -> AnswerQualityJudge:
    return AnswerQualityJudge(_profile(model), _BrokenClient())


@pytest.mark.asyncio
async def test_majority_uses_the_modal_dimension_score() -> None:
    ensemble = JudgeEnsemble([_judge(4), _judge(4), _judge(3)], strategy="MAJORITY")

    verdict = await ensemble.judge(_request())

    assert verdict["status"] == "SCORED"
    assert verdict["dimension_scores"]["clarity"] == 4
    assert verdict["score"] == pytest.approx(1.0)
    assert verdict["judge_agreement"] == pytest.approx(2 / 3)


@pytest.mark.asyncio
async def test_mean_strategy_rounds_two_judges() -> None:
    ensemble = JudgeEnsemble([_judge(4), _judge(2)], strategy="MEAN")

    verdict = await ensemble.judge(_request())

    assert verdict["status"] == "SCORED"
    assert verdict["dimension_scores"]["clarity"] == 3


@pytest.mark.asyncio
async def test_disagreement_is_inconclusive_not_averaged() -> None:
    ensemble = JudgeEnsemble([_judge(4), _judge(1), _judge(2)], strategy="MAJORITY")

    verdict = await ensemble.judge(_request())

    assert verdict["status"] == "UNPARSEABLE"
    assert verdict["reason"] == "judge_disagreement"


@pytest.mark.asyncio
async def test_quorum_failure_is_failed() -> None:
    ensemble = JudgeEnsemble([_judge(4), _broken(), _broken()], strategy="MAJORITY")

    verdict = await ensemble.judge(_request())

    assert verdict["status"] == "FAILED"
    assert verdict["reason"] == "judge_ensemble_quorum_not_met"


def test_ensemble_manifest_roundtrip() -> None:
    ensemble = FrozenJudgeEnsemble((_profile("a"), _profile("b")), strategy="MAJORITY")
    manifest = default_evaluator_manifest(judge_ensemble=ensemble)

    rebuilt = judge_ensemble_from_manifest(manifest)

    assert rebuilt is not None
    assert rebuilt.evaluator_version == ensemble.evaluator_version
    assert manifest["evaluator_versions"]["answer-quality-judge"] == ensemble.evaluator_version
    assert manifest["evaluator_versions"]["answer-quality-judge"].startswith(JUDGE_VERSION_PREFIX)


def test_ensemble_requires_one_rubric() -> None:
    from packages.evaluation.judge import AnswerQualityRubric, RubricDimension

    other_rubric = AnswerQualityRubric(
        rubric_id="other",
        rubric_version=1,
        dimensions=(RubricDimension("clarity", 1, "different"),),
    )
    other = FrozenJudgeProfile(
        provider="fake",
        model="other",
        temperature=0.0,
        max_tokens=128,
        timeout_seconds=10.0,
        profile_id=str(uuid4()),
        credential_ref=str(uuid4()),
        rubric=other_rubric,
    )

    with pytest.raises(ValueError, match="share one rubric"):
        FrozenJudgeEnsemble((_profile("a"), other))


def _request():
    from packages.evaluation.judge import JudgeRequest

    return JudgeRequest(question="q", answer="a")
