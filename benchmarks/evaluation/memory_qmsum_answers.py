"""Frozen, single-attempt B/C answers for the QMSum M4 study."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from benchmarks.evaluation.memory_deepseek_v41_counter import DeepseekV41TextCounter
from benchmarks.evaluation.memory_qmsum_live import (
    _configured_credential,
    prepare_reviewed_summaries,
    run_live_summaries,
    verify_resumed_result,
)
from benchmarks.evaluation.memory_qmsum_protocol import (
    QMSumArm,
    QMSumArmRecord,
    QMSumCallMeasurements,
    QMSumCaseStatus,
    QMSumDispatchPlan,
    QMSumObservationStatus,
    QMSumPlannedArm,
    QMSumProtocolError,
    QMSumSourceBundle,
    load_frozen_qmsum,
    plan_offline,
    record_observations,
    summarize_score_records,
    validate_online_arm_sequence,
)
from benchmarks.evaluation.memory_qmsum_summary_runner import (
    QMSumSummaryRun,
    SummaryNotDispatched,
    SummaryResumeUnverified,
)
from benchmarks.evaluation.memory_trial_budget import MemoryTrialBudget
from benchmarks.evaluation.memory_trial_client import (
    MODEL,
    PRICE_IDENTITY,
    generate_once,
    trial_request_identity,
)
from benchmarks.evaluation.memory_trial_policy import create_trial_policy
from packages.agent_runtime.context_budget import ApproxTokenEstimator, ContextBudgetPolicy

AnswerDispatch = Callable[[str, QMSumPlannedArm], Awaitable[Mapping[str, Any]]]
ANSWER_OUTPUT_LIMIT = 1024


@dataclass(frozen=True, slots=True)
class QMSumAnswerTerminalCase:
    question_id: str
    status: QMSumCaseStatus
    reason: str

    def __post_init__(self) -> None:
        if self.status is not QMSumCaseStatus.C_SUMMARY_INVALID:
            raise QMSumProtocolError("terminal answer case must be an invalid completed summary")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise QMSumProtocolError("terminal answer case requires a reason")


@dataclass(frozen=True, slots=True)
class QMSumAnswerRun:
    records: tuple[QMSumArmRecord, ...]
    unattempted: tuple[tuple[str, QMSumArm], ...]
    stop_reason: str | None
    measurements: Mapping[tuple[str, QMSumArm], Mapping[str, Any]]
    terminal_cases: tuple[QMSumAnswerTerminalCase, ...] = ()

    def __post_init__(self) -> None:
        terminal_cases = tuple(self.terminal_cases)
        if any(not isinstance(item, QMSumAnswerTerminalCase) for item in terminal_cases):
            raise QMSumProtocolError("answer run contains an invalid terminal case")
        object.__setattr__(self, "terminal_cases", terminal_cases)


def freeze_answer_inputs(plan: QMSumDispatchPlan, path: Path) -> None:
    """Record the exact balanced inputs before any answer is dispatched."""
    sequence = tuple((case.question_id, arm.arm) for case in plan.cases for arm in case.arms)
    validate_online_arm_sequence(plan, sequence)
    payload = {
        "format": "qmsum-frozen-answer-inputs-v1",
        "model": MODEL,
        "price_identity": PRICE_IDENTITY,
        "max_output_tokens": ANSWER_OUTPUT_LIMIT,
        "cases": [
            {
                "question_id": case.question_id,
                "status": case.status.value,
                "blocked_reason": case.blocked_reason,
                "arms": [
                    {
                        "arm": arm.arm.value,
                        "order": arm.order,
                        "candidate_id": arm.compressed_candidate_id,
                        "estimated_input": arm.admitted.usage.estimated_input_after,
                        "available_input": arm.admitted.usage.available_input,
                        "messages": [
                            {"role": message.role, "content": message.content}
                            for message in arm.admitted.messages
                        ],
                    }
                    for arm in case.arms
                ],
            }
            for case in plan.cases
        ],
    }
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if path.read_text(encoding="utf-8") != serialized:
            raise QMSumProtocolError("frozen answer input changed after registration") from None


async def execute_answer_plan(
    plan: QMSumDispatchPlan, *, dispatch: AnswerDispatch
) -> QMSumAnswerRun:
    """Run only planned arms in order; stop after any unconfirmed provider call."""
    items = tuple((case.question_id, arm) for case in plan.cases for arm in case.arms)
    validate_online_arm_sequence(plan, tuple((question_id, arm.arm) for question_id, arm in items))
    records: list[QMSumArmRecord] = []
    metadata: dict[tuple[str, QMSumArm], Mapping[str, Any]] = {}
    terminal_cases = tuple(
        QMSumAnswerTerminalCase(case.question_id, case.status, case.blocked_reason or "")
        for case in plan.cases
        if case.status is QMSumCaseStatus.C_SUMMARY_INVALID
    )
    for index, (question_id, arm) in enumerate(items):
        try:
            result = await dispatch(question_id, arm)
        except SummaryResumeUnverified:
            records.append(
                QMSumArmRecord(
                    question_id,
                    arm.arm,
                    "",
                    QMSumCallMeasurements(None, None, None, None, None),
                    summary_candidate_id=arm.compressed_candidate_id,
                    status=QMSumObservationStatus.UNVERIFIED_RESPONSE,
                )
            )
            record_observations(plan, records)
            return QMSumAnswerRun(
                tuple(records),
                tuple((qid, item.arm) for qid, item in items[index + 1 :]),
                "RESUME_IDENTITY_UNVERIFIED",
                metadata,
                terminal_cases,
            )
        except SummaryNotDispatched:
            return QMSumAnswerRun(
                tuple(records),
                tuple((qid, item.arm) for qid, item in items[index:]),
                "NOT_DISPATCHED",
                metadata,
                terminal_cases,
            )
        except Exception:
            records.append(
                QMSumArmRecord(
                    question_id,
                    arm.arm,
                    "",
                    QMSumCallMeasurements(None, None, None, None, None),
                    summary_candidate_id=arm.compressed_candidate_id,
                    status=QMSumObservationStatus.UNKNOWN_OUTCOME,
                )
            )
            record_observations(plan, records)
            return QMSumAnswerRun(
                tuple(records),
                tuple((qid, item.arm) for qid, item in items[index + 1 :]),
                "UNKNOWN_OUTCOME",
                metadata,
                terminal_cases,
            )
        try:
            record = _observed_record(question_id, arm, result)
        except (
            KeyError,
            TypeError,
            ValueError,
            OverflowError,
            InvalidOperation,
            QMSumProtocolError,
        ):
            records.append(
                QMSumArmRecord(
                    question_id,
                    arm.arm,
                    "",
                    QMSumCallMeasurements(None, None, None, None, None),
                    summary_candidate_id=arm.compressed_candidate_id,
                    status=QMSumObservationStatus.UNVERIFIED_RESPONSE,
                )
            )
            record_observations(plan, records)
            return QMSumAnswerRun(
                tuple(records),
                tuple((qid, item.arm) for qid, item in items[index + 1 :]),
                "UNVERIFIED_RESPONSE",
                metadata,
                terminal_cases,
            )
        records.append(record)
        metadata[(question_id, arm.arm)] = {
            key: result.get(key)
            for key in (
                "attempt_id",
                "finish_reason",
                "input_tokens",
                "output_tokens",
                "cached_tokens",
                "cache_write_tokens",
                "elapsed_ms",
                "conservative_cost_cny",
                "cost_identity",
            )
        }
        counter = result.get("input_counter")
        if isinstance(counter, Mapping) and counter.get("tokens") != result.get("input_tokens"):
            record_observations(plan, records)
            return QMSumAnswerRun(
                tuple(records),
                tuple((qid, item.arm) for qid, item in items[index + 1 :]),
                "PROVIDER_COUNTER_MISMATCH",
                metadata,
                terminal_cases,
            )
    record_observations(plan, records)
    return QMSumAnswerRun(tuple(records), (), None, metadata, terminal_cases)


def _observed_record(
    question_id: str, arm: QMSumPlannedArm, result: Mapping[str, Any]
) -> QMSumArmRecord:
    if not isinstance(result, Mapping) or not isinstance(result.get("content"), str):
        raise ValueError("answer content is unavailable")
    if result.get("model") != MODEL or result.get("cost_identity") != PRICE_IDENTITY:
        raise ValueError("answer model or pricing is unverified")
    input_tokens, output_tokens = result.get("input_tokens"), result.get("output_tokens")
    elapsed = result.get("elapsed_ms")
    if (
        type(input_tokens) is not int
        or not 0 <= input_tokens <= 32768
        or type(output_tokens) is not int
        or not 0 <= output_tokens <= ANSWER_OUTPUT_LIMIT
        or type(elapsed) not in (int, float)
        or elapsed < 0
    ):
        raise ValueError("answer usage is unverified")
    amount = Decimal(result["conservative_cost_cny"])
    expected = (Decimal(input_tokens) * 2 + Decimal(output_tokens) * 8) / Decimal(1_000_000)
    if not amount.is_finite() or amount != expected:
        raise ValueError("answer conservative price is unverified")
    counter = result.get("input_counter")
    if counter is not None and (
        not isinstance(counter, Mapping) or type(counter.get("tokens")) is not int
    ):
        raise ValueError("answer counter is unverified")
    counter_matches = counter is None or counter["tokens"] == input_tokens
    output = result["content"]
    status = (
        QMSumObservationStatus.OBSERVED
        if result.get("finish_reason") == "stop" and output.strip() and counter_matches
        else QMSumObservationStatus.FAILED
    )
    return QMSumArmRecord(
        question_id,
        arm.arm,
        output,
        QMSumCallMeasurements(input_tokens, output_tokens, round(elapsed), amount, PRICE_IDENTITY),
        summary_candidate_id=arm.compressed_candidate_id,
        status=status,
    )


async def run_live_answers(
    plan: QMSumDispatchPlan,
    *,
    ledger_path: Path,
    result_directory: Path,
    frozen_input_path: Path,
    endpoint: str,
    api_key: str,
    provider_counter: DeepseekV41TextCounter | None = None,
) -> QMSumAnswerRun:
    with MemoryTrialBudget(ledger_path, price_identity=PRICE_IDENTITY) as budget:
        if not frozen_input_path.is_file() and any(
            row.get("stage") == "M4" and row.get("attempt_id", "").startswith("m4-answer-")
            for row in budget.state["attempts"]
        ):
            raise QMSumProtocolError("frozen answer input is missing for an existing trial")
        freeze_answer_inputs(plan, frozen_input_path)

        async def dispatch(question_id: str, arm: QMSumPlannedArm) -> Mapping[str, Any]:
            question = question_id.split("/train/", 1)[1]
            meeting, query = question.split(".json#specific_query_list[", 1)
            attempt_id = f"m4-answer-{meeting}-{query.rstrip(']')}-{arm.arm.value}"
            path = result_directory / f"{attempt_id}.json"
            matching = [row for row in budget.state["attempts"] if row["attempt_id"] == attempt_id]
            if len(matching) > 1:
                raise ValueError("DUPLICATE_MEMORY_TRIAL_ATTEMPT")
            if matching:
                row = matching[0]
                if row["status"] != "SETTLED":
                    raise ValueError("UNRESOLVED_MEMORY_TRIAL_ATTEMPT")
                try:
                    result = json.loads(path.read_text(encoding="utf-8"))
                    verify_resumed_result(
                        result,
                        row,
                        arm.admitted,
                        attempt_id,
                        output_limit=ANSWER_OUTPUT_LIMIT,
                        expected_request_identity=trial_request_identity(
                            endpoint=endpoint,
                            messages=arm.admitted.messages,
                            output_limit=ANSWER_OUTPUT_LIMIT,
                        ),
                    )
                except (OSError, UnicodeError, ValueError, TypeError, KeyError) as error:
                    raise SummaryResumeUnverified from error
                return result
            if path.exists():
                raise ValueError("MEMORY_TRIAL_RESULT_WITHOUT_LEDGER")
            try:
                return await generate_once(
                    budget=budget,
                    admission=arm.admitted,
                    original_messages=arm.admitted.messages,
                    attempt_id=attempt_id,
                    stage="M4",
                    endpoint=endpoint,
                    api_key=api_key,
                    output_limit=ANSWER_OUTPUT_LIMIT,
                    output_directory=result_directory,
                    provider_counter=provider_counter,
                    provider_input_limit=(85 * arm.admitted.usage.available_input - 1) // 100,
                )
            except ValueError as error:
                raise SummaryNotDispatched from error

        return await execute_answer_plan(plan, dispatch=dispatch)


def plan_answer_phase(
    source: QMSumSourceBundle,
    summaries: QMSumSummaryRun,
    *,
    policy: ContextBudgetPolicy,
    expected_summary_ids: tuple[str, ...] | None = None,
) -> QMSumDispatchPlan:
    """Build the CLI answer plan from resolved summary observations."""
    if not isinstance(source, QMSumSourceBundle):
        raise TypeError("source must be a QMSum source bundle")
    if not isinstance(summaries, QMSumSummaryRun):
        raise TypeError("summaries must be a QMSum summary run")
    if summaries.unattempted_question_ids:
        raise QMSumProtocolError("answer planning requires every summary attempt to be resolved")
    if expected_summary_ids is not None:
        if (
            not isinstance(expected_summary_ids, tuple)
            or any(not isinstance(value, str) or not value for value in expected_summary_ids)
            or len(set(expected_summary_ids)) != len(expected_summary_ids)
            or set(expected_summary_ids) != {row.question_id for row in summaries.observations}
        ):
            raise QMSumProtocolError("summary observations do not match the frozen preparations")

    candidates = {}
    invalid_reasons = {}
    for observation in summaries.observations:
        if observation.status == "VALID":
            if observation.candidate is None:
                raise QMSumProtocolError("valid summary observation has no candidate")
            candidates[observation.question_id] = observation.candidate
        elif observation.status == "INVALID":
            invalid_reasons[observation.question_id] = (
                ",".join(observation.issues) if observation.issues else "SUMMARY_INVALID"
            )
        else:
            raise QMSumProtocolError(
                "answer planning requires VALID or INVALID summary observations"
            )

    return plan_offline(
        source,
        admission=policy,
        token_counter=ApproxTokenEstimator(),
        compressed_candidates=candidates,
        summary_invalid_reasons=invalid_reasons,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qmsum-root", type=Path, required=True)
    parser.add_argument("--annotations", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--summary-directory", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--tokenizer", type=Path)
    args = parser.parse_args()
    source = load_frozen_qmsum(args.qmsum_root)
    policy = create_trial_policy()
    baseline = plan_offline(source, admission=policy, token_counter=ApproxTokenEstimator())
    annotations = json.loads(args.annotations.read_text(encoding="utf-8"))
    preparations = prepare_reviewed_summaries(source, baseline, annotations, policy=policy)
    endpoint, api_key = _configured_credential(args.env_file)
    provider_counter = DeepseekV41TextCounter.from_file(args.tokenizer) if args.tokenizer else None
    summaries = asyncio.run(
        run_live_summaries(
            preparations,
            ledger_path=args.ledger,
            result_directory=args.summary_directory / "responses",
            frozen_input_path=args.summary_directory / "frozen-summary-inputs.json",
            endpoint=endpoint,
            api_key=api_key,
            provider_counter=provider_counter,
        )
    )
    if summaries.unattempted_question_ids or any(
        row.status not in {"VALID", "INVALID"} for row in summaries.observations
    ):
        print(json.dumps({"status": "SUMMARY_PHASE_UNRESOLVED"}))
        return 1
    plan = plan_answer_phase(
        source,
        summaries,
        policy=policy,
        expected_summary_ids=tuple(item.question_id for item in preparations),
    )
    answer_run = asyncio.run(
        run_live_answers(
            plan,
            ledger_path=args.ledger,
            result_directory=args.output_directory / "responses",
            frozen_input_path=args.output_directory / "frozen-answer-inputs.json",
            endpoint=endpoint,
            api_key=api_key,
            provider_counter=provider_counter,
        )
    )
    score = summarize_score_records(plan, answer_run.records)
    report = {
        "source_commit": source.commit,
        "frozen_question_count": len(plan.cases),
        "summary_statuses": [
            {"question_id": row.question_id, "status": row.status, "issues": row.issues}
            for row in summaries.observations
        ],
        "cases": [
            {
                "question_id": case.question_id,
                "status": case.status.value,
                "blocked_reason": case.blocked_reason,
                "planned_arms": [arm.arm.value for arm in case.arms],
            }
            for case in plan.cases
        ],
        "answers": [
            {
                "question_id": row.question_id,
                "arm": row.arm.value,
                "status": row.status.value,
                "measurements": dict(answer_run.measurements.get((row.question_id, row.arm), {})),
                "review": "UNREVIEWED",
            }
            for row in answer_run.records
        ],
        "unattempted": [
            {"question_id": qid, "arm": arm.value} for qid, arm in answer_run.unattempted
        ],
        "stop_reason": answer_run.stop_reason,
        "pair_accounting": {
            "invalid_summary_questions": score.invalid_summary_questions,
            "planned_pairs": score.planned_pairs,
            "complete_observed_pairs": score.complete_observed_pairs,
            "partial_pairs": score.partial_pairs,
            "not_started_pairs": score.not_started_pairs,
            "failed_pairs": score.failed_pairs,
            "unknown_pairs": score.unknown_pairs,
            "unverified_pairs": score.unverified_pairs,
            "failure_categories_may_overlap": True,
            "semantic_accuracy": score.semantic_accuracy,
        },
    }
    args.output_directory.mkdir(parents=True, exist_ok=True)
    destination = args.output_directory / "answer-observations.json"
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, destination)
    print(json.dumps(report, ensure_ascii=False))
    return int(answer_run.stop_reason is not None)


if __name__ == "__main__":
    raise SystemExit(main())
