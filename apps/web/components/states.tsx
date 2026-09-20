"use client";

import Link from "next/link";
import type { ReactNode } from "react";

import { useI18n } from "../i18n/provider";
import type { MessageKey } from "../i18n/messages";
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
  const { t } = useI18n();
  return (
    <div className="state-block state-error" role="alert">
      <p className="state-title">
        <code className="state-code">{code}</code> {message}
      </p>
      {hint && <p className="state-hint">{hint}</p>}
      {onRetry && (
        <button type="button" className="button button-ghost" onClick={onRetry}>
          {t("common.retry")}
        </button>
      )}
    </div>
  );
}

/** Structured section-level loading placeholder (no full-page blocking). */
export function LoadingState({ label }: { label?: string }) {
  const { t } = useI18n();
  return (
    <div className="state-block state-loading" role="status" aria-live="polite">
      <span className="loading-bar" aria-hidden="true" />
      <span>{label ?? t("common.loading")}</span>
    </div>
  );
}

/**
 * Rendered by workspace-scoped pages when the signed-in user has no
 * active workspace selected. Opening the selector loads the page in
 * place; if the account has no workspace yet, onboarding is offered.
 */
export function SessionRequired({ contextKey }: { contextKey?: MessageKey }) {
  const { t } = useI18n();
  const { openPanel, workspaces, tenancyLoading } = useFrontendSession();
  const context = contextKey ? t(contextKey) : undefined;

  if (tenancyLoading) return <LoadingState />;

  if (workspaces.length === 0) {
    return (
      <div className="state-block state-session">
        <p className="state-title">{t("workspace.noWorkspaces")}</p>
        <p className="state-hint">{t("workspace.noWorkspacesHint")}</p>
        <Link className="button button-primary" href="/settings">
          {t("workspace.createWorkspace")}
        </Link>
      </div>
    );
  }

  return (
    <div className="state-block state-session">
      <p className="state-title">{t("session.requiredTitle")}</p>
      <p className="state-hint">{t("session.requiredHint", { context: context ?? "" })}</p>
      <button type="button" className="button button-primary" onClick={openPanel}>
        {t("session.configure")}
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
