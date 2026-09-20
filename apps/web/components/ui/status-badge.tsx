"use client";

import { useI18n } from "@/i18n/provider";
import { statusTone, type StatusTone } from "@/components/ui/badge-tones";

/**
 * Shared status badge. The displayed label is a localized presentation of
 * the raw status value, which remains the underlying contract. Unknown
 * statuses fall back to the raw value, so color and text both stay honest.
 */
export default function StatusBadge({
  status,
  label,
  tone,
}: {
  status: string;
  label?: string;
  tone?: StatusTone;
}) {
  const { statusLabel } = useI18n();
  const resolvedTone = tone ?? statusTone(status);
  return (
    <span className={`status-badge tone-${resolvedTone}`}>
      <span className="status-dot" aria-hidden="true" />
      {label ?? statusLabel(status)}
    </span>
  );
}
