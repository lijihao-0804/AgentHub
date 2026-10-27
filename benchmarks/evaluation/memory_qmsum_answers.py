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
    QMSumDispatchPlan,
    QMSumObservationStatus,
    QMSumPlannedArm,
    QMSumProtocolError,
    load_frozen_qmsum,
    plan_offline,
    record_observations,
    validate_online_arm_sequence,
)
from benchmarks.evaluation.memory_qmsum_summary_runner import SummaryNotDispatched
from benchmarks.evaluation.memory_trial_budget import MemoryTrialBudget
from benchmarks.evaluation.memory_trial_client import MODEL, PRICE_IDENTITY, generate_once
from benchmarks.evaluation.memory_trial_policy import create_trial_policy
from packages.agent_runtime.context_budget import ApproxTokenEstimator

AnswerDispatch = Callable[[str, QMSumPlannedArm], Awaitable[Mapping[str, Any]]]
ANSWER_OUTPUT_LIMIT = 1024


@dataclass(frozen=True, slots=True)
class QMSumAnswerRun:
    records: tuple[QMSumArmRecord, ...]
    unattempted: tuple[tuple[str, QMSumArm], ...]
    stop_reason: str | None
    measurements: Mapping[tuple[str, QMSumArm], Mapping[str, Any]]


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
    for index, (question_id, arm) in enumerate(items):
        try:
            result = await dispatch(question_id, arm)
        except SummaryNotDispatched:
            return QMSumAnswerRun(
                tuple(records),
                tuple((qid, item.arm) for qid, item in items[index:]),
                "NOT_DISPATCHED",
                metadata,
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
                    status=QMSumObservationStatus.UNKNOWN_OUTCOME,
                )
            )
            return QMSumAnswerRun(
                tuple(records),
                tuple((qid, item.arm) for qid, item in items[index + 1 :]),
                "UNVERIFIED_RESPONSE",
                metadata,
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
    record_observations(plan, records)
    return QMSumAnswerRun(tuple(records), (), None, metadata)


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
    output = result["content"]
    status = (
        QMSumObservationStatus.OBSERVED
        if result.get("finish_reason") == "stop" and output.strip()
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
) -> QMSumAnswerRun:
    freeze_answer_inputs(plan, frozen_input_path)
    with MemoryTrialBudget(ledger_path, price_identity=PRICE_IDENTITY) as budget:

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
                if row["status"] != "SETTLED" or not path.is_file():
                    raise ValueError("UNRESOLVED_MEMORY_TRIAL_ATTEMPT")
                result = json.loads(path.read_text(encoding="utf-8"))
                verify_resumed_result(
                    result, row, arm.admitted, attempt_id, output_limit=ANSWER_OUTPUT_LIMIT
                )
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
                )
            except ValueError as error:
                raise SummaryNotDispatched from error

        return await execute_answer_plan(plan, dispatch=dispatch)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qmsum-root", type=Path, required=True)
    parser.add_argument("--annotations", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--summary-directory", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    source = load_frozen_qmsum(args.qmsum_root)
    policy = create_trial_policy()
    baseline = plan_offline(source, admission=policy, token_counter=ApproxTokenEstimator())
    annotations = json.loads(args.annotations.read_text(encoding="utf-8"))
    preparations = prepare_reviewed_summaries(source, baseline, annotations, policy=policy)
    endpoint, api_key = _configured_credential(args.env_file)
    summaries = asyncio.run(
        run_live_summaries(
            preparations,
            ledger_path=args.ledger,
            result_directory=args.summary_directory / "responses",
            frozen_input_path=args.summary_directory / "frozen-summary-inputs.json",
            endpoint=endpoint,
            api_key=api_key,
        )
    )
    if summaries.unattempted_question_ids or any(
        row.status not in {"VALID", "INVALID"} for row in summaries.observations
    ):
        print(json.dumps({"status": "SUMMARY_PHASE_UNRESOLVED"}))
        return 1
    candidates = {
        row.question_id: row.candidate
        for row in summaries.observations
        if row.candidate is not None
    }
    plan = plan_offline(
        source,
        admission=policy,
        token_counter=ApproxTokenEstimator(),
        compressed_candidates=candidates,
    )
    answer_run = asyncio.run(
        run_live_answers(
            plan,
            ledger_path=args.ledger,
            result_directory=args.output_directory / "responses",
            frozen_input_path=args.output_directory / "frozen-answer-inputs.json",
            endpoint=endpoint,
            api_key=api_key,
        )
    )
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
