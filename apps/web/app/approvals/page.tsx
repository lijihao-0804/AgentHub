"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import StatusBadge from "../../components/status-badge";
import { EmptyState, ErrorState, LoadingState, Panel, SessionRequired } from "../../components/states";
import { KeyValues } from "../../components/technical-details";
import TechnicalDetails from "../../components/technical-details";
import { ApiError, errorHint, toApiError } from "../../lib/api-client";
import { Approval, decideApproval, listApprovals } from "../../lib/approvals";
import { useFrontendSession } from "../../components/session-provider";

type DecisionState = { approvalId: string; decision: "approve" | "deny" } | null;

function formatTimestamp(value: string | null): string {
  return value ? new Date(value).toLocaleString() : "—";
}

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
    if (value === null || value === undefined || typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
      return [key, value] as [string, unknown];
    }
    return [key, JSON.stringify(value)] as [string, unknown];
  });
}

export default function ApprovalsPage() {
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
      setError(toApiError(caught, "Could not load approvals."));
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
      const apiError = toApiError(caught, "Could not record the approval decision.");
      setDecisionError({ approvalId, message: `${apiError.code}: ${apiError.message}` });
    } finally {
      setDecisionState(null);
    }
  }

  if (!connected) {
    return (
      <div className="page">
        <header className="page-header">
          <p className="eyebrow">APPROVALS</p>
          <h1>Approvals</h1>
          <p className="page-lede">
            Durable human-in-the-loop decisions for tool actions. Decision and execution states are
            tracked separately.
          </p>
        </header>
        <SessionRequired context="the approval inbox" />
      </div>
    );
  }

  const counts = countSummary(approvals);

  return (
    <div className="page">
      <header className="page-header">
        <p className="eyebrow">APPROVALS</p>
        <h1>Approvals</h1>
        <p className="page-lede">
          Durable human-in-the-loop decisions for tool actions. Decision and execution states are
          tracked separately — an approved action can still fail, and an unknown outcome needs
          attention rather than a retry.
        </p>
      </header>

      <Panel
        ariaLabel="Approval inbox"
        title="Approval inbox"
        eyebrow="HUMAN-IN-THE-LOOP"
        actions={
          <>
            <span className="state-hint" aria-live="polite">
              {loading ? "Loading…" : `${approvals.length} loaded`}
            </span>
            <button type="button" className="button button-ghost" onClick={() => void refresh()} disabled={loading}>
              Refresh
            </button>
          </>
        }
      >
        <div className="approval-counts">
          <StatusBadge status="PENDING" label={`Pending ${counts.pending}`} />
          <StatusBadge status="APPROVED" label={`Approved ${counts.approved}`} />
          <StatusBadge status="DENIED" label={`Denied ${counts.denied}`} />
          <StatusBadge status="UNKNOWN_OUTCOME" label={`Needs attention ${counts.needsAttention}`} />
        </div>
        <p className="state-hint">Counts reflect the currently loaded list.</p>

        {error && (
          <ErrorState code={error.code} message={error.message} hint={errorHint(error)} onRetry={() => void refresh()} />
        )}
        {loading && approvals.length === 0 && !error && <LoadingState label="Loading approvals…" />}
        {loaded && approvals.length === 0 && !error && (
          <EmptyState
            title="No approvals in this workspace."
            hint="Pending approval requests appear here when an agent requests a risky tool action."
          />
        )}

        <div className="approval-list">
          {approvals.map((approval) => {
            const deciding = decisionState?.approvalId === approval.id ? decisionState.decision : null;
            const cardError = decisionError?.approvalId === approval.id ? decisionError.message : null;
            return (
              <article className="approval-card" key={approval.id}>
                <div className="approval-header">
                  <div>
                    <p className="eyebrow">TOOL IDENTITY</p>
                    <code>{approval.tool_identity}</code>
                  </div>
                  <Link href={`/runs/${encodeURIComponent(approval.run_id)}`}>
                    Run {approval.run_id.slice(0, 8)}… →
                  </Link>
                </div>

                <div className="approval-split">
                  <div className="approval-state-block">
                    <span className="approval-state-label">Decision</span>
                    <StatusBadge status={approval.decision_status} />
                    <span className="approval-state-meta">
                      Decided {formatTimestamp(approval.decided_at)}
                      {approval.decided_by ? ` by ${approval.decided_by}` : ""}
                    </span>
                    <span className="approval-state-meta">Requested {formatTimestamp(approval.created_at)}</span>
                  </div>
                  <div className="approval-state-block">
                    <span className="approval-state-label">Execution</span>
                    <StatusBadge status={approval.execution_status} />
                    {approval.execution_status === "UNKNOWN_OUTCOME" && (
                      <span className="approval-state-meta">
                        Needs attention: the action ran but its outcome could not be confirmed. Do not retry blindly.
                      </span>
                    )}
                    <span className="approval-state-meta">Executed {formatTimestamp(approval.executed_at)}</span>
                    {approval.execution_attempt_count > 0 && (
                      <span className="approval-state-meta">Attempts: {approval.execution_attempt_count}</span>
                    )}
                  </div>
                </div>

                {approval.failure_code && (
                  <p className="state-hint">
                    Failure <code>{approval.failure_code}</code>
                    {approval.safe_failure_message ? ` — ${approval.safe_failure_message}` : ""}
                  </p>
                )}

                {argumentEntries(approval).length > 0 && (
                  <>
                    <KeyValues entries={argumentEntries(approval)} />
                    <TechnicalDetails
                      summary="Technical arguments"
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
                      {deciding === "deny" ? "Denying…" : "Deny"}
                    </button>
                    <button
                      type="button"
                      className="button button-primary"
                      onClick={() => void decide(approval.id, "approve")}
                      disabled={deciding !== null}
                    >
                      {deciding === "approve" ? "Approving…" : "Approve"}
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
