"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import StatusBadge from "../../components/status-badge";
import { EmptyState, ErrorState, LoadingState, Panel, SessionRequired } from "../../components/states";
import { KeyValues } from "../../components/technical-details";
import TechnicalDetails from "../../components/technical-details";
import { ApiError, errorHintKey, toApiError } from "../../lib/api-client";
import { Approval, decideApproval, listApprovals } from "../../lib/approvals";
import { useFrontendSession } from "../../components/session-provider";
import { useI18n } from "../../i18n/provider";

type DecisionState = { approvalId: string; decision: "approve" | "deny" } | null;

/** Client-side counts over the currently loaded list; no aggregate API exists. */
function countSummary(approvals: Approval[]) {
  const pending = approvals.filter((a) => a.decision_status === "PENDING").length;
  const approved = approvals.filter((a) => a.decision_status === "APPROVED").length;
  const denied = approvals.filter((a) => a.decision_status === "DENIED").length;
  const needsAttention = approvals.filter((a) => a.execution_status === "UNKNOWN_OUTCOME").length;
  return { pending, approved, denied, needsAttention };
}

function argumentEntries(approval: Approval): Array<[string, unknown]> {
  return Object.entries(approval.canonical_arguments ?? {}).map(([key, value]) => {
    if (
      value === null ||
      value === undefined ||
      typeof value === "string" ||
      typeof value === "number" ||
      typeof value === "boolean"
    ) {
      return [key, value] as [string, unknown];
    }
    return [key, JSON.stringify(value)] as [string, unknown];
  });
}

