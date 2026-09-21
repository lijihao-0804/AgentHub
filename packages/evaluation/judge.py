"""LLM-as-judge scoring for the open-ended slice of the evaluation platform.

Deterministic evaluators stay primary.  The judge only produces a supplementary
``answer_quality`` score for output that exact-match evaluators cannot score at all
(research summaries, incident analyses, data interpretations).

The judge is frozen exactly the way an ``AgentVersion`` freezes its model profile: the
non-secret provider/model/parameter projection is hashed with the rubric identity, and
that hash becomes the evaluator version recorded on every ``answer_quality`` metric row.
Swapping the judge model therefore changes the evaluator version, and the existing
``evaluator_version`` guard in the comparison path refuses to compare the two runs
instead of silently treating them as comparable.

No secret ever reaches this module.  ``ModelGatewayJudgeClient`` calls the model gateway,
which resolves the encrypted ``provider_credentials`` record itself, and the persisted
verdict carries scores, identities and hashes only -- never the judged text.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from packages.core.canonical.json_hash import canonical_json_hash
from packages.core.errors.exceptions import AgentHubError
from packages.core.execution_context.models import WorkspaceExecutionContext
from packages.model_gateway.contracts import (
    CapabilityRequirements,
    ModelGateway,
    ModelMessage,
    ModelRequest,
)

#: Manifest key under ``evaluator_versions`` that pins the frozen judge identity.
JUDGE_MANIFEST_KEY = "answer-quality-judge"

#: Manifest value used when an experiment was frozen without a judge.
JUDGE_DISABLED_VERSION = "disabled"

#: Evaluator-version prefix.  ``v1`` is the judging algorithm; the suffix is the
#: frozen judge identity hash, so the whole string fits ``String(32)``.
JUDGE_VERSION_PREFIX = "jv1-"

#: Length of the identity hash prefix embedded in the evaluator version.
JUDGE_VERSION_HASH_LENGTH = 16

#: Key under ``EvaluationExperimentCaseResult.observation`` holding the verdict.
JUDGE_OBSERVATION_KEY = "answer_quality_judgement"

#: Metric produced from the verdict.
ANSWER_QUALITY_METRIC = "answer_quality"

_MAX_DIMENSION_SCORE = 4


class JudgeVerdictStatus:
    """Verdict outcomes.  Only ``SCORED`` yields an available metric."""

    SCORED = "SCORED"
    UNPARSEABLE = "UNPARSEABLE"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class RubricDimension:
    name: str
    weight: int
    guidance: str


@dataclass(frozen=True, slots=True)
class AnswerQualityRubric:
    """A frozen rubric.  Editing it requires a new ``rubric_version``."""

    rubric_id: str
    rubric_version: int
    dimensions: tuple[RubricDimension, ...]

    def __post_init__(self) -> None:
        if not self.dimensions:
            raise ValueError("a rubric requires at least one dimension")
        if len({dimension.name for dimension in self.dimensions}) != len(self.dimensions):
            raise ValueError("rubric dimension names must be unique")
        if any(dimension.weight <= 0 for dimension in self.dimensions):
            raise ValueError("rubric dimension weights must be positive")

    @property
    def total_weight(self) -> int:
        return sum(dimension.weight for dimension in self.dimensions)

    def projection(self) -> dict[str, Any]:
        return {
            "rubric_id": self.rubric_id,
            "rubric_version": self.rubric_version,
            "max_dimension_score": _MAX_DIMENSION_SCORE,
            "dimensions": [
                {"name": dimension.name, "weight": dimension.weight, "guidance": dimension.guidance}
                for dimension in self.dimensions
            ],
        }

    def normalize(self, scores: Mapping[str, int]) -> float:
        """Deterministic weighted mean of integer dimension scores, mapped to [0, 1]."""
        weighted = sum(
            dimension.weight * scores[dimension.name] for dimension in self.dimensions
        )
        return weighted / (self.total_weight * _MAX_DIMENSION_SCORE)


DEFAULT_ANSWER_QUALITY_RUBRIC = AnswerQualityRubric(
    rubric_id="answer-quality",
    rubric_version=1,
    dimensions=(
        RubricDimension(
            "instruction_coverage",
            2,
            "Every part of the request is addressed, with no invented extra scope.",
        ),
        RubricDimension(
            "evidence_grounding",
            3,
            "Claims are traceable to the supplied evidence or reference answer.",
        ),
        RubricDimension(
            "factual_consistency",
            3,
            "No statement contradicts the supplied evidence or reference answer.",
        ),
        RubricDimension(
            "actionability",
            1,
            "Conclusions are concrete enough for the reader to act on.",
        ),
        RubricDimension(
            "clarity",
            1,
            "Structure and wording are unambiguous for the intended reader.",
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class FrozenJudgeProfile:
    """The non-secret judge identity, frozen like an AgentVersion model projection."""

    provider: str
    model: str
    temperature: float
    max_tokens: int
    timeout_seconds: float
    profile_id: str
    credential_ref: str
    rubric: AnswerQualityRubric = DEFAULT_ANSWER_QUALITY_RUBRIC

    def projection(self) -> dict[str, Any]:
        """Mirror of ``packages.agent_runtime.publish._profile_projection`` plus the rubric.

        The credential reference is an identifier, never a secret: the encrypted
        material stays in ``provider_credentials`` and is only read by the gateway.
        """
        return {
            "judge_algorithm_version": JUDGE_VERSION_PREFIX.rstrip("-"),
            "provider": self.provider,
            "model": self.model,
            "temperature": _canonical_float(self.temperature),
            "max_tokens": self.max_tokens,
            "timeout_seconds": _canonical_float(self.timeout_seconds),
            "profile_id": self.profile_id,
            "credential_ref": self.credential_ref,
            "rubric": self.rubric.projection(),
        }

    @property
    def identity_hash(self) -> str:
        return canonical_json_hash(self.projection())

    @property
    def evaluator_version(self) -> str:
        """Evaluator version recorded with every metric scored by this judge."""
        return f"{JUDGE_VERSION_PREFIX}{self.identity_hash[:JUDGE_VERSION_HASH_LENGTH]}"


def freeze_judge_profile(
    profile: Any,
    *,
    provider: str,
    rubric: AnswerQualityRubric = DEFAULT_ANSWER_QUALITY_RUBRIC,
) -> FrozenJudgeProfile:
    """Freeze a resolved model profile into a judge identity.

    ``profile`` is any object exposing the ``ResolvedModelProfile`` shape, so the judge
    reuses the same resolution path as agent publication rather than a parallel one.
    """
    return FrozenJudgeProfile(
        provider=provider,
        model=str(profile.model),
        temperature=_canonical_float(profile.temperature),
        max_tokens=int(profile.max_tokens),
        timeout_seconds=_canonical_float(profile.timeout_seconds),
        profile_id=str(profile.id),
        credential_ref=str(profile.provider_credential_id),
        rubric=rubric,
    )


def is_judge_evaluator_version(value: Any) -> bool:
    if not isinstance(value, str) or not value.startswith(JUDGE_VERSION_PREFIX):
        return False
    suffix = value[len(JUDGE_VERSION_PREFIX) :]
    return len(suffix) == JUDGE_VERSION_HASH_LENGTH and all(
        character in "0123456789abcdef" for character in suffix
    )


class JudgeClient(Protocol):
    """Minimal transport seam so the judge is testable without a network call."""

    async def complete(self, *, system_prompt: str, user_prompt: str) -> str: ...


class ModelGatewayJudgeClient:
    """Judge transport backed by the existing model gateway.

    Credentials are never handled here: the gateway resolves the encrypted
    ``provider_credentials`` row for ``model_profile_id`` through the existing
    ``AGENTHUB_CREDENTIAL_MASTER_KEY`` mechanism.
    """

    def __init__(
        self,
        gateway: ModelGateway,
        context: WorkspaceExecutionContext,
        model_profile_id: UUID,
    ) -> None:
        self.gateway = gateway
        self.context = context
        self.model_profile_id = model_profile_id

    async def complete(self, *, system_prompt: str, user_prompt: str) -> str:
        response = await self.gateway.generate(
            self.context,
            self.model_profile_id,
            ModelRequest(
                messages=(
                    ModelMessage(role="system", content=system_prompt),
                    ModelMessage(role="user", content=user_prompt),
                ),
                required_capabilities=CapabilityRequirements(),
            ),
        )
        return response.content


@dataclass(frozen=True, slots=True)
class JudgeRequest:
    """Everything the judge is allowed to see about one case."""

    question: str
    answer: str
    reference_answer: str | None = None
    evidence: tuple[str, ...] = field(default_factory=tuple)


class AnswerQualityJudge:
    """Scores open-ended output against a frozen rubric with a frozen judge profile."""

    def __init__(self, profile: FrozenJudgeProfile, client: JudgeClient) -> None:
        self.profile = profile
        self.client = client

    @property
    def rubric(self) -> AnswerQualityRubric:
        return self.profile.rubric

    def system_prompt(self) -> str:
        dimensions = "\n".join(
            f"- {dimension.name} (weight {dimension.weight}): {dimension.guidance}"
            for dimension in self.rubric.dimensions
        )
        names = ", ".join(f'"{dimension.name}"' for dimension in self.rubric.dimensions)
        return (
            "You grade an assistant answer against a rubric. "
            f"Score every dimension with an integer from 0 to {_MAX_DIMENSION_SCORE}.\n"
            f"{dimensions}\n"
            "Reply with JSON only, no prose and no code fence, shaped as "
            f'{{"scores": {{{names}: <int>}}, "rationale": "<one sentence>"}}.'
        )

    def user_prompt(self, request: JudgeRequest) -> str:
        sections = [f"[REQUEST]\n{request.question}", f"[ANSWER]\n{request.answer}"]
        if request.reference_answer is not None:
            sections.append(f"[REFERENCE ANSWER]\n{request.reference_answer}")
        if request.evidence:
            joined = "\n".join(f"- {item}" for item in request.evidence)
            sections.append(f"[EVIDENCE]\n{joined}")
        return "\n\n".join(sections)

    async def judge(self, request: JudgeRequest) -> dict[str, Any]:
        """Return the verdict payload to embed under ``JUDGE_OBSERVATION_KEY``."""
        try:
            raw = await self.client.complete(
                system_prompt=self.system_prompt(),
                user_prompt=self.user_prompt(request),
            )
        except Exception:  # noqa: BLE001 - a judge outage must not fail the case
            return self._verdict(JudgeVerdictStatus.FAILED, reason="judge_call_failed")
        return self.verdict_from_raw(raw)

    def verdict_from_raw(self, raw: str) -> dict[str, Any]:
        parsed = _parse_json_object(raw)
        if parsed is None:
            return self._verdict(JudgeVerdictStatus.UNPARSEABLE, reason="judge_output_not_json")
        scores = parsed.get("scores")
        if not isinstance(scores, Mapping):
            return self._verdict(JudgeVerdictStatus.UNPARSEABLE, reason="judge_scores_missing")
        normalized: dict[str, int] = {}
        for dimension in self.rubric.dimensions:
            value = scores.get(dimension.name)
            if isinstance(value, bool) or not isinstance(value, int):
                return self._verdict(
                    JudgeVerdictStatus.UNPARSEABLE, reason="judge_score_not_integer"
                )
            if not 0 <= value <= _MAX_DIMENSION_SCORE:
                return self._verdict(
                    JudgeVerdictStatus.UNPARSEABLE, reason="judge_score_out_of_range"
                )
            normalized[dimension.name] = value
        rationale = parsed.get("rationale")
        return self._verdict(
            JudgeVerdictStatus.SCORED,
            dimension_scores=normalized,
            score=self.rubric.normalize(normalized),
            rationale_hash=canonical_json_hash(rationale if isinstance(rationale, str) else ""),
        )

    def _verdict(
        self,
        status: str,
        *,
        dimension_scores: Mapping[str, int] | None = None,
        score: float | None = None,
        rationale_hash: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "reason": reason,
            "score": score,
            "dimension_scores": dict(sorted((dimension_scores or {}).items())),
            "rationale_hash": rationale_hash,
            "rubric_id": self.rubric.rubric_id,
            "rubric_version": self.rubric.rubric_version,
            "judge_identity_hash": self.profile.identity_hash,
            "judge_evaluator_version": self.profile.evaluator_version,
        }


def _parse_json_object(raw: Any) -> Mapping[str, Any] | None:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _canonical_float(value: Any) -> float:
    numeric = float(value) if not isinstance(value, Decimal) else float(value)
    return 0.0 if numeric == 0 else numeric


def judge_manifest_entry(profile: FrozenJudgeProfile | None) -> str:
    return JUDGE_DISABLED_VERSION if profile is None else profile.evaluator_version


def judge_version_from_manifest(manifest: Mapping[str, Any] | None) -> str:
    """Read the frozen judge version out of a persisted evaluator manifest.

    A manifest written before the judge existed has no key; that is read as
    ``disabled`` so historical experiments keep validating unchanged.
    """
    if not isinstance(manifest, Mapping):
        return JUDGE_DISABLED_VERSION
    versions = manifest.get("evaluator_versions")
    if not isinstance(versions, Mapping):
        return JUDGE_DISABLED_VERSION
    value = versions.get(JUDGE_MANIFEST_KEY, JUDGE_DISABLED_VERSION)
    return value if is_judge_evaluator_version(value) else JUDGE_DISABLED_VERSION


def judge_profile_from_manifest(manifest: Mapping[str, Any] | None) -> FrozenJudgeProfile | None:
    """Rebuild the frozen judge an experiment was created with, or ``None`` if it has none.

    The rebuilt profile is re-projected and re-hashed, and the result must equal the
    evaluator version stored beside it.  A projection that no longer produces its own
    version is not the judge this experiment was frozen with -- scoring with it anyway
    would file two different judges' verdicts under one version string, which is
    precisely what freezing the judge exists to prevent.
    """

    version = judge_version_from_manifest(manifest)
    if version == JUDGE_DISABLED_VERSION:
        return None
    projection = manifest.get("judge_profile") if isinstance(manifest, Mapping) else None
    if not isinstance(projection, Mapping):
        raise _judge_manifest_invalid()
    rubric_projection = projection.get("rubric")
    if not isinstance(rubric_projection, Mapping):
        raise _judge_manifest_invalid()
    dimensions = rubric_projection.get("dimensions")
    if not isinstance(dimensions, Sequence) or isinstance(dimensions, (str, bytes)):
        raise _judge_manifest_invalid()
    try:
        rubric = AnswerQualityRubric(
            rubric_id=str(rubric_projection["rubric_id"]),
            rubric_version=int(rubric_projection["rubric_version"]),
            dimensions=tuple(
                RubricDimension(
                    str(item["name"]), int(item["weight"]), str(item["guidance"])
                )
                for item in dimensions
            ),
        )
        profile = FrozenJudgeProfile(
            provider=str(projection["provider"]),
            model=str(projection["model"]),
            temperature=_canonical_float(projection["temperature"]),
            max_tokens=int(projection["max_tokens"]),
            timeout_seconds=_canonical_float(projection["timeout_seconds"]),
            profile_id=str(projection["profile_id"]),
            credential_ref=str(projection["credential_ref"]),
            rubric=rubric,
        )
    except (KeyError, TypeError, ValueError):
        raise _judge_manifest_invalid() from None
    if profile.evaluator_version != version:
        raise _judge_manifest_invalid()
    return profile


def _judge_manifest_invalid() -> AgentHubError:
    return AgentHubError(
        "EVALUATION_JUDGE_MANIFEST_INVALID",
        "The frozen judge identity could not be read.",
        422,
    )


def sequence_of_text(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(str(item) for item in value)


__all__ = [
    "ANSWER_QUALITY_METRIC",
    "DEFAULT_ANSWER_QUALITY_RUBRIC",
    "JUDGE_DISABLED_VERSION",
    "JUDGE_MANIFEST_KEY",
    "JUDGE_OBSERVATION_KEY",
    "JUDGE_VERSION_PREFIX",
    "AnswerQualityJudge",
    "AnswerQualityRubric",
    "FrozenJudgeProfile",
    "JudgeClient",
    "JudgeRequest",
    "JudgeVerdictStatus",
    "ModelGatewayJudgeClient",
    "RubricDimension",
    "freeze_judge_profile",
    "is_judge_evaluator_version",
    "judge_manifest_entry",
    "judge_profile_from_manifest",
    "judge_version_from_manifest",
    "sequence_of_text",
]
