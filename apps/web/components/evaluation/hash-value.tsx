"use client";

import { useState } from "react";

import { useI18n } from "@/i18n/provider";

/** Short mono hash/id with copy-to-clipboard. Full value available as title. */
export default function HashValue({ value, label }: { value: string | null | undefined; label?: string }) {
  const { t } = useI18n();
  const [copied, setCopied] = useState(false);
  if (!value) return <span className="muted">—</span>;
  const resolved = value;
  const short = resolved.length <= 12 ? resolved : `${resolved.slice(0, 8)}…`;
  async function copy() {
    try {
      await navigator.clipboard.writeText(resolved);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard unavailable (permissions/insecure context) — title still shows the value.
    }
  }
  return (
    <span className="hash-value">
      <code title={resolved}>{short}</code>
      <button type="button" className="hash-copy" onClick={copy} aria-label={`${label ?? ""} ${t("common.copy")}`.trim()}>
        {copied ? t("common.copied") : t("common.copy")}
      </button>
    </span>
  );
}

/** Inline confirmation area for destructive/freezing mutations. */
export function InlineConfirm({
  title,
  text,
  confirmLabel,
  pendingLabel,
  cancelLabel,
  onConfirm,
  onCancel,
  pending,
}: {
  title: string;
  text: string;
  confirmLabel: string;
  pendingLabel: string;
  cancelLabel: string;
  onConfirm: () => void;
  onCancel: () => void;
  pending: boolean;
}) {
  return (
    <div className="inline-confirm" role="alertdialog" aria-label={title}>
      <p className="state-title">{title}</p>
      <p className="state-hint">{text}</p>
      <div className="inline-confirm-actions">
        <button type="button" className="button button-danger" onClick={onConfirm} disabled={pending}>
          {pending ? pendingLabel : confirmLabel}
        </button>
        <button type="button" className="button button-ghost" onClick={onCancel} disabled={pending}>
          {cancelLabel}
        </button>
      </div>
    </div>
  );
}
