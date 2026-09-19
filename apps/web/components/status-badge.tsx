"use client";

import { useI18n } from "../i18n/provider";
import { statusTone, type StatusTone } from "./badge-tones";

/**
 * Shared status badge. The displayed label is a localized presentation of
 * the raw status value, which remains the underlying contract. Unknown
 * statuses fall back to the raw value, so color and text both stay honest.
 */
const TONE_ARIA_KEY: Record<StatusTone, `status.tone.${StatusTone}`> = {
  neutral: "status.tone.neutral",
  info: "status.tone.info",
  success: "status.tone.success",
  warning: "status.tone.warning",
  danger: "status.tone.danger",
  attention: "status.tone.attention",
};

export default function StatusBadge({
  status,
  label,
  tone,
}: {
  status: string;
  label?: string;
  tone?: StatusTone;
}) {
  const { statusLabel, t } = useI18n();
  const resolvedTone = tone ?? statusTone(status);
  return (
    <span className={`status-badge tone-${resolvedTone}`} aria-label={t(TONE_ARIA_KEY[resolvedTone])}>
      <span className="status-dot" aria-hidden="true" />
      {label ?? statusLabel(status)}
    </span>
  );
}
