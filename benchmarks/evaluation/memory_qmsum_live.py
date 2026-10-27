"""Bounded, resumable live-summary portion of the pre-registered M4 study.

This module does not run on import. It only accepts the already configured
Beijing route and the durable single-owner trial ledger. Source annotations
contain turn ordinals rather than gold answers or model-generated judgments.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from benchmarks.evaluation.memory_deepseek_v41_counter import DeepseekV41TextCounter
from benchmarks.evaluation.memory_qmsum_protocol import (
    QMSumCaseStatus,
    QMSumDispatchPlan,
    QMSumProtocolError,
    QMSumSourceBundle,
    load_frozen_qmsum,
    plan_offline,
)
from benchmarks.evaluation.memory_qmsum_summary import QMSumSummaryPreparation, prepare_summary
from benchmarks.evaluation.memory_qmsum_summary_runner import (
    QMSumSummaryRun,
    SummaryNotDispatched,
    run_summary_candidates,
)
from benchmarks.evaluation.memory_trial_budget import MemoryTrialBudget
from benchmarks.evaluation.memory_trial_client import MODEL, PRICE_IDENTITY, generate_once
from benchmarks.evaluation.memory_trial_policy import create_trial_policy
from packages.agent_runtime.context_budget import (
    ApproxTokenEstimator,
    ContextAdmissionResult,
    ContextBudgetPolicy,
)


def prepare_reviewed_summaries(
    source: QMSumSourceBundle,
    plan: QMSumDispatchPlan,
    annotations: Mapping[str, Any],
    *,
    policy: ContextBudgetPolicy,
) -> tuple[QMSumSummaryPreparation, ...]:
    """Resolve pre-reviewed complete turns without opening reference answers."""
    if (
        set(annotations) != {"format", "cases"}
        or annotations.get("format") != "qmsum-reviewed-turns-v1"
    ):
        raise QMSumProtocolError("reviewed-turn annotation format is invalid")
    cases = annotations["cases"]
    if not isinstance(cases, list):
        raise QMSumProtocolError("reviewed-turn cases must be a list")
    expected = tuple(
        case.question_id
        for case in plan.cases
        if case.status is QMSumCaseStatus.B_READY_C_NOT_APPLICABLE
    )
    if len(cases) != len(expected):
        raise QMSumProtocolError("reviewed-turn cases must preserve all admissible questions")
    preparations = []
    for expected_id, annotation in zip(expected, cases, strict=True):
        if not isinstance(annotation, dict) or set(annotation) != {"question_id", "turns"}:
            raise QMSumProtocolError("reviewed-turn case fields are invalid")
        if annotation["question_id"] != expected_id:
            raise QMSumProtocolError("reviewed-turn order differs from the frozen plan")
        turns = annotation["turns"]
        if (
            not isinstance(turns, list)
            or not 1 <= len(turns) <= 20
            or any(type(ordinal) is not int or ordinal < 0 for ordinal in turns)
            or turns != sorted(set(turns))
        ):
            raise QMSumProtocolError("reviewed turn ordinals must be unique and ordered")
        question = next(item for item in source.questions if item.question_id == expected_id)
        if any(ordinal >= len(question.transcript) for ordinal in turns):
            raise QMSumProtocolError("a reviewed turn is outside the frozen transcript")
        excerpts = tuple(
            f"[{ordinal}] {question.transcript[ordinal].speaker}: "
            f"{question.transcript[ordinal].content}"
            for ordinal in turns
        )
        preparations.append(
            prepare_summary(
                source=source,
                plan=plan,
                question_id=expected_id,
                protected_excerpts=excerpts,
                policy=policy,
            )
        )
    return tuple(preparations)


def freeze_summary_inputs(preparations: tuple[QMSumSummaryPreparation, ...], path: Path) -> None:
    """Freeze exact input bytes before dispatch; never overwrite a prior freeze."""
    payload = {
        "format": "qmsum-frozen-summary-inputs-v1",
        "model": MODEL,
        "price_identity": PRICE_IDENTITY,
        "max_output_tokens": 2048,
        "cases": [
            {
                "question_id": item.question_id,
                "available_input": item.admitted.usage.available_input,
                "estimated_input": item.admitted.usage.estimated_input_after,
                "messages": [
                    {"role": message.role, "content": message.content} for message in item.messages
                ],
            }
            for item in preparations
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
            raise QMSumProtocolError("frozen summary input changed after registration") from None


async def run_live_summaries(
    preparations: tuple[QMSumSummaryPreparation, ...],
    *,
    ledger_path: Path,
    result_directory: Path,
    frozen_input_path: Path,
    endpoint: str,
    api_key: str,
    provider_counter: DeepseekV41TextCounter | None = None,
) -> QMSumSummaryRun:
    """Run or safely reconstruct confirmed calls; never retry an unknown call."""
    freeze_summary_inputs(preparations, frozen_input_path)
    with MemoryTrialBudget(ledger_path, price_identity=PRICE_IDENTITY) as budget:

        async def dispatch(preparation: QMSumSummaryPreparation) -> Mapping[str, Any]:
            question = preparation.question_id.split("/train/", 1)[1]
            meeting, query = question.split(".json#specific_query_list[", 1)
            attempt_id = f"m4-summary-{meeting}-{query.rstrip(']')}"
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
                    result, row, preparation.admitted, attempt_id, output_limit=2048
                )
                return result
            if path.exists():
                raise ValueError("MEMORY_TRIAL_RESULT_WITHOUT_LEDGER")
            try:
                return await generate_once(
                    budget=budget,
                    admission=preparation.admitted,
                    original_messages=preparation.messages,
                    attempt_id=attempt_id,
                    stage="M4",
                    endpoint=endpoint,
                    api_key=api_key,
                    output_limit=2048,
                    output_directory=result_directory,
                    provider_counter=provider_counter,
                )
            except ValueError as error:
                raise SummaryNotDispatched from error

        return await run_summary_candidates(preparations, dispatch=dispatch)


def verify_resumed_result(
    result: Any,
    row: Mapping[str, Any],
    admission: ContextAdmissionResult,
    attempt_id: str,
    *,
    output_limit: int,
) -> None:
    if (
        not isinstance(admission, ContextAdmissionResult)
        or type(output_limit) is not int
        or not 0 < output_limit <= 2048
    ):
        raise ValueError("INVALID_MEMORY_TRIAL_RESUME_BOUND")
    try:
        recorded_cost = Decimal(result["conservative_cost_cny"])
        expected_cost = (
            Decimal(result["input_tokens"]) * 2 + Decimal(result["output_tokens"]) * 8
        ) / Decimal(1_000_000)
        valid_cost = (
            recorded_cost.is_finite()
            and recorded_cost == Decimal(row["charged_or_reserved_cny"])
            and recorded_cost == expected_cost
        )
    except (KeyError, TypeError, InvalidOperation, ValueError):
        valid_cost = False
    if (
        not isinstance(result, dict)
        or result.get("attempt_id") != attempt_id
        or result.get("stage") != "M4"
        or result.get("model") != MODEL
        or result.get("cost_identity") != PRICE_IDENTITY
        or not isinstance(result.get("content"), str)
        or type(result.get("input_tokens")) is not int
        or not 0 <= result["input_tokens"] <= 32768
        or type(result.get("output_tokens")) is not int
        or not 0 <= result["output_tokens"] <= output_limit
        or row.get("stage") != "M4"
        or row.get("estimated_input_tokens")
        not in {admission.usage.estimated_input_after, 32768}
        or row.get("requested_output_tokens") != output_limit
        or row.get("input_counter") != result.get("input_counter")
        or not valid_cost
    ):
        raise ValueError("MEMORY_TRIAL_RESULT_MISMATCH")


def _configured_credential(env_file: Path | None) -> tuple[str, str]:
    values: dict[str, str] = {}
    names = {"AGENTHUB_RAG_BASE_URL", "AGENTHUB_RAG_API_KEY"}
    if env_file is not None:
        for line in env_file.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                if key.strip() in names:
                    values[key.strip()] = value.strip().strip("\"'")
    for name in names:
        values[name] = os.environ.get(name) or values.get(name, "")
    if not all(values.values()):
        raise ValueError("MEMORY_TRIAL_CREDENTIAL_UNAVAILABLE")
    return values["AGENTHUB_RAG_BASE_URL"], values["AGENTHUB_RAG_API_KEY"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qmsum-root", type=Path, required=True)
    parser.add_argument("--annotations", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--tokenizer", type=Path)
    args = parser.parse_args()
    source = load_frozen_qmsum(args.qmsum_root)
    policy = create_trial_policy()
    plan = plan_offline(source, admission=policy, token_counter=ApproxTokenEstimator())
    annotations = json.loads(args.annotations.read_text(encoding="utf-8"))
    preparations = prepare_reviewed_summaries(source, plan, annotations, policy=policy)
    endpoint, api_key = _configured_credential(args.env_file)
    result = asyncio.run(
        run_live_summaries(
            preparations,
            ledger_path=args.ledger,
            result_directory=args.output_directory / "responses",
            frozen_input_path=args.output_directory / "frozen-summary-inputs.json",
            endpoint=endpoint,
            api_key=api_key,
            provider_counter=(
                DeepseekV41TextCounter.from_file(args.tokenizer) if args.tokenizer else None
            ),
        )
    )
    report = {
        "source_commit": source.commit,
        "frozen_question_count": len(plan.cases),
        "observations": [
            {
                "question_id": item.question_id,
                "status": item.status,
                "issues": item.issues,
                "measurements": dict(item.measurements),
                "candidate_id": item.candidate.candidate_id if item.candidate else None,
            }
            for item in result.observations
        ],
        "unattempted_question_ids": result.unattempted_question_ids,
    }
    args.output_directory.mkdir(parents=True, exist_ok=True)
    destination = args.output_directory / "summary-observations.json"
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, destination)
    print(json.dumps(report, ensure_ascii=False))
    return int(
        bool(result.unattempted_question_ids)
        or any(item.status not in {"VALID", "INVALID"} for item in result.observations)
    )


if __name__ == "__main__":
    raise SystemExit(main())
