/**
 * Content-free presentation helpers for experiment case evidence.
 *
 * The evidence API returns identities, hashes, statuses, and error codes only.
 * Keep any raw observation, prompt, argument, result, or private fact outside
 * this model even if an older endpoint happens to return it.
 */

import type {
  EvaluationCaseEvidence,
  EvaluationCaseEvidenceEpisode,
  EvaluationCaseEvidenceStep,
  EvaluationCaseFailureLabel,
  EvaluationCaseResult,
  EvaluationTaskSuccessProjection,
} from "@/lib/api/evaluation";

export type EvidenceMetricValue = EvaluationTaskSuccessProjection;

export type EvidenceEpisodeTurn = EvaluationCaseEvidenceEpisode["turns"][number];

export type EvidenceEpisode = Omit<
  EvaluationCaseEvidenceEpisode,
  "episode_id" | "execution_status" | "cleanup_status"
> & {
  episode_id: string | null;
  execution_status: string | null;
  cleanup_status: string | null;
};

export type ExperimentRunEvidenceStep = EvaluationCaseEvidenceStep;

export type EvidenceFailureLabel = EvaluationCaseFailureLabel;

export type ExperimentRunCaseEvidence = Omit<
  EvaluationCaseEvidence,
  "schema_version" | "episode"
> & {
  schema_version: number | null;
  episode: EvidenceEpisode | null;
};

/**
 * The list endpoint's current typed fields plus optional safe projections that
 * may be added by the API client. No property is inferred from observation.
 */
export type CaseEvidenceListItem = Pick<
  EvaluationCaseResult,
  | "id"
  | "status"
  | "observed_agent_status"
  | "case_execution_key"
  | "experiment_variant_id"
  | "repetition_index"
  | "agent_run_id"
  | "cost_amount"
  | "cost_currency"
  | "failure_code"
  | "observed_agent_failure_code"
  | "safe_failure_message"
> & {
  case_key?: string | null;
  category?: string | null;
  variant_label?: string | null;
  task_success?: EvidenceMetricValue | null;
};

export type CaseEvidenceList = {
  total: number;
  items: CaseEvidenceListItem[];
};

export const COST_RESERVATION_UNAVAILABLE = "INTERACTIVE_COST_RESERVATION_UNAVAILABLE";

const REJECTED_MARKERS = new Set([
  "DENY",
  "DENIED",
  "REJECT",
  "REJECTED",
  "BLOCKED",
  "POLICY_BLOCKED",
  "APPROVAL_REJECTED",
  "APPROVAL_DENIED",
]);

const PROPOSAL_STEP_TYPES = new Set(["TOOL_PROPOSAL", "TOOL_CALL_PROPOSAL", "PROPOSAL"]);
const EXECUTION_STEP_TYPES = new Set([
  "TOOL_EXECUTE",
  "ACTION",
  "ACTION_EXECUTE",
  "APPROVAL_EXECUTION",
]);

function isUnknownOutcomeMarker(value: string | null | undefined): boolean {
  const normalized = value?.toUpperCase() ?? "";
  return normalized === "UNKNOWN" || normalized.includes("UNKNOWN_OUTCOME");
}

function isFailedExecutionMarker(value: string | null | undefined): boolean {
  const normalized = value?.toUpperCase() ?? "";
  return (
    normalized === "FAILED" ||
    normalized === "FAILURE" ||
    normalized.endsWith("_FAILED")
  );
}

export type EvidenceStepClassification =
  | "proposed"
  | "rejected"
  | "executed"
  | "failed_execution"
  | "unknown_outcome"
  | "other";

export type TaskSuccessKind =
  | "success"
  | "failure"
  | "unknown"
  | "not_available"
  | "not_applicable"
  | "other";

export type TaskSuccessView = {
  kind: TaskSuccessKind;
  status: string;
  value: number | null;
  reason: string | null;
};

export type CostView =
  | { kind: "unknown"; value: null; currency: string | null; text: "UNKNOWN" }
  | { kind: "recorded"; value: number | string; currency: string | null; text: string };

