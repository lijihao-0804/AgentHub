"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import StatusBadge from "@/components/ui/status-badge";
import { EmptyState, ErrorState, InlineError, LoadingState, Panel, SessionRequired } from "@/components/ui/states";
import { KeyValues } from "@/components/ui/technical-details";
import TechnicalDetails from "@/components/ui/technical-details";
import { ApiError, errorHintKey, toApiError } from "@/lib/api/client";
import { Approval, decideApproval, listApprovals } from "@/lib/api/approvals";
import { useFrontendSession } from "@/components/providers/session-provider";
import { useI18n } from "@/i18n/provider";

type DecisionState = { approvalId: string; decision: "approve" | "deny" } | null;

/** Poll cadence for the approvals inbox while decisions are pending. */
const APPROVALS_POLL_MS = 5000;
const EXPIRY_CLOCK_TOLERANCE_MS = 60_000;

/** Client-side counts over the currently loaded list; no aggregate API exists. */
function countSummary(approvals: Approval[]) {
  const pending = approvals.filter((a) => a.decision_status === "PENDING").length;
  const approved = approvals.filter((a) => a.decision_status === "APPROVED").length;
  const denied = approvals.filter((a) => a.decision_status === "DENIED").length;
  const expired = approvals.filter((a) => a.decision_status === "EXPIRED").length;
  const needsAttention = approvals.filter((a) => a.execution_status === "UNKNOWN_OUTCOME").length;
  return { pending, approved, denied, expired, needsAttention };
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
  const { t, formatDateTime, formatDurationMs } = useI18n();
  const { workspaceId, accessToken, connected, permissions, sessionId } = useFrontendSession();
  const cannotDecide = permissions !== null && !permissions.includes("approve_action");
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [error, setError] = useState<ApiError | null>(null);
  const [decisionError, setDecisionError] = useState<{ approvalId: string; error: ApiError } | null>(null);
  const [decisionState, setDecisionState] = useState<DecisionState>(null);
  const [lastDecisionId, setLastDecisionId] = useState<string | null>(null);
  /** The approval currently showing its deny-reason prompt, if any. */
  const [denyPromptId, setDenyPromptId] = useState<string | null>(null);
  const [denyReason, setDenyReason] = useState("");
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [clockNowMs, setClockNowMs] = useState(() => Date.now());
  /** Which inbox slice is on display; server-side filter via ?decision=. */
  const [decisionTab, setDecisionTab] = useState<"PENDING" | "ALL">("PENDING");
  const [total, setTotal] = useState<number | null>(null);
  /** Generation of the list request; a stale workspace's response never lands. */
  const generationRef = useRef(0);
  const activeSessionRef = useRef(sessionId);
  activeSessionRef.current = sessionId;
  useEffect(() => {
    generationRef.current += 1;
    pagedRef.current = false;
    setApprovals([]);
    setTotal(null);
    setLoaded(false);
    setLoading(false);
    setError(null);
    setDecisionState(null);
    setDecisionError(null);
    setDenyPromptId(null);
    setDenyReason("");
    setLastDecisionId(null);
  }, [sessionId]);
  /** True once the user paged past the first page; polling then pauses so a
   * refresh never collapses an expanded list back to page one. */
  const pagedRef = useRef(false);
  const PAGE_SIZE = 50;
  /** Live workspace identity, for guarding decision write-backs. */
  const workspaceRef = useRef(workspaceId);
  workspaceRef.current = workspaceId;
  const hasPendingExpirations = approvals.some(
    (approval) => approval.decision_status === "PENDING" && approval.expires_at,
  );

  useEffect(() => {
    if (!hasPendingExpirations) return;
    const refreshClock = () => setClockNowMs(Date.now());
    const interval = window.setInterval(refreshClock, 30_000);
    document.addEventListener("visibilitychange", refreshClock);
    return () => {
      window.clearInterval(interval);
      document.removeEventListener("visibilitychange", refreshClock);
    };
  }, [hasPendingExpirations]);

  const refresh = useCallback(async () => {
    if (!connected) return;
    const generation = (generationRef.current += 1);
    const requestSessionId = sessionId;
    setError(null);
    setLoading(true);
    try {
      const page = await listApprovals(workspaceId, accessToken, {
        decision: decisionTab === "PENDING" ? "PENDING" : undefined,
        limit: PAGE_SIZE,
        offset: 0,
      });
      if (generationRef.current !== generation || activeSessionRef.current !== requestSessionId) return;
      pagedRef.current = false;
      setApprovals(page.items);
      setTotal(page.total);
      setLoaded(true);
    } catch (caught) {
      if (generationRef.current !== generation || activeSessionRef.current !== requestSessionId) return;
      setError(toApiError(caught, ""));
    } finally {
      if (generationRef.current === generation && activeSessionRef.current === requestSessionId) setLoading(false);
    }
  }, [connected, workspaceId, accessToken, decisionTab, sessionId]);

  const loadMore = useCallback(async () => {
    if (!connected) return;
    const generation = (generationRef.current += 1);
    const requestSessionId = sessionId;
    setError(null);
    setLoading(true);
    try {
      const page = await listApprovals(workspaceId, accessToken, {
        decision: decisionTab === "PENDING" ? "PENDING" : undefined,
        limit: PAGE_SIZE,
        offset: approvals.length,
      });
      if (generationRef.current !== generation || activeSessionRef.current !== requestSessionId) return;
      pagedRef.current = true;
      setApprovals((current) => [...current, ...page.items]);
      setTotal(page.total);
      setLoaded(true);
    } catch (caught) {
      if (generationRef.current !== generation || activeSessionRef.current !== requestSessionId) return;
      setError(toApiError(caught, ""));
    } finally {
      if (generationRef.current === generation && activeSessionRef.current === requestSessionId) setLoading(false);
    }
  }, [connected, workspaceId, accessToken, decisionTab, approvals.length, sessionId]);

  function switchDecisionTab(tab: "PENDING" | "ALL") {
    if (tab === decisionTab) return;
    pagedRef.current = false;
    setDecisionTab(tab);
    setApprovals([]);
    setTotal(null);
    setLoaded(false);
  }

  useEffect(() => {
    if (connected && !loaded) void refresh();
  }, [connected, loaded, refresh]);

  useEffect(() => {
    if (!connected) setLoaded(false);
  }, [connected]);

  // Poll even when the inbox is currently empty: otherwise the first new
  // approval would never be discovered while this page stays open. Once the
  // user has paged deeper, polling pauses — a page-one refresh would throw
  // away the loaded history.
  useEffect(() => {
    if (!connected) return;
    const interval = window.setInterval(() => {
      if (!document.hidden && !pagedRef.current) void refresh();
    }, APPROVALS_POLL_MS);
    const onVisible = () => {
      if (!document.hidden) void refresh();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(interval);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [connected, refresh]);

  async function decide(approvalId: string, decision: "approve" | "deny", reason?: string) {
    setDecisionError(null);
    setLastDecisionId(null);
    setDecisionState({ approvalId, decision });
    const requestWorkspace = workspaceRef.current;
    const requestSessionId = sessionId;
    try {
      const result = await decideApproval(workspaceId, approvalId, decision, accessToken, { reason });
      if (workspaceRef.current !== requestWorkspace || activeSessionRef.current !== requestSessionId) return;
      // An inbox poll that started before this decision must not overwrite its
      // result with the old PENDING row.
      generationRef.current += 1;
      setLoading(false);
      setApprovals((current) =>
        current.map((approval) => (approval.id === approvalId ? result.approval : approval)),
      );
      setLastDecisionId(approvalId);
    } catch (caught) {
      if (workspaceRef.current !== requestWorkspace || activeSessionRef.current !== requestSessionId) return;
      const apiError = toApiError(caught, "");
      setDecisionError({ approvalId, error: apiError });
    } finally {
      if (workspaceRef.current === requestWorkspace && activeSessionRef.current === requestSessionId) setDecisionState(null);
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
              {loading
                ? t("common.loading")
                : total !== null
                  ? t("approvals.loadedOf", { loaded: approvals.length, total })
                  : t("approvals.loaded", { count: approvals.length })}
            </span>
            <button type="button" className="button button-ghost" onClick={() => void refresh()} disabled={loading}>
              {t("common.refresh")}
            </button>
          </>
        }
      >
        <div className="tab-strip" role="tablist" aria-label={t("approvals.inboxTitle")}>
          <button
            type="button"
            aria-selected={decisionTab === "PENDING"}
            onClick={() => switchDecisionTab("PENDING")}
          >
            {t("approvals.tabPending")}
          </button>
          <button
            type="button"
            aria-selected={decisionTab === "ALL"}
            onClick={() => switchDecisionTab("ALL")}
          >
            {t("approvals.tabAll")}
          </button>
        </div>
        <div className="approval-counts">
          <StatusBadge status="PENDING" label={t("approvals.counts.pending", { count: counts.pending })} />
          <StatusBadge status="APPROVED" label={t("approvals.counts.approved", { count: counts.approved })} />
          <StatusBadge status="DENIED" label={t("approvals.counts.denied", { count: counts.denied })} />
          <StatusBadge status="EXPIRED" label={t("approvals.counts.expired", { count: counts.expired })} />
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
            const cardError = decisionError?.approvalId === approval.id ? decisionError.error : null;
            const decidedAt = approval.decided_at ? formatDateTime(approval.decided_at) : "—";
            const expiresAtMs = approval.expires_at ? Date.parse(approval.expires_at) : Number.NaN;
            const expiryElapsed = approval.decision_status === "PENDING" &&
              Number.isFinite(expiresAtMs) &&
              expiresAtMs + EXPIRY_CLOCK_TOLERANCE_MS <= clockNowMs;
            /** Inside five minutes the countdown turns warning-colored so an
             * operator scanning the inbox sees urgency without reading. */
            const expiringSoon = approval.decision_status === "PENDING" &&
              Number.isFinite(expiresAtMs) &&
              !expiryElapsed &&
              expiresAtMs - clockNowMs <= 5 * 60_000;
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
                    {approval.decision_status === "PENDING" && Number.isFinite(expiresAtMs) && (
                      <span
                        className={`approval-state-meta${expiringSoon ? " approval-state-expiring" : ""}`}
                        aria-live="off"
                        title={t("approvals.card.expiryClockHint")}
                      >
                        {expiryElapsed
                          ? t("approvals.card.expiryElapsed")
                          : t("approvals.card.expiresIn", {
                              duration: formatDurationMs(Math.max(0, expiresAtMs - clockNowMs)),
                            })}
                      </span>
                    )}
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

                {lastDecisionId === approval.id && approval.decision_status !== "PENDING" && (
                  <p className="inline-notice" role="status">
                    {t("approvals.card.decisionSaved")} {" "}
                    <Link href={`/runs/${encodeURIComponent(approval.run_id)}`}>
                      {t("approvals.card.returnToRun")}
                    </Link>
                  </p>
                )}

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
                    {cannotDecide && <p className="state-hint" role="note">{t("approvals.noPermissionHint")}</p>}
                    {denyPromptId === approval.id ? (
                      <div className="inline-confirm" role="alertdialog" aria-label={t("approvals.denyReasonTitle")}>
                        <p className="state-title">{t("approvals.denyReasonTitle")}</p>
                        <textarea
                          value={denyReason}
                          onChange={(event) => setDenyReason(event.target.value)}
                          placeholder={t("approvals.denyReasonPlaceholder")}
                          rows={3}
                          maxLength={500}
                          aria-label={t("approvals.denyReasonTitle")}
                        />
                        <p className="state-hint">{t("approvals.denyReasonHint")}</p>
                        <div className="inline-confirm-actions">
                          <button
                            type="button"
                            className="button button-danger"
                            disabled={deciding !== null || denyReason.trim().length === 0}
                            onClick={() => {
                              const reason = denyReason.trim();
                              setDenyPromptId(null);
                              setDenyReason("");
                              void decide(approval.id, "deny", reason);
                            }}
                          >
                            {deciding === "deny" ? t("approvals.card.denying") : t("approvals.confirmDeny")}
                          </button>
                          <button
                            type="button"
                            className="button button-ghost"
                            onClick={() => {
                              setDenyPromptId(null);
                              setDenyReason("");
                            }}
                            disabled={deciding !== null}
                          >
                            {t("common.cancel")}
                          </button>
                        </div>
                      </div>
                    ) : (
                      <button
                        type="button"
                        className="button button-ghost"
                        onClick={() => {
                          setDenyPromptId(approval.id);
                          setDenyReason("");
                        }}
                        disabled={deciding !== null || cannotDecide || expiryElapsed}
                        title={expiryElapsed ? t("approvals.card.expiryElapsed") : cannotDecide ? t("approvals.noPermissionHint") : undefined}
                      >
                        {deciding === "deny" ? t("approvals.card.denying") : t("approvals.card.deny")}
                      </button>
                    )}
                    <button
                      type="button"
                      className="button button-primary"
                      onClick={() => void decide(approval.id, "approve")}
                      disabled={deciding !== null || cannotDecide || expiryElapsed}
                      title={expiryElapsed ? t("approvals.card.expiryElapsed") : cannotDecide ? t("approvals.noPermissionHint") : undefined}
                    >
                      {deciding === "approve" ? t("approvals.card.approving") : t("approvals.card.approve")}
                    </button>
                    <InlineError error={cardError} fallback={t("errors.decideApproval")} />
                  </div>
                ) : (
                  decisionError?.approvalId === approval.id && (
                    <div className="approval-actions">
                      <InlineError error={decisionError.error} fallback={t("errors.decideApproval")} />
                    </div>
                  )
                )}
              </article>
            );
          })}
        </div>
        {loaded && !error && total !== null && approvals.length < total && (
          <div className="form-actions">
            <button type="button" className="button button-ghost" onClick={() => void loadMore()} disabled={loading}>
              {t("approvals.loadMore")}
            </button>
          </div>
        )}
      </Panel>
    </div>
  );
}
