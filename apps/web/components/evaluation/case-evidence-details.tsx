"use client";

import Link from "next/link";

import HashValue from "@/components/evaluation/hash-value";
import { Panel } from "@/components/ui/states";
import StatusBadge from "@/components/ui/status-badge";
import { useI18n } from "@/i18n/provider";
import type { Locale } from "@/i18n/types";
import {
  COST_RESERVATION_UNAVAILABLE,
  caseDisplayIdentity,
  classifyEvidenceStep,
  cleanupStatusTone,
  describeCost,
  describeTaskSuccess,
  evidenceHasCostReservationUnavailable,
  hasUnknownOutcomeEvidence,
  isCostReservationUnavailable,
  type CaseEvidenceListItem,
  type EvidenceFailureLabel,
  type ExperimentRunCaseEvidence,
  type ExperimentRunEvidenceStep,
  type TaskSuccessKind,
} from "@/lib/case-evidence-view";

const DETAIL_COPY = {
  "en-US": {
    eyebrow: "Case evidence",
    fallbackTitle: "Case",
    identity: "Identity",
    category: "Category",
    variant: "Variant",
    evidenceLevel: "Evidence level",
    schemaVersion: "Evidence schema",
    taskSuccess: "Task success",
    taskSuccessNote: "This is separate from case and AgentRun execution status.",
    failureCodes: "Failure or attention codes",
    caseFailureCode: "Case failure code",
    agentFailureCode: "AgentRun failure code",
    safeFailureMessage: "Safe failure message",
    executionCleanup: "Historical execution cleanup result",
    executionCleanupNote: "Immutable at the time this case result was recorded.",
    currentCleanup: "Current episode cleanup status",
    currentCleanupNote: "This reflects the lifecycle record now; it does not rewrite the historical result.",
    terminationReason: "Recorded termination reason",
    currentTermination: "Current episode termination reason",
    episode: "Episode",
    episodeMissing:
      "No episode record was returned. This may be a deterministic or older case; absence is not proof that no interaction occurred.",
    episodeId: "Episode ID",
    executionStatus: "Episode execution status",
    cleanupError: "Cleanup error",
    thread: "Thread ID",
    observationHash: "Observation hash",
    assertionHash: "Assertion hash",
    turns: "Turns and AgentRuns",
    turn: "Turn",
    openRun: "Open AgentRun",
    noTurns: "No turn-to-Run links were returned.",
    steps: "Safe execution steps",
    stepsNote:
      "A proposal is not execution. A policy rejection is not execution. UNKNOWN_OUTCOME is an unconfirmed side effect. APPROVAL_EXECUTION comes from a persisted approval execution record and may have no corresponding ACTION step. List position cannot be used to infer exact execution timing.",
    noSteps:
      "No safe steps were returned. This does not mean no tool call or side effect occurred.",
    proposed: "Proposed",
    rejected: "Policy/approval rejected",
    executed: "Execution step recorded",
    failedExecution: "Execution failed",
    unknownOutcome: "UNKNOWN_OUTCOME",
    otherStep: "Other evidence step",
    step: "Step",
    approvalExecutionNoOrdinal: "Approval execution record (no step ordinal)",
    type: "Type",
    tool: "Tool",
    policy: "Policy/approval decision",
    status: "Status",
    error: "Error code",
    actionId: "Logical action ID",
    argumentsHash: "Arguments hash",
    run: "AgentRun",
    ruleLabels: "Rule-based failure labels",
    ruleLabelsNote:
      "These labels locate related steps with deterministic rules. They are not presented as causal root causes.",
    noRuleLabels:
      "No rule labels were returned. Missing labels do not prove that no violation occurred.",
    relatedStep: "Related step orders",
    unknownTitle: "Unknown side effect — reconciliation required",
    unknownBody:
      "The side effect could not be confirmed. Do not retry it automatically. Keep this case's evidence and fixture for reconciliation; raw fixture contents are not exposed here.",
    cost: "Pricing estimate",
    costUnknown: "UNKNOWN",
    costUnknownNote: "Unknown is not zero and does not mean the call was free.",
    costRecorded: "Recorded pricing estimate (not a bill)",
    currency: "Currency",
    currencyMissing: "Not recorded",
    costSourceMissing: "Pricing source not recorded.",
    costReservationTitle: "Amount hard limit unavailable",
    costReservationBody:
      "The amount hard limit is not supported; no billable call was dispatched.",
    legacyTitle: "Older or incomplete evidence protocol",
    legacyBody:
      "Missing fields are shown as not recorded. They are not interpreted as a clean outcome, zero, or absence of a violation.",
    notRecorded: "Not recorded",
    currentRun: "Open the current AgentRun",
    taskSuccessLabels: {
      success: "Success (value 1)",
      failure: "Failure (value 0)",
      unknown: "Unknown",
      not_available: "Not available",
      not_applicable: "Not applicable",
      other: "Other status",
    },
  },
  "zh-CN": {
    eyebrow: "逐题证据",
    fallbackTitle: "题目",
    identity: "身份",
    category: "类别",
    variant: "变体",
    evidenceLevel: "证据等级",
    schemaVersion: "证据协议版本",
    taskSuccess: "任务成功",
    taskSuccessNote: "该结果与 case 执行状态、AgentRun 状态相互独立。",
    failureCodes: "失败或待关注码",
    caseFailureCode: "Case 失败码",
    agentFailureCode: "AgentRun 失败码",
    safeFailureMessage: "安全失败说明",
    executionCleanup: "执行时清理结论（历史）",
    executionCleanupNote: "这是记录该题结果时冻结的历史结论。",
    currentCleanup: "Episode 当前清理状态",
    currentCleanupNote: "该字段反映当前生命周期记录，不会改写历史结果。",
    terminationReason: "记录时终止原因",
    currentTermination: "Episode 当前终止原因",
    episode: "Episode",
    episodeMissing:
      "未返回 Episode 记录。该题可能是确定性题或旧版本结果；缺失并不证明没有发生交互。",
    episodeId: "Episode ID",
    executionStatus: "Episode 执行状态",
    cleanupError: "清理错误",
    thread: "Thread ID",
    observationHash: "Observation 哈希",
    assertionHash: "Assertion 哈希",
    turns: "轮次与 AgentRun",
    turn: "轮次",
    openRun: "打开 AgentRun",
    noTurns: "未返回轮次与 Run 的关联。",
    steps: "安全执行步骤",
    stepsNote:
      "提议不等于执行；策略/审批拒绝不等于执行；UNKNOWN_OUTCOME 表示副作用未确认。APPROVAL_EXECUTION 来自持久审批执行记录，可能没有对应 ACTION 步骤。列表位置不能用于推断精确执行时序。",
    noSteps: "未返回安全步骤。这并不表示没有发生工具调用或外部副作用。",
    proposed: "模型提议",
    rejected: "策略/审批拒绝",
    executed: "已记录执行步骤",
    failedExecution: "执行失败",
    unknownOutcome: "UNKNOWN_OUTCOME",
    otherStep: "其他证据步骤",
    step: "步骤",
    approvalExecutionNoOrdinal: "审批执行记录（无步骤序号）",
    type: "类型",
    tool: "工具",
    policy: "策略/审批决策",
    status: "状态",
    error: "错误码",
    actionId: "逻辑 Action ID",
    argumentsHash: "参数哈希",
    run: "AgentRun",
    ruleLabels: "规则式失败标签",
    ruleLabelsNote: "这些标签由确定性规则定位相关步骤，不作为因果根因结论。",
    noRuleLabels: "未返回规则标签。缺失标签不证明没有违规。",
    relatedStep: "关联步骤序号",
    unknownTitle: "副作用结果未知，需要对账",
    unknownBody:
      "外部副作用无法确认。不要自动重试；请保留该题证据与 fixture 用于对账，本页面不暴露原始 fixture 内容。",
    cost: "计价估算",
    costUnknown: "UNKNOWN",
    costUnknownNote: "未知不等于零，也不表示调用免费。",
    costRecorded: "记录的计价估算（非账单）",
    currency: "币种",
    currencyMissing: "未记录",
    costSourceMissing: "费用来源未记录。",
    costReservationTitle: "金额硬上限暂不支持",
    costReservationBody: "金额硬上限暂不支持，收费调用尚未派发。",
    legacyTitle: "旧版或不完整证据协议",
    legacyBody: "缺失字段显示为未记录，不解释为结果干净、数值为零或没有违规。",
    notRecorded: "未记录",
    currentRun: "打开当前 AgentRun",
    taskSuccessLabels: {
      success: "成功（值 1）",
      failure: "失败（值 0）",
      unknown: "未知",
      not_available: "不可用",
      not_applicable: "不适用",
      other: "其他状态",
    },
  },
} as const;