export default function ApprovalsPage() {
  const { t, formatDateTime } = useI18n();
  const { workspaceId, accessToken, connected } = useFrontendSession();
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [error, setError] = useState<ApiError | null>(null);
  const [decisionError, setDecisionError] = useState<{ approvalId: string; message: string } | null>(null);
  const [decisionState, setDecisionState] = useState<DecisionState>(null);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);

  const refresh = useCallback(async () => {
    setError(null);
    if (!connected) return;
    setLoading(true);
    try {
      setApprovals(await listApprovals(workspaceId, accessToken));
      setLoaded(true);
    } catch (caught) {
      setError(toApiError(caught, ""));
    } finally {
      setLoading(false);
    }
  }, [connected, workspaceId, accessToken]);

  useEffect(() => {
    if (connected && !loaded) void refresh();
  }, [connected, loaded, refresh]);

  useEffect(() => {
    if (!connected) setLoaded(false);
  }, [connected]);

  async function decide(approvalId: string, decision: "approve" | "deny") {
    setDecisionError(null);
    setDecisionState({ approvalId, decision });
    try {
      const result = await decideApproval(workspaceId, approvalId, decision, accessToken);
      setApprovals((current) =>
        current.map((approval) => (approval.id === approvalId ? result.approval : approval)),
      );
    } catch (caught) {
      const apiError = toApiError(caught, "");
      setDecisionError({ approvalId, message: apiError.message || t("errors.decideApproval") });
    } finally {
      setDecisionState(null);
    }
  }

  if (!connected) {
    return (
      <div className="page">
        <header className="page-header">
          <p className="eyebrow">{t("approvals.eyebrow")}</p>
          <h1>{t("approvals.title")}</h1>
          <p className="page-lede">{t("approvals.lede")}</p>
        </header>
        <SessionRequired contextKey="session.context.approvals" />
      </div>
    );
  }

  const counts = countSummary(approvals);
  const hint = error ? errorHintKey(error) : null;

  return (
    <div className="page">
      <header className="page-header">
        <p className="eyebrow">{t("approvals.eyebrow")}</p>
        <h1>{t("approvals.title")}</h1>
        <p className="page-lede">{t("approvals.lede")}</p>
      </header>

      <Panel
        ariaLabel={t("approvals.inboxTitle")}
        title={t("approvals.inboxTitle")}
        eyebrow={t("approvals.inboxEyebrow")}
        actions={
          <>
            <span className="state-hint" aria-live="polite">
              {loading ? t("common.loading") : t("approvals.loaded", { count: approvals.length })}
            </span>
            <button type="button" className="button button-ghost" onClick={() => void refresh()} disabled={loading}>
              {t("common.refresh")}
            </button>
          </>
        }
      >
        <div className="approval-counts">
          <StatusBadge status="PENDING" label={t("approvals.counts.pending", { count: counts.pending })} />
          <StatusBadge status="APPROVED" label={t("approvals.counts.approved", { count: counts.approved })} />
          <StatusBadge status="DENIED" label={t("approvals.counts.denied", { count: counts.denied })} />
          <StatusBadge
            status="UNKNOWN_OUTCOME"
            label={t("approvals.counts.needsAttention", { count: counts.needsAttention })}
          />
        </div>
        <p className="state-hint">{t("approvals.countsReflect")}</p>

        {error && (
          <ErrorState
            code={error.code}
            message={error.message || t("errors.loadApprovals")}
            hint={hint ? t(hint) : undefined}
            onRetry={() => void refresh()}
          />
        )}
        {loading && approvals.length === 0 && !error && <LoadingState />}
        {loaded && approvals.length === 0 && !error && (
          <EmptyState title={t("approvals.empty")} hint={t("approvals.emptyHint")} />
        )}

        <div className="approval-list">
          {approvals.map((approval) => {
            const deciding = decisionState?.approvalId === approval.id ? decisionState.decision : null;
            const cardError = decisionError?.approvalId === approval.id ? decisionError.message : null;
            const decidedAt = approval.decided_at ? formatDateTime(approval.decided_at) : "—";
            return (
              <article className="approval-card" key={approval.id}>
                <div className="approval-header">
                  <div>
                    <p className="eyebrow">{t("approvals.card.toolIdentity")}</p>
                    <code>{approval.tool_identity}</code>
                  </div>
                  <Link href={`/runs/${encodeURIComponent(approval.run_id)}`}>
                    {t("approvals.card.runLink", { id: approval.run_id.slice(0, 8) + "…" })}
                  </Link>
                </div>

                <div className="approval-split">
                  <div className="approval-state-block">
                    <span className="approval-state-label">{t("approvals.card.decision")}</span>
                    <StatusBadge status={approval.decision_status} />
                    <span className="approval-state-meta">
                      {approval.decided_by
                        ? t("approvals.card.decidedBy", { time: decidedAt, user: approval.decided_by })
                        : t("approvals.card.decidedAt", { time: decidedAt })}
                    </span>
                    <span className="approval-state-meta">
                      {t("approvals.card.requested", {
                        time: approval.created_at ? formatDateTime(approval.created_at) : "—",
                      })}
                    </span>
                  </div>
                  <div className="approval-state-block">
                    <span className="approval-state-label">{t("approvals.card.execution")}</span>
                    <StatusBadge status={approval.execution_status} />
                    {approval.execution_status === "UNKNOWN_OUTCOME" && (
                      <span className="approval-state-meta">{t("approvals.card.unknownWarning")}</span>
                    )}
                    <span className="approval-state-meta">
                      {t("approvals.card.executed", {
                        time: approval.executed_at ? formatDateTime(approval.executed_at) : "—",
                      })}
                    </span>
                    {approval.execution_attempt_count > 0 && (
                      <span className="approval-state-meta">
                        {t("approvals.card.attempts", { count: approval.execution_attempt_count })}
                      </span>
                    )}
                  </div>
                </div>

                {approval.failure_code && (
                  <p className="state-hint">
                    {approval.safe_failure_message
                      ? t("approvals.card.failure", {
                          code: approval.failure_code,
                          message: approval.safe_failure_message,
                        })
                      : t("approvals.card.failureCodeOnly", { code: approval.failure_code })}
                  </p>
                )}

                {argumentEntries(approval).length > 0 && (
                  <>
                    <KeyValues entries={argumentEntries(approval)} />
                    <TechnicalDetails
                      summary={t("approvals.card.technicalArguments")}
                      value={approval.canonical_arguments}
                    />
                  </>
                )}

                {approval.decision_status === "PENDING" ? (
                  <div className="approval-actions">
                    <button
                      type="button"
                      className="button button-ghost"
                      onClick={() => void decide(approval.id, "deny")}
                      disabled={deciding !== null}
                    >
                      {deciding === "deny" ? t("approvals.card.denying") : t("approvals.card.deny")}
                    </button>
                    <button
                      type="button"
                      className="button button-primary"
                      onClick={() => void decide(approval.id, "approve")}
                      disabled={deciding !== null}
                    >
                      {deciding === "approve" ? t("approvals.card.approving") : t("approvals.card.approve")}
                    </button>
                    {cardError && <p className="state-hint" role="alert">{cardError}</p>}
                  </div>
                ) : (
                  decisionError?.approvalId === approval.id && (
                    <div className="approval-actions">
                      <p className="state-hint" role="alert">{decisionError.message}</p>
                    </div>
                  )
                )}
              </article>
            );
          })}
        </div>
      </Panel>
    </div>
  );
}
