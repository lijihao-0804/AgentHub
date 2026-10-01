/**
 * Pure status → tone mapping. The status text itself is the primary
 * signal; tones only reinforce the state and are locale-independent.
 */
export type StatusTone = "neutral" | "info" | "success" | "warning" | "danger" | "attention";

const TONE_BY_STATUS: Record<string, StatusTone> = {
  // Run statuses
  RUNNING: "info",
  WAITING_APPROVAL: "warning",
  SUCCEEDED: "success",
  FAILED: "danger",
  NEEDS_ATTENTION: "attention",
  CANCEL_REQUESTED: "warning",
  CANCELLED: "neutral",
  // Approval decision statuses
  PENDING: "warning",
  APPROVED: "success",
  DENIED: "danger",
  EXPIRED: "neutral",
  // Approval execution statuses
  NOT_STARTED: "neutral",
  CLAIMED: "info",
  UNKNOWN_OUTCOME: "attention",
  // Timeline step statuses
  COMPLETED: "success",
  // Evaluation artifact statuses
  DRAFT: "warning",
  PUBLISHED: "success",
  READY: "success",
  QUEUED: "info",
  AVAILABLE: "success",
  NOT_AVAILABLE: "neutral",
  NOT_APPLICABLE: "neutral",
  COMPLETE: "success",
  INCOMPLETE: "warning",
  NOT_COMPARABLE: "neutral",
  PASS: "success",
  FAIL: "danger",
  INCONCLUSIVE: "neutral",
};

export function statusTone(status: string): StatusTone {
  return TONE_BY_STATUS[status.toUpperCase()] ?? "neutral";
}

/**
 * Tool risk is its own dimension (AGENTS.md rule 9: READ does not mean
 * safe), so it never maps through the status table: HIGH must read as
 * danger everywhere, not neutral grey.
 */
export function riskTone(risk: string | null | undefined): StatusTone {
  switch ((risk ?? "").toUpperCase()) {
    case "LOW":
      return "info";
    case "MEDIUM":
      return "warning";
    case "HIGH":
      return "danger";
    default:
      return "neutral";
  }
}