function stepLabel(
  step: ExperimentRunEvidenceStep,
  text: (typeof DETAIL_COPY)[Locale],
): string {
  switch (classifyEvidenceStep(step)) {
    case "proposed":
      return text.proposed;
    case "rejected":
      return text.rejected;
    case "executed":
      return text.executed;
    case "failed_execution":
      return text.failedExecution;
    case "unknown_outcome":
      return text.unknownOutcome;
    default:
      return text.otherStep;
  }
}

function stepTone(step: ExperimentRunEvidenceStep): "neutral" | "info" | "success" | "warning" | "danger" | "attention" {
  switch (classifyEvidenceStep(step)) {
    case "proposed":
      return "info";
    case "rejected":
      return "danger";
    case "executed":
      return "success";
    case "failed_execution":
      return "danger";
    case "unknown_outcome":
      return "attention";
    default:
      return "neutral";
  }
}

function taskSuccessTone(kind: TaskSuccessKind): "neutral" | "success" | "danger" | "attention" {
  if (kind === "success") return "success";
  if (kind === "failure") return "danger";
  if (kind === "unknown") return "attention";
  return "neutral";
}

function labelsForStep(
  labels: EvidenceFailureLabel[],
  stepOrder: number,
): EvidenceFailureLabel[] {
  return labels.filter((label) => label.step_orders.includes(stepOrder));
}

