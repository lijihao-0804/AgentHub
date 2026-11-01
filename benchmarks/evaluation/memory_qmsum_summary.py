"""Prepare the single pre-registered QMSum compression candidate.

These are research-only document groups, not production Thread authorization.
The caller supplies a frozen, reviewed set of exact excerpts before any model
outputs are observed.  Mechanical validation cannot establish semantic
completeness of that set or authorize a live Thread source.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from benchmarks.evaluation.memory_qmsum_protocol import (
    QMSumArm,
    QMSumCaseStatus,
    QMSumCompressedCandidate,
    QMSumDispatchPlan,
    QMSumProtocolError,
    QMSumSourceBundle,
    QMSumSourceProvenance,
    build_online_messages,
    online_projection,
)
from packages.agent_runtime.context_budget import (
    ApproxTokenEstimator,
    ContextAdmissionResult,
    ContextBudgetPolicy,
    ContextCategory,
    ContextMessage,
)
from packages.agent_runtime.summary_prompt import (
    SummaryInputContext,
    SummaryPromptSource,
    SummaryProtectedFact,
    build_summary_prompt,
)
from packages.agent_runtime.summary_validation import (
    SummaryValidationResult,
    validate_summary_candidate,
)
from packages.model_gateway.contracts import ModelMessage

SUMMARY_TARGET_TOKENS = 2048
SUMMARY_FINAL_VIEW_TOKENS = 4096


class _Counter:
    def count(self, text: str) -> int:
        return ApproxTokenEstimator().estimate(text)


@dataclass(frozen=True, slots=True)
class QMSumSummaryPreparation:
    question_id: str
    context: SummaryInputContext
    messages: tuple[ModelMessage, ...]
    admitted: ContextAdmissionResult
    baseline_source_tokens: int


def prepare_summary(
    *,
    source: QMSumSourceBundle,
    plan: QMSumDispatchPlan,
    question_id: str,
    protected_excerpts: tuple[str, ...],
    policy: ContextBudgetPolicy,
) -> QMSumSummaryPreparation:
    """Admit one complete research transcript and reviewed excerpt set."""
    if source.provenance is not QMSumSourceProvenance.VERIFIED_FROZEN_QMSUM:
        raise QMSumProtocolError("summary preparation requires frozen source provenance")
    question = next((item for item in source.questions if item.question_id == question_id), None)
    case = next((item for item in plan.cases if item.question_id == question_id), None)
    if question is None or question.domain == "Committee" or case is None:
        raise QMSumProtocolError("question is not in the authorized online subset")
    if case.status is not QMSumCaseStatus.B_READY_C_NOT_APPLICABLE or len(case.arms) != 1:
        raise QMSumProtocolError("summary preparation requires a complete, uncompressed B plan")
    baseline = case.arms[0]
    if baseline.arm is not QMSumArm.B:
        raise QMSumProtocolError("summary preparation requires a B baseline")
    expected_baseline = tuple(
        item.message for item in build_online_messages(online_projection(question), arm=QMSumArm.B)
    )
    if baseline.admitted.messages != expected_baseline or baseline.admitted.usage.truncated:
        raise QMSumProtocolError("baseline does not match the complete frozen transcript")
    usage = baseline.admitted.usage
    if (
        usage.context_limit != 32768
        or usage.context_policy_version != "adaptive_context_v1"
        or usage.available_input != 29184
    ):
        raise QMSumProtocolError("baseline does not use the frozen trial budget")
    if not (
        70 * usage.available_input <= 100 * usage.estimated_input_after < 85 * usage.available_input
    ):
        raise QMSumProtocolError("summary preparation requires the frozen estimated pressure band")
    if not isinstance(protected_excerpts, tuple) or not 1 <= len(protected_excerpts) <= 20:
        raise QMSumProtocolError("protected excerpts must be a bounded frozen tuple")
    if any(
        not isinstance(item, str) or not item.strip() or item != item.strip()
        for item in protected_excerpts
    ):
        raise QMSumProtocolError("protected excerpts must be non-empty exact text")
    if len(set(protected_excerpts)) != len(protected_excerpts):
        raise QMSumProtocolError("protected excerpts must not be repeated")

    transcript_lines = tuple(
        f"[{turn.ordinal}] {turn.speaker}: {turn.content}" for turn in question.transcript
    )
    transcript = "\n".join(transcript_lines)
    if any(excerpt not in transcript_lines for excerpt in protected_excerpts):
        raise QMSumProtocolError("a protected turn is absent from the frozen transcript")
    estimator = ApproxTokenEstimator()
    source_tokens = estimator.estimate(transcript)
    wire_transcript = json.dumps(
        [[turn.speaker, turn.content] for turn in question.transcript],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    baseline_source_tokens = estimator.estimate(wire_transcript)
    if source_tokens <= SUMMARY_TARGET_TOKENS:
        raise QMSumProtocolError("source is too small for the frozen summary target")
    source_id = f"qmsum-{question.meeting_id}-{question.query_index}"
    workspace = "qmsum-research-only"
    thread = f"qmsum-meeting-{question.meeting_id}"
    source_record = SummaryPromptSource(
        source_id=source_id,
        workspace_id=workspace,
        thread_id=thread,
        identity=question.question_id,
        revision=question.meeting_sha256,
        sequence=1,
        watermark=2,
        role="MEETING_TRANSCRIPT",
        speaker="MULTIPLE_DOCUMENT_AUTHORS",
        occurred_at="UNKNOWN",
        content=transcript,
        original_ref=question.question_id,
        summary_eligible=True,
        exchange_complete=True,
    )
    facts = tuple(
        SummaryProtectedFact(fact_ref=f"reviewed-{index}", text=text, source_ids=(source_id,))
        for index, text in enumerate(protected_excerpts, start=1)
    )
    # The empty action/reference inventories describe this research envelope,
    # not an assertion that every natural-language fact has been identified.
    context = SummaryInputContext(
        workspace_id=workspace,
        thread_id=thread,
        permission_snapshot="public-qmsum-research-no-business-authority",
        task_goal=question.question,
        prefix_first_sequence=1,
        prefix_last_sequence=1,
        watermark=2,
        sources=(source_record,),
        protected_facts=facts,
        completed_actions=(),
        required_references=(),
        source_budget_tokens=source_tokens,
        target_budget_tokens=SUMMARY_TARGET_TOKENS,
        final_view_budget_tokens=SUMMARY_FINAL_VIEW_TOKENS,
        prefix_complete=True,
        protected_manifest_complete=True,
        action_manifest_complete=True,
        reference_manifest_complete=True,
    )
    prompt = build_summary_prompt(context)
    tagged = (
        ContextMessage(ModelMessage("system", prompt.system_prompt), ContextCategory.SYSTEM_PROMPT),
        ContextMessage(ModelMessage("user", prompt.user_prompt), ContextCategory.CURRENT_USER_TASK),
    )
    admitted = policy.admit(tagged)
    expected = tuple(item.message for item in tagged)
    if (
        admitted.usage.truncated
        or admitted.usage.dropped_exchange_count
        or admitted.messages != expected
        or admitted.tool_definitions
        or admitted.usage.available_input != usage.available_input
        or admitted.usage.context_limit != usage.context_limit
        or admitted.usage.context_policy_version != usage.context_policy_version
        or admitted.usage.reserved_output < SUMMARY_TARGET_TOKENS
    ):
        raise QMSumProtocolError("summary request is not fully admitted")
    return QMSumSummaryPreparation(question_id, context, expected, admitted, baseline_source_tokens)


def validate_research_summary(
    preparation: QMSumSummaryPreparation,
    *,
    content: str,
    finish_reason: str | None,
) -> tuple[SummaryValidationResult | None, QMSumCompressedCandidate | None]:
    """Validate once; never repair, truncate, or retry a provider response."""
    if finish_reason != "stop":
        return None, None
    validation = validate_summary_candidate(
        content, context=preparation.context, counter=_Counter()
    )
    if not validation.valid or validation.candidate is None:
        return validation, None
    if validation.final_view_estimated_tokens >= preparation.baseline_source_tokens:
        return validation, None
    candidate = QMSumCompressedCandidate(
        candidate_id=f"qmsum-summary-v1:{preparation.question_id}",
        arm=QMSumArm.C,
        validation=validation,
        source_question_ids=(preparation.question_id,),
        source_estimated_tokens=preparation.baseline_source_tokens,
    )
    return validation, candidate
