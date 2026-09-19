"use client";

import { useState } from "react";
import Link from "next/link";

import {
  Approval,
  ApprovalApiError,
  decideApproval,
  listApprovals,
} from "../lib/approvals";

const milestones = [
  ["M0", "Engineering baseline", "next"],
  ["M1", "Auth / Tenant / RBAC", "next"],
  ["M2", "Model Gateway", "next"],
  ["M3", "Reliable Knowledge Hub", "next"],
  ["M5-A", "Approval Runtime", "complete"],
  ["M6-A", "Run Observability", "active"],
];

export default function Home() {
  const [workspaceId, setWorkspaceId] = useState("");
  const [accessToken, setAccessToken] = useState("");
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [message, setMessage] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function refreshApprovals() {
    setMessage(null);
    if (!workspaceId.trim() || !accessToken.trim()) {
      setMessage("Workspace ID and access token are required.");
      return;
    }
    setLoading(true);
    try {
      setApprovals(await listApprovals(workspaceId, accessToken));
    } catch (error) {
      setMessage(error instanceof ApprovalApiError ? error.message : "Could not load approvals.");
    } finally {
      setLoading(false);
    }
  }

  async function decide(approvalId: string, decision: "approve" | "deny") {
    setMessage(null);
    setLoading(true);
    try {
      const result = await decideApproval(workspaceId, approvalId, decision, accessToken);
      setApprovals((current) =>
        current.map((approval) => (approval.id === approvalId ? result.approval : approval)),
      );
    } catch (error) {
      setMessage(error instanceof ApprovalApiError ? error.message : "Could not decide approval.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <main className="shell">
      <section className="hero">
        <p className="eyebrow">ENTERPRISE AGENT RUNTIME &amp; CONTROL PLANE</p>
        <h1>AgentHub</h1>
        <p className="lede">
          A staged foundation for explainable, durable and evaluable enterprise agents.
        </p>
      </section>
      <section className="card" aria-labelledby="status-title">
        <div className="card-header">
          <div>
            <p className="eyebrow">DELIVERY STATUS</p>
            <h2 id="status-title">Milestone roadmap</h2>
          </div>
          <span className="badge">M6-A IN PROGRESS</span>
        </div>
        <div className="milestones">
          {milestones.map(([id, label, status]) => (
            <div className={`milestone ${status}`} key={id}>
              <span className="milestone-id">{id}</span>
              <span>{label}</span>
              <span className="milestone-status">
                {status === "active" ? "Active" : status === "complete" ? "Accepted" : "Queued"}
              </span>
            </div>
          ))}
        </div>
      </section>
      <section className="card approval-card" aria-labelledby="approval-title">
        <div className="card-header">
          <div>
            <p className="eyebrow">DURABLE HUMAN-IN-THE-LOOP</p>
            <h2 id="approval-title">Approval inbox</h2>
          </div>
          <span className="badge">NO TOKEN STORAGE</span>
        </div>
        <p className="playground-note">
          Developer-facing MVP surface. The token stays in this React state only and disappears on refresh.
        </p>
        <div className="approval-form">
          <label>
            Workspace ID
            <input value={workspaceId} onChange={(event) => setWorkspaceId(event.target.value)} />
          </label>
          <label>
            Access token
            <input
              type="password"
              value={accessToken}
              onChange={(event) => setAccessToken(event.target.value)}
            />
          </label>
          <button type="button" onClick={refreshApprovals} disabled={loading}>
            {loading ? "Loading…" : "Refresh approvals"}
          </button>
        </div>
        {message && <p className="state-message">{message}</p>}
        <div className="approval-list">
          {approvals.length === 0 && !message && (
            <p className="muted">No approvals loaded.</p>
          )}
          {approvals.map((approval) => (
            <article className="approval-item" key={approval.id}>
              <div className="approval-item-header">
                <div>
                  <p className="eyebrow">{approval.tool_identity}</p>
                  <strong>{approval.decision_status}</strong>
                </div>
                <span className="approval-status">{approval.execution_status}</span>
              </div>
              <p className="muted">Run {approval.run_id}</p>
              <pre>{JSON.stringify(approval.canonical_arguments, null, 2)}</pre>
              {approval.execution_status === "UNKNOWN_OUTCOME" && (
                <p className="needs-attention">Needs Attention: reconciliation required.</p>
              )}
              {approval.decision_status === "PENDING" && (
                <div className="approval-actions">
                  <button type="button" onClick={() => decide(approval.id, "approve")} disabled={loading}>
                    Approve
                  </button>
                  <button type="button" onClick={() => decide(approval.id, "deny")} disabled={loading}>
                    Deny
                  </button>
                </div>
              )}
            </article>
          ))}
        </div>
      </section>
      <section className="card" aria-labelledby="runs-title">
        <div className="card-header">
          <div>
            <p className="eyebrow">M6-A OBSERVABILITY</p>
            <h2 id="runs-title">Run query and timeline</h2>
          </div>
          <Link className="run-detail-link" href="/runs">
            Open Runs →
          </Link>
        </div>
        <p className="playground-note">
          Explore workspace-scoped runtime status, usage, cost, approvals and safe timeline events.
        </p>
      </section>
    </main>
  );
}