function hasConcreteStepOrdinal(step: ExperimentRunEvidenceStep): boolean {
  return !(
    step.step_type.toUpperCase() === "APPROVAL_EXECUTION" &&
    step.step_order === 0
  );
}

function CostReservationNotice({
  item,
  evidence,
  text,
}: {
  item?: CaseEvidenceListItem | null;
  evidence: ExperimentRunCaseEvidence;
  text: (typeof DETAIL_COPY)[Locale];
}) {
  const codes = [
    item?.failure_code,
    item?.observed_agent_failure_code,
    ...evidence.failure_labels.map((label) => label.code),
  ].filter((code): code is string => code?.toUpperCase() === COST_RESERVATION_UNAVAILABLE);
  if (
    codes.length === 0 &&
    !isCostReservationUnavailable(item) &&
    !evidenceHasCostReservationUnavailable(evidence)
  ) {
    return null;
  }
  const uniqueCodes = [...new Set(codes)];
  return (
    <div className="case-evidence-notice case-evidence-notice-attention" role="alert">
      <p className="state-title">{text.costReservationTitle}</p>
      <p>{text.costReservationBody}</p>
      {uniqueCodes.map((code) => (
        <code key={code}>{code}</code>
      ))}
    </div>
  );
}

export function CaseEvidenceDetails({
  evidence,
  caseResult,
}: {
  evidence: ExperimentRunCaseEvidence;
  caseResult?: CaseEvidenceListItem | null;
}) {
  const { locale, statusLabel } = useI18n();
  const text = DETAIL_COPY[locale];
  const identity = caseResult
    ? caseDisplayIdentity(caseResult, evidence)
    : {
        caseKey: evidence.case_key ?? (evidence.case_id || text.fallbackTitle),
        category: evidence.category,
        variantLabel: evidence.variant_label,
        taskSuccess: evidence.task_success,
      };
  const taskSuccess = describeTaskSuccess(identity.taskSuccess);
  const cost = describeCost(caseResult?.cost_amount, caseResult?.cost_currency);
  const unknownOutcome = hasUnknownOutcomeEvidence(evidence, caseResult);
  const legacy = evidence.schema_version !== 1;
  const title = identity.caseKey ?? evidence.case_id ?? text.fallbackTitle;
  const stepOrderCounts = new Map<number, number>();
  for (const step of evidence.steps) {
    stepOrderCounts.set(step.step_order, (stepOrderCounts.get(step.step_order) ?? 0) + 1);
  }

  return (
    <Panel title={title} eyebrow={text.eyebrow}>
      <div className="case-evidence-detail">
        <CostReservationNotice item={caseResult} evidence={evidence} text={text} />

        {legacy && (
          <div className="case-evidence-notice" role="status">
            <p className="state-title">{text.legacyTitle}</p>
            <p>{text.legacyBody}</p>
            <code>{evidence.schema_version ?? text.notRecorded}</code>
          </div>
        )}

        {unknownOutcome && (
          <div className="case-evidence-notice case-evidence-notice-attention" role="alert">
            <p className="state-title">{text.unknownTitle}</p>
            <p>{text.unknownBody}</p>
          </div>
        )}

        <dl className="case-evidence-summary">
          <div>
            <dt>{text.identity}</dt>
            <dd><code title={evidence.case_id}>{evidence.case_id || text.notRecorded}</code></dd>
          </div>
          <div>
            <dt>{text.category}</dt>
            <dd>{identity.category ?? text.notRecorded}</dd>
          </div>
          <div>
            <dt>{text.variant}</dt>
            <dd>{identity.variantLabel ?? text.notRecorded}</dd>
          </div>
          <div>
            <dt>{text.evidenceLevel}</dt>
            <dd>{evidence.evidence_level ?? text.notRecorded}</dd>
          </div>
          <div>
            <dt>{text.schemaVersion}</dt>
            <dd>{evidence.schema_version ?? text.notRecorded}</dd>
          </div>
        </dl>

        <section className="case-evidence-section" aria-label={text.taskSuccess}>
          <h3>{text.taskSuccess}</h3>
          <p className="state-hint">{text.taskSuccessNote}</p>
          <div className="case-evidence-task-success">
            <StatusBadge
              status={taskSuccess.status}
              label={text.taskSuccessLabels[taskSuccess.kind]}
              tone={taskSuccessTone(taskSuccess.kind)}
            />
            <span>{text.status}: {taskSuccess.status || text.notRecorded}</span>
            <span>value: {taskSuccess.value === null ? text.notRecorded : String(taskSuccess.value)}</span>
            {taskSuccess.reason && <code>{taskSuccess.reason}</code>}
          </div>
        </section>

        <section className="case-evidence-section" aria-label={text.failureCodes}>
          <h3>{text.failureCodes}</h3>
          {!caseResult?.failure_code &&
          !caseResult?.observed_agent_failure_code &&
          !caseResult?.safe_failure_message ? (
            <p className="case-evidence-empty">{text.notRecorded}</p>
          ) : (
            <dl className="case-evidence-inline-facts">
              <div>
                <dt>{text.caseFailureCode}</dt>
                <dd>
                  {caseResult?.failure_code ? (
                    <code>{caseResult.failure_code}</code>
                  ) : (
                    text.notRecorded
                  )}
                </dd>
              </div>
              <div>
                <dt>{text.agentFailureCode}</dt>
                <dd>
                  {caseResult?.observed_agent_failure_code ? (
                    <code>{caseResult.observed_agent_failure_code}</code>
                  ) : (
                    text.notRecorded
                  )}
                </dd>
              </div>
              {caseResult?.safe_failure_message && (
                <div>
                  <dt>{text.safeFailureMessage}</dt>
                  <dd>{caseResult.safe_failure_message}</dd>
                </div>
              )}
            </dl>
          )}
        </section>

        <div className="case-evidence-lifecycle">
          <section className="case-evidence-section" aria-label={text.executionCleanup}>
            <h3>{text.executionCleanup}</h3>
            <p className="state-hint">{text.executionCleanupNote}</p>
            {evidence.execution_cleanup_status ? (
              <StatusBadge
                status={evidence.execution_cleanup_status}
                tone={cleanupStatusTone(evidence.execution_cleanup_status)}
              />
            ) : (
              <p>{text.notRecorded}</p>
            )}
            <dl className="case-evidence-inline-facts">
              <div>
                <dt>{text.terminationReason}</dt>
                <dd>{evidence.termination_reason ?? text.notRecorded}</dd>
              </div>
            </dl>
          </section>

          <section className="case-evidence-section" aria-label={text.currentCleanup}>
            <h3>{text.currentCleanup}</h3>
            <p className="state-hint">{text.currentCleanupNote}</p>
            {evidence.episode?.cleanup_status ? (
              <StatusBadge
                status={evidence.episode.cleanup_status}
                tone={cleanupStatusTone(evidence.episode.cleanup_status)}
              />
            ) : (
              <p>{text.notRecorded}</p>
            )}
            {evidence.episode?.cleanup_error && (
              <code>{evidence.episode.cleanup_error}</code>
            )}
          </section>
        </div>

        <section className="case-evidence-section" aria-label={text.episode}>
          <h3>{text.episode}</h3>
          {!evidence.episode ? (
            <p className="case-evidence-empty">{text.episodeMissing}</p>
          ) : (
            <>
              <dl className="case-evidence-summary">
                <div>
                  <dt>{text.episodeId}</dt>
                  <dd><code>{evidence.episode.episode_id ?? text.notRecorded}</code></dd>
                </div>
                <div>
                  <dt>{text.executionStatus}</dt>
                  <dd>
                    {evidence.episode.execution_status ? (
                      <StatusBadge status={evidence.episode.execution_status} />
                    ) : (
                      text.notRecorded
                    )}
                  </dd>
                </div>
                <div>
                  <dt>{text.currentTermination}</dt>
                  <dd>{evidence.episode.termination_reason ?? text.notRecorded}</dd>
                </div>
                <div>
                  <dt>{text.cleanupError}</dt>
                  <dd>{evidence.episode.cleanup_error ?? text.notRecorded}</dd>
                </div>
                <div>
                  <dt>{text.thread}</dt>
                  <dd><code title={evidence.episode.thread_id ?? undefined}>{evidence.episode.thread_id ?? text.notRecorded}</code></dd>
                </div>
                <div>
                  <dt>{text.observationHash}</dt>
                  <dd><HashValue value={evidence.episode.observation_hash} label={text.observationHash} /></dd>
                </div>
                <div>
                  <dt>{text.assertionHash}</dt>
                  <dd><HashValue value={evidence.episode.assertion_hash} label={text.assertionHash} /></dd>
                </div>
              </dl>

              <h4>{text.turns}</h4>
              {evidence.episode.turns.length === 0 ? (
                <p className="case-evidence-empty">{text.noTurns}</p>
              ) : (
                <ol className="case-evidence-turns">
                  {evidence.episode.turns.map((turn) => (
                    <li className="case-evidence-turn" key={`${turn.turn_index}:${turn.agent_run_id ?? "none"}`}>
                      <span>{text.turn} {turn.turn_index}</span>
                      {turn.status ? <StatusBadge status={turn.status} /> : <span>{text.notRecorded}</span>}
                      {turn.agent_run_id ? (
                        <Link href={`/runs/${encodeURIComponent(turn.agent_run_id)}`}>
                          {text.openRun}
                        </Link>
                      ) : (
                        <span>{text.notRecorded}</span>
                      )}
                    </li>
                  ))}
                </ol>
              )}
            </>
          )}
        </section>

        <section className="case-evidence-section" aria-label={text.steps}>
          <h3>{text.steps}</h3>
          <p className="state-hint">{text.stepsNote}</p>
          {evidence.steps.length === 0 ? (
            <p className="case-evidence-empty">{text.noSteps}</p>
          ) : (
            <ol className="case-evidence-steps">
              {evidence.steps.map((step, index) => {
                const hasOrdinal = hasConcreteStepOrdinal(step);
                const ruleLabels =
                  hasOrdinal && stepOrderCounts.get(step.step_order) === 1
                    ? labelsForStep(evidence.failure_labels, step.step_order)
                    : [];
                return (
                  <li
                    className={`case-evidence-step case-evidence-step-${classifyEvidenceStep(step)}`}
                    key={[
                      index,
                      step.step_order,
                      step.step_type,
                      step.agent_run_id ?? "none",
                      step.logical_action_id ?? "none",
                    ].join(":")}
                  >
                    <div className="case-evidence-step-header">
                      <span>
                        {hasOrdinal
                          ? `${text.step} ${step.step_order}`
                          : text.approvalExecutionNoOrdinal}
                      </span>
                      <StatusBadge
                        status={`EVIDENCE_${classifyEvidenceStep(step).toUpperCase()}`}
                        label={stepLabel(step, text)}
                        tone={stepTone(step)}
                      />
                    </div>
                    <dl className="case-evidence-step-meta">
                      <div>
                        <dt>{text.type}</dt>
                        <dd><code>{step.step_type}</code></dd>
                      </div>
                      <div>
                        <dt>{text.tool}</dt>
                        <dd>{step.tool_name ?? text.notRecorded}</dd>
                      </div>
                      <div>
                        <dt>{text.status}</dt>
                        <dd>
                          {step.status ? <StatusBadge status={step.status} /> : text.notRecorded}
                        </dd>
                      </div>
                      <div>
                        <dt>{text.policy}</dt>
                        <dd>{step.policy_decision ?? text.notRecorded}</dd>
                      </div>
                      <div>
                        <dt>{text.error}</dt>
                        <dd>{step.error_code ? <code>{step.error_code}</code> : text.notRecorded}</dd>
                      </div>
                      <div>
                        <dt>{text.actionId}</dt>
                        <dd><HashValue value={step.logical_action_id} label={text.actionId} /></dd>
                      </div>
                      <div>
                        <dt>{text.argumentsHash}</dt>
                        <dd><HashValue value={step.arguments_hash} label={text.argumentsHash} /></dd>
                      </div>
                      <div>
                        <dt>{text.run}</dt>
                        <dd>
                          {step.agent_run_id ? (
                            <Link href={`/runs/${encodeURIComponent(step.agent_run_id)}`}>
                              {text.openRun}
                            </Link>
                          ) : (
                            text.notRecorded
                          )}
                        </dd>
                      </div>
                    </dl>
                    {ruleLabels.length > 0 && (
                      <div className="case-evidence-label-list">
                        {ruleLabels.map((label) => (
                          <code className="case-evidence-label" key={label.code}>
                            {label.code}
                          </code>
                        ))}
                      </div>
                    )}
                  </li>
                );
              })}
            </ol>
          )}
        </section>

        <section className="case-evidence-section" aria-label={text.ruleLabels}>
          <h3>{text.ruleLabels}</h3>
          <p className="state-hint">{text.ruleLabelsNote}</p>
          {evidence.failure_labels.length === 0 ? (
            <p className="case-evidence-empty">{text.noRuleLabels}</p>
          ) : (
            <ul className="case-evidence-label-list">
              {evidence.failure_labels.map((label, index) => (
                <li
                  className="case-evidence-label"
                  key={`${index}:${label.code}:${label.step_orders.join(",")}`}
                >
                  <code>{label.code}</code>
                  <span>
                    {text.relatedStep}: {label.step_orders.length > 0 ? label.step_orders.join(", ") : text.notRecorded}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="case-evidence-section" aria-label={text.cost}>
          <h3>{text.cost}</h3>
          {cost.kind === "unknown" ? (
            <>
              <strong>{text.costUnknown}</strong>
              <p className="state-hint">{text.costUnknownNote}</p>
            </>
          ) : (
            <>
              <strong>{cost.text} {cost.currency ?? ""}</strong>
              <p className="state-hint">{text.costRecorded}</p>
            </>
          )}
          <p className="state-hint">
            {text.currency}: {cost.currency ?? text.currencyMissing}
          </p>
          <p className="state-hint">{text.costSourceMissing}</p>
        </section>

        {caseResult?.agent_run_id && (
          <Link className="button button-ghost" href={`/runs/${encodeURIComponent(caseResult.agent_run_id)}`}>
            {text.currentRun}
          </Link>
        )}
      </div>
    </Panel>
  );
}

export default CaseEvidenceDetails;
