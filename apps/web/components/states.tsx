"use client";

import type { ReactNode } from "react";

import { useFrontendSession } from "./session-provider";

/** Quiet, structural placeholder for "nothing here yet". */
export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="state-block state-empty">
      <p className="state-title">{title}</p>
      {hint && <p className="state-hint">{hint}</p>}
    </div>
  );
}

/**
 * Safe API error presentation: code + safe message from the documented
 * error envelope. Stack traces never reach this component.
 */
export function ErrorState({
  code,
  message,
  hint,
  onRetry,
}: {
  code: string;
  message: string;
  hint?: string;
  onRetry?: () => void;
}) {
  return (
    <div className="state-block state-error" role="alert">
      <p className="state-title">
        <code className="state-code">{code}</code> {message}
      </p>
      {hint && <p className="state-hint">{hint}</p>}
      {onRetry && (
        <button type="button" className="button button-ghost" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

/** Structured section-level loading placeholder (no full-page blocking). */
export function LoadingState({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="state-block state-loading" role="status" aria-live="polite">
      <span className="loading-bar" aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}

/**
 * Rendered on deep links after a refresh, when the in-memory session is
 * gone. Configuring the session loads the current page in place.
 */
export function SessionRequired({ context }: { context?: string }) {
  const { openPanel } = useFrontendSession();
  return (
    <div className="state-block state-session">
      <p className="state-title">Session required</p>
      <p className="state-hint">
        Configure a workspace session{context ? ` to view ${context}` : " to view this page"}.
        The session lives in memory only and is cleared on refresh.
      </p>
      <button type="button" className="button button-primary" onClick={openPanel}>
        Configure session
      </button>
    </div>
  );
}

export function Panel({
  title,
  eyebrow,
  actions,
  children,
  ariaLabel,
}: {
  title?: string;
  eyebrow?: string;
  actions?: ReactNode;
  children: ReactNode;
  ariaLabel?: string;
}) {
  return (
    <section className="panel" aria-label={ariaLabel} aria-labelledby={title ? undefined : ariaLabel}>
      {(title || actions) && (
        <div className="panel-heading">
          <div>
            {eyebrow && <p className="eyebrow">{eyebrow}</p>}
            {title && <h2>{title}</h2>}
          </div>
          {actions && <div className="panel-actions">{actions}</div>}
        </div>
      )}
      {children}
    </section>
  );
}
