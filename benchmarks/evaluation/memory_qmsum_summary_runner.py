"""Serial, single-attempt coordinator for frozen QMSum summary requests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from benchmarks.evaluation.memory_qmsum_protocol import QMSumCompressedCandidate, QMSumProtocolError
from benchmarks.evaluation.memory_qmsum_summary import (
    QMSumSummaryPreparation,
    validate_research_summary,
)

SummaryDispatch = Callable[[QMSumSummaryPreparation], Awaitable[Mapping[str, Any]]]
_STATUSES = frozenset(
    {
        "VALID",
        "INVALID",
        "UNKNOWN_OUTCOME",
        "NOT_DISPATCHED",
        "UNVERIFIED_RESPONSE",
        "LOCAL_VALIDATION_ERROR",
    }
)
_MEASUREMENT_KEYS = frozenset(
    {
        "attempt_id",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "cache_write_tokens",
        "elapsed_ms",
        "conservative_cost_cny",
        "cost_identity",
        "finish_reason",
    }
)


class SummaryNotDispatched(Exception):
    """The dispatcher proved this attempt failed before the provider boundary."""


class SummaryResumeUnverified(Exception):
    """A prior response exists but cannot be bound to the frozen request."""


@dataclass(frozen=True, slots=True)
class QMSumSummaryObservation:
    question_id: str
    status: str
    measurements: Mapping[str, Any]
    issues: tuple[str, ...] = ()
    candidate: QMSumCompressedCandidate | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.question_id, str) or not self.question_id:
            raise QMSumProtocolError("summary observation requires a question identity")
        if self.status not in _STATUSES:
            raise QMSumProtocolError("summary observation status is invalid")
        if (
            not isinstance(self.measurements, Mapping)
            or not set(self.measurements) <= _MEASUREMENT_KEYS
        ):
            raise QMSumProtocolError("summary measurements contain unsupported fields")
        issues = tuple(self.issues)
        if any(not isinstance(issue, str) or not issue for issue in issues):
            raise QMSumProtocolError("summary issue codes must be non-empty strings")
        if (self.status == "VALID") != (self.candidate is not None):
            raise QMSumProtocolError("summary status and candidate disagree")
        if self.candidate is not None and not isinstance(self.candidate, QMSumCompressedCandidate):
            raise QMSumProtocolError("summary candidate is invalid")
        if self.candidate is not None and self.candidate.source_question_ids != (self.question_id,):
            raise QMSumProtocolError("summary candidate is bound to a different question")
        object.__setattr__(self, "measurements", MappingProxyType(dict(self.measurements)))
        object.__setattr__(self, "issues", issues)


@dataclass(frozen=True, slots=True)
class QMSumSummaryRun:
    observations: tuple[QMSumSummaryObservation, ...]
    unattempted_question_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        observations = tuple(self.observations)
        unattempted = tuple(self.unattempted_question_ids)
        if any(not isinstance(item, QMSumSummaryObservation) for item in observations):
            raise QMSumProtocolError("summary run contains an invalid observation")
        identifiers = [item.question_id for item in observations]
        if (
            len(set(identifiers)) != len(identifiers)
            or len(set(unattempted)) != len(unattempted)
            or set(identifiers).intersection(unattempted)
        ):
            raise QMSumProtocolError("summary run cannot duplicate a question")
        object.__setattr__(self, "observations", observations)
        object.__setattr__(self, "unattempted_question_ids", unattempted)


async def run_summary_candidates(
    preparations: Sequence[QMSumSummaryPreparation], *, dispatch: SummaryDispatch
) -> QMSumSummaryRun:
    """Never repeat an attempt; any uncertain call stops later dispatches.

    The caller supplies a durable budgeted dispatcher that rejects repeated
    attempt identities. This coordinator does not catch cancellation or log
    raw meeting text, model output, credentials, or exception messages.
    """
    items = tuple(preparations)
    if any(not isinstance(item, QMSumSummaryPreparation) for item in items):
        raise TypeError("preparations must be frozen QMSum summary requests")
    if len({item.question_id for item in items}) != len(items):
        raise QMSumProtocolError("one summary preparation per question is required")
    if not callable(dispatch):
        raise TypeError("dispatch must be callable")
    observations: list[QMSumSummaryObservation] = []
    for position, preparation in enumerate(items):
        try:
            result = await dispatch(preparation)
        except SummaryResumeUnverified:
            observations.append(
                QMSumSummaryObservation(
                    preparation.question_id, "UNVERIFIED_RESPONSE", {},
                    ("RESUME_IDENTITY_UNVERIFIED",),
                )
            )
            return QMSumSummaryRun(
                tuple(observations), tuple(item.question_id for item in items[position + 1 :])
            )
        except SummaryNotDispatched:
            observations.append(
                QMSumSummaryObservation(preparation.question_id, "NOT_DISPATCHED", {})
            )
            return QMSumSummaryRun(
                tuple(observations), tuple(item.question_id for item in items[position + 1 :])
            )
        except Exception:
            observations.append(
                QMSumSummaryObservation(preparation.question_id, "UNKNOWN_OUTCOME", {})
            )
            return QMSumSummaryRun(
                tuple(observations), tuple(item.question_id for item in items[position + 1 :])
            )
        if (
            not isinstance(result, Mapping)
            or not isinstance(result.get("content"), str)
            or not isinstance(result.get("attempt_id"), str)
        ):
            observations.append(
                QMSumSummaryObservation(preparation.question_id, "UNVERIFIED_RESPONSE", {})
            )
            return QMSumSummaryRun(
                tuple(observations), tuple(item.question_id for item in items[position + 1 :])
            )
        measurements = {
            key: result.get(key)
            for key in (
                "attempt_id",
                "input_tokens",
                "output_tokens",
                "cached_tokens",
                "cache_write_tokens",
                "elapsed_ms",
                "conservative_cost_cny",
                "cost_identity",
                "finish_reason",
            )
        }
        input_tokens = measurements["input_tokens"]
        if type(input_tokens) is not int or input_tokens < 0:
            observations.append(
                QMSumSummaryObservation(
                    preparation.question_id, "UNVERIFIED_RESPONSE", measurements,
                    ("PROVIDER_INPUT_USAGE_UNAVAILABLE",),
                )
            )
            return QMSumSummaryRun(
                tuple(observations), tuple(item.question_id for item in items[position + 1 :])
            )
        counter = result.get("input_counter")
        if counter is not None and (
            not isinstance(counter, Mapping) or type(counter.get("tokens")) is not int
        ):
            observations.append(
                QMSumSummaryObservation(
                    preparation.question_id, "UNVERIFIED_RESPONSE", measurements,
                    ("PROVIDER_COUNTER_UNVERIFIED",),
                )
            )
            return QMSumSummaryRun(
                tuple(observations), tuple(item.question_id for item in items[position + 1 :])
            )
        issues = ()
        if input_tokens > preparation.admitted.usage.available_input:
            issues += ("PROVIDER_INPUT_EXCEEDED_BUDGET",)
        if counter is not None and counter["tokens"] != input_tokens:
            issues += ("PROVIDER_COUNTER_MISMATCH",)
        if issues:
            if measurements["finish_reason"] != "stop":
                issues += ("PROVIDER_RESPONSE_INCOMPLETE",)
            observations.append(
                QMSumSummaryObservation(preparation.question_id, "INVALID", measurements, issues)
            )
            if "PROVIDER_COUNTER_MISMATCH" in issues:
                return QMSumSummaryRun(
                    tuple(observations), tuple(item.question_id for item in items[position + 1 :])
                )
            continue
        try:
            validation, candidate = validate_research_summary(
                preparation,
                content=result["content"],
                finish_reason=result.get("finish_reason"),
            )
        except Exception:
            observations.append(
                QMSumSummaryObservation(
                    preparation.question_id, "LOCAL_VALIDATION_ERROR", measurements
                )
            )
            return QMSumSummaryRun(
                tuple(observations), tuple(item.question_id for item in items[position + 1 :])
            )
        issues = (
            tuple(issue.code.value for issue in validation.issues)
            if validation is not None
            else ("PROVIDER_RESPONSE_INCOMPLETE",)
        )
        if candidate is None and not issues:
            issues = ("NO_TOKEN_REDUCTION",)
        observations.append(
            QMSumSummaryObservation(
                preparation.question_id,
                "VALID" if candidate is not None else "INVALID",
                measurements,
                issues,
                candidate,
            )
        )
    return QMSumSummaryRun(tuple(observations), ())
