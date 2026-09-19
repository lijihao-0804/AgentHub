"use client";

import Link from "next/link";

import { useFrontendSession } from "../components/session-provider";

const QUICK_LINKS = [
  {
    href: "/dashboard",
    title: "Dashboard",
    description: "Success rate, latency, usage, cost and failure analytics for this workspace.",
  },
  {
    href: "/runs",
    title: "Runs",
    description: "Workspace-scoped run history with safe status, usage and cost projections.",
  },
  {
    href: "/approvals",
    title: "Approvals",
    description: "Review pending tool approvals and reconcile actions that need attention.",
  },
  {
    href: "/knowledge/playground",
    title: "Retrieval Playground",
    description: "Inspect dense, sparse, fused and reranked evidence for one knowledge snapshot.",
  },
];

export default function Home() {
  const { connected, workspaceId, openPanel } = useFrontendSession();

  return (
    <div className="page">
      <header className="page-header">
        <p className="eyebrow">OVERVIEW</p>
        <h1>Workspace overview</h1>
        <p className="page-lede">
          Operational control plane for running, approving and observing enterprise agents.
        </p>
      </header>

      <section className="panel" aria-labelledby="session-overview-title">
        <div className="panel-heading">
          <div>
            <p className="eyebrow">SESSION</p>
            <h2 id="session-overview-title">Workspace connection</h2>
          </div>
        </div>
        {connected ? (
          <p className="state-hint">
            Session active for workspace <code>{workspaceId.slice(0, 4)}…{workspaceId.slice(-3)}</code>.
            The token stays in memory and is cleared when you refresh.
          </p>
        ) : (
          <div className="state-block state-inline">
            <p className="state-title">Not connected</p>
            <p className="state-hint">
              Connect a workspace session to load dashboards, runs and approvals. Credentials stay
              in memory only — nothing is persisted.
            </p>
            <button type="button" className="button button-primary" onClick={openPanel}>
              Connect workspace
            </button>
          </div>
        )}
      </section>

      <section className="overview-grid" aria-label="Quick navigation">
        {QUICK_LINKS.map((item) => (
          <Link className="overview-card" href={item.href} key={item.href}>
            <span className="overview-card-title">{item.title}</span>
            <span className="overview-card-description">{item.description}</span>
            <span className="overview-card-cta" aria-hidden="true">
              Open →
            </span>
          </Link>
        ))}
      </section>
    </div>
  );
}
