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
};

export function statusTone(status: string): StatusTone {
  return TONE_BY_STATUS[status.toUpperCase()] ?? "neutral";
}
