/**
 * Shared status badge. The status text is always visible, so color is
 * never the only signal; tones only reinforce the state.
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

const TONE_LABEL: Record<StatusTone, string> = {
  neutral: "Status",
  info: "Status: active",
  success: "Status: ok",
  warning: "Status: waiting",
  danger: "Status: failed",
  attention: "Status: needs attention",
};

export function statusTone(status: string): StatusTone {
  return TONE_BY_STATUS[status.toUpperCase()] ?? "neutral";
}

export default function StatusBadge({
  status,
  label,
  tone,
}: {
  status: string;
  label?: string;
  tone?: StatusTone;
}) {
  const resolvedTone = tone ?? statusTone(status);
  return (
    <span className={`status-badge tone-${resolvedTone}`} aria-label={TONE_LABEL[resolvedTone]}>
      <span className="status-dot" aria-hidden="true" />
      {label ?? status}
    </span>
  );
}