function record(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function integerValue(value: unknown): number | null {
  const number = finiteNumber(value);
  return number !== null && Number.isInteger(number) ? number : null;
}

function normalizeTaskSuccess(value: unknown): EvidenceMetricValue {
  const item = record(value);
  if (!item) {
    return { status: "NOT_AVAILABLE", value: null, reason: null };
  }
  return {
    status: stringValue(item.status) ?? "NOT_AVAILABLE",
    value: finiteNumber(item.value),
    reason: stringValue(item.reason),
  };
}

function normalizeEpisode(value: unknown): EvidenceEpisode | null {
  const item = record(value);
  if (!item) return null;
  const turns = Array.isArray(item.turns)
    ? item.turns.flatMap((candidate): EvidenceEpisodeTurn[] => {
        const turn = record(candidate);
        const turnIndex = turn ? integerValue(turn.turn_index) : null;
        if (!turn || turnIndex === null) return [];
        return [
          {
            turn_index: turnIndex,
            agent_run_id: stringValue(turn.agent_run_id),
            status: stringValue(turn.status),
          },
        ];
      })
    : [];
  return {
    episode_id: stringValue(item.episode_id),
    execution_status: stringValue(item.execution_status),
    cleanup_status: stringValue(item.cleanup_status),
    cleanup_error: stringValue(item.cleanup_error),
    termination_reason: stringValue(item.termination_reason),
    thread_id: stringValue(item.thread_id),
    observation_hash: stringValue(item.observation_hash),
    assertion_hash: stringValue(item.assertion_hash),
    turns,
  };
}

function normalizeStep(value: unknown): ExperimentRunEvidenceStep | null {
  const item = record(value);
  const stepOrder = item ? integerValue(item.step_order) : null;
  const stepType = item ? stringValue(item.step_type) : null;
  if (!item || stepOrder === null || !stepType) return null;
  return {
    step_order: stepOrder,
    step_type: stepType,
    agent_run_id: stringValue(item.agent_run_id),
    logical_action_id: stringValue(item.logical_action_id),
    arguments_hash: stringValue(item.arguments_hash),
    status: stringValue(item.status),
    error_code: stringValue(item.error_code),
    tool_name: stringValue(item.tool_name),
    policy_decision: stringValue(item.policy_decision),
  };
}

function normalizeFailureLabel(value: unknown): EvidenceFailureLabel | null {
  const item = record(value);
  const code = item ? stringValue(item.code) : null;
  if (!item || !code) return null;
  const stepOrders = Array.isArray(item.step_orders)
    ? item.step_orders.flatMap((candidate) => {
        const order = integerValue(candidate);
        return order === null ? [] : [order];
      })
    : [];
  return { code, step_orders: stepOrders };
}

/**
 * Rebuild the response from the documented safe fields. This is a privacy
 * boundary as well as an old-response compatibility layer: unknown properties
 * are discarded before any component can render them.
 */
export function normalizeCaseEvidence(payload: unknown): ExperimentRunCaseEvidence {
  const item: Record<string, unknown> = record(payload) ?? {};
  return {
    schema_version: integerValue(item.schema_version),
    case_id: stringValue(item.case_id) ?? "",
    case_key: stringValue(item.case_key),
    category: stringValue(item.category),
    variant_label: stringValue(item.variant_label),
    task_success: normalizeTaskSuccess(item.task_success),
    evidence_level: stringValue(item.evidence_level),
    execution_cleanup_status: stringValue(item.execution_cleanup_status),
    termination_reason: stringValue(item.termination_reason),
    episode: normalizeEpisode(item.episode),
    steps: Array.isArray(item.steps)
      ? item.steps.flatMap((step) => {
          const normalized = normalizeStep(step);
          return normalized ? [normalized] : [];
        })
      : [],
    failure_labels: Array.isArray(item.failure_labels)
      ? item.failure_labels.flatMap((label) => {
          const normalized = normalizeFailureLabel(label);
          return normalized ? [normalized] : [];
        })
      : [],
  };
}

/**
 * Copy only list fields used by the case UI. In particular, the legacy raw
 * `observation` object is never retained by this view model.
 */
export function normalizeCaseEvidenceList(payload: unknown): CaseEvidenceList {
  const item: Record<string, unknown> = record(payload) ?? {};
  const candidates = Array.isArray(payload)
    ? payload
    : Array.isArray(item.items)
      ? item.items
      : [];
  const items = candidates.flatMap((candidate): CaseEvidenceListItem[] => {
    const row = record(candidate);
    const id = row ? stringValue(row.id) : null;
    if (!row || !id) return [];
    const repetitionIndex = integerValue(row.repetition_index);
    const taskSuccess = record(row.task_success);
    return [
      {
        id,
        status: stringValue(row.status) ?? "",
        observed_agent_status: stringValue(row.observed_agent_status),
        case_execution_key: stringValue(row.case_execution_key) ?? id,
        experiment_variant_id: stringValue(row.experiment_variant_id) ?? "",
        repetition_index: repetitionIndex ?? 0,
        agent_run_id: stringValue(row.agent_run_id),
        cost_amount:
          typeof row.cost_amount === "number" || typeof row.cost_amount === "string"
            ? row.cost_amount
            : null,
        cost_currency: stringValue(row.cost_currency),
        failure_code: stringValue(row.failure_code),
        observed_agent_failure_code: stringValue(row.observed_agent_failure_code),
        safe_failure_message: stringValue(row.safe_failure_message),
        case_key: stringValue(row.case_key),
        category: stringValue(row.category),
        variant_label: stringValue(row.variant_label),
        task_success: taskSuccess ? normalizeTaskSuccess(taskSuccess) : null,
      },
    ];
  });
  return {
    items,
    total: finiteNumber(item.total) ?? items.length,
  };
}

export function describeTaskSuccess(metric: EvidenceMetricValue | null | undefined): TaskSuccessView {
  if (!metric) {
    return { kind: "not_available", status: "NOT_LOADED", value: null, reason: null };
  }
  const status = metric.status.toUpperCase();
  const base = { status: metric.status, value: metric.value, reason: metric.reason };
  if (status === "AVAILABLE") {
    if (metric.value === 1) return { ...base, kind: "success" };
    if (metric.value === 0) return { ...base, kind: "failure" };
    return { ...base, kind: metric.value === null ? "unknown" : "other" };
  }
  if (status === "UNKNOWN") return { ...base, kind: "unknown" };
  if (status === "NOT_APPLICABLE") return { ...base, kind: "not_applicable" };
  if (status === "NOT_AVAILABLE") return { ...base, kind: "not_available" };
  return { ...base, kind: "other" };
}

export function describeCost(value: number | string | null | undefined, currency: string | null | undefined): CostView {
  const resolvedCurrency = stringValue(currency);
  if (value === null || value === undefined || value === "") {
    return { kind: "unknown", value: null, currency: resolvedCurrency, text: "UNKNOWN" };
  }
  return { kind: "recorded", value, currency: resolvedCurrency, text: String(value) };
}

export function classifyEvidenceStep(step: ExperimentRunEvidenceStep): EvidenceStepClassification {
  const stepType = step.step_type.toUpperCase();
  const status = step.status?.toUpperCase() ?? "";
  const errorCode = step.error_code?.toUpperCase() ?? "";
  const policyDecision = step.policy_decision?.toUpperCase() ?? "";

  if (
    isUnknownOutcomeMarker(status) ||
    isUnknownOutcomeMarker(errorCode) ||
    isUnknownOutcomeMarker(policyDecision)
  ) {
    return "unknown_outcome";
  }
  if (
    REJECTED_MARKERS.has(status) ||
    REJECTED_MARKERS.has(errorCode) ||
    REJECTED_MARKERS.has(policyDecision)
  ) {
    return "rejected";
  }
  if (EXECUTION_STEP_TYPES.has(stepType)) {
    if (isFailedExecutionMarker(status) || Boolean(errorCode)) {
      return "failed_execution";
    }
    return "executed";
  }
  if (PROPOSAL_STEP_TYPES.has(stepType)) return "proposed";
  return "other";
}

export function isCostReservationUnavailable(item: CaseEvidenceListItem | null | undefined): boolean {
  return (
    item?.failure_code?.toUpperCase() === COST_RESERVATION_UNAVAILABLE ||
    item?.observed_agent_failure_code?.toUpperCase() === COST_RESERVATION_UNAVAILABLE
  );
}

export function evidenceHasCostReservationUnavailable(evidence: ExperimentRunCaseEvidence | null | undefined): boolean {
  return (
    evidence?.failure_labels.some(
      (label) => label.code.toUpperCase() === COST_RESERVATION_UNAVAILABLE,
    ) ?? false
  );
}

export function hasUnknownOutcomeEvidence(
  evidence: ExperimentRunCaseEvidence | null | undefined,
  item?: CaseEvidenceListItem | null,
): boolean {
  if (!evidence) {
    return (
      isUnknownOutcomeMarker(item?.failure_code) ||
      isUnknownOutcomeMarker(item?.observed_agent_failure_code)
    );
  }
  if (
    isUnknownOutcomeMarker(evidence.termination_reason) ||
    isUnknownOutcomeMarker(evidence.execution_cleanup_status) ||
    isUnknownOutcomeMarker(evidence.episode?.execution_status) ||
    isUnknownOutcomeMarker(evidence.episode?.termination_reason) ||
    isUnknownOutcomeMarker(evidence.episode?.cleanup_error)
  ) {
    return true;
  }
  return (
    evidence.steps.some((step) => classifyEvidenceStep(step) === "unknown_outcome") ||
    evidence.failure_labels.some((label) => isUnknownOutcomeMarker(label.code)) ||
    isUnknownOutcomeMarker(item?.failure_code) ||
    isUnknownOutcomeMarker(item?.observed_agent_failure_code)
  );
}

export function caseDisplayIdentity(
  item: CaseEvidenceListItem,
  evidence?: ExperimentRunCaseEvidence | null,
) {
  const evidenceTaskSuccess =
    evidence?.task_success.status.toUpperCase() === "NOT_AVAILABLE"
      ? null
      : evidence?.task_success;
  return {
    caseKey:
      stringValue(evidence?.case_key) ??
      stringValue(item.case_key) ??
      item.case_execution_key,
    category: stringValue(evidence?.category) ?? stringValue(item.category),
    variantLabel: stringValue(evidence?.variant_label) ?? stringValue(item.variant_label),
    taskSuccess:
      evidenceTaskSuccess ??
      item.task_success ??
      evidence?.task_success ??
      null,
  };
}

export function cleanupStatusTone(
  status: string | null | undefined,
): "neutral" | "warning" | "success" | "danger" | "attention" {
  switch (status?.toUpperCase()) {
    case "TEARDOWN_COMPLETE":
      return "success";
    case "PENDING":
      return "warning";
    case "TEARDOWN_FAILED":
      return "danger";
    case "CLEANUP_BLOCKED":
      return "attention";
    default:
      return "neutral";
  }
}
