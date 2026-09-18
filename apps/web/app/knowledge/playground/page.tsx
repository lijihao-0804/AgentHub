"use client";

import type { FormEvent } from "react";
import { useState } from "react";

import {
  RetrievalPlaygroundError,
  runRetrievalPlayground,
  type PlaygroundStage,
  type RetrievalPlaygroundResponse,
} from "../../../lib/api";

type PageState = "initial" | "loading" | "success" | "empty" | "error";

const stageLabels: Array<[keyof RetrievalPlaygroundResponse["stages"], string]> = [
  ["dense", "Dense"],
  ["sparse", "Sparse"],
  ["fused", "Fused"],
  ["rerank", "Rerank"],
];

function shortId(value: string | null): string {
  return value ? `${value.slice(0, 8)}…` : "—";
}

function formatLocator(locator: Record<string, unknown> | null): string {
  if (!locator) return "—";
  if (locator.type === "page") return `Page ${String(locator.page)}`;
  if (locator.type === "page_range") {
    return `Pages ${String(locator.page_start)}–${String(locator.page_end)}`;
  }
  if (locator.type === "text_range") {
    return `Characters ${String(locator.char_start)}–${String(locator.char_end)}`;
  }
  return JSON.stringify(locator);
}

function StageCard({ label, stage }: { label: string; stage: PlaygroundStage }) {
  return (
    <section className="playground-stage" aria-labelledby={`${label}-stage`}>
      <div className="playground-stage-header">
        <div>
          <p className="eyebrow">RETRIEVAL STAGE</p>
          <h2 id={`${label}-stage`}>{label}</h2>
        </div>
        <span className="stage-latency">{stage.latency_ms.toFixed(1)} ms</span>
      </div>
      <p className="stage-count">{stage.results.length} results</p>
      <div className="stage-results">
        {stage.results.length === 0 ? (
          <p className="muted">No results</p>
        ) : (
          stage.results.map((item) => (
            <div className="stage-result" key={`${item.chunk_id}-${item.rank}`}>
              <div className="stage-result-topline">
                <strong>#{item.rank}</strong>
                <span>{item.score.toFixed(4)}</span>
              </div>
              <code>{shortId(item.chunk_id)}</code>
              <span className="stage-result-meta">
                rev {shortId(item.document_revision_id)} · {formatLocator(item.locator)}
              </span>
            </div>
          ))
        )}
      </div>
    </section>
  );
}

export default function RetrievalPlaygroundPage() {
  const [workspaceId, setWorkspaceId] = useState("");
  const [knowledgeBaseId, setKnowledgeBaseId] = useState("");
  const [snapshotId, setSnapshotId] = useState("");
  const [query, setQuery] = useState("");
  const [denseTopK, setDenseTopK] = useState("30");
  const [sparseTopK, setSparseTopK] = useState("30");
  const [candidateTopK, setCandidateTopK] = useState("20");
  const [finalTopK, setFinalTopK] = useState("6");
  const [pageState, setPageState] = useState<PageState>("initial");
  const [result, setResult] = useState<RetrievalPlaygroundResponse | null>(null);
  const [error, setError] = useState<{ code: string; message: string } | null>(null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setPageState("loading");
    setResult(null);
    setError(null);
    try {
      const nextResult = await runRetrievalPlayground({
        workspaceId,
        knowledgeBaseId,
        query,
        snapshotId,
        denseTopK: Number(denseTopK),
        sparseTopK: Number(sparseTopK),
        candidateTopK: Number(candidateTopK),
        finalTopK: Number(finalTopK),
      });
      setResult(nextResult);
      setPageState(nextResult.evidence.length === 0 ? "empty" : "success");
    } catch (caught) {
      const nextError =
        caught instanceof RetrievalPlaygroundError
          ? caught
          : new RetrievalPlaygroundError("REQUEST_FAILED", "Retrieval failed.");
      setError({ code: nextError.code, message: nextError.message });
      setPageState("error");
    }
  }

  return (
    <main className="playground-shell">
      <header className="playground-header">
        <div>
          <p className="eyebrow">AGENTHUB · M3-D</p>
          <h1>Retrieval Playground</h1>
          <p className="playground-lede">
            Inspect Dense, Sparse, Fused and Rerank evidence for one concrete knowledge snapshot.
          </p>
        </div>
        <a className="back-link" href="/">
          Back to AgentHub
        </a>
      </header>

      <form className="playground-panel query-panel" onSubmit={handleSubmit}>
        <div className="panel-heading">
          <div>
            <p className="eyebrow">QUERY PANEL</p>
            <h2>Run snapshot-scoped retrieval</h2>
          </div>
          <span className="badge">Developer tool</span>
        </div>
        <p className="playground-note">
          M3-D requires a concrete Snapshot ID. LATEST and snapshot lifecycle arrive in M3-F.
        </p>
        <div className="form-grid">
          <label>
            Workspace ID
            <input required value={workspaceId} onChange={(event) => setWorkspaceId(event.target.value)} />
          </label>
          <label>
            Knowledge Base ID
            <input
              required
              value={knowledgeBaseId}
              onChange={(event) => setKnowledgeBaseId(event.target.value)}
            />
          </label>
          <label>
            Snapshot ID
            <input required value={snapshotId} onChange={(event) => setSnapshotId(event.target.value)} />
          </label>
          <label className="query-field">
            Query
            <textarea
              required
              rows={4}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Search the selected snapshot…"
            />
          </label>
        </div>
        <details className="advanced-options">
          <summary>Advanced top-k limits</summary>
          <div className="form-grid top-k-grid">
            <label>
              Dense Top K
              <input type="number" min={1} max={100} value={denseTopK} onChange={(event) => setDenseTopK(event.target.value)} />
            </label>
            <label>
              Sparse Top K
              <input type="number" min={1} max={100} value={sparseTopK} onChange={(event) => setSparseTopK(event.target.value)} />
            </label>
            <label>
              Candidate Top K
              <input type="number" min={1} max={100} value={candidateTopK} onChange={(event) => setCandidateTopK(event.target.value)} />
            </label>
            <label>
              Final Top K
              <input type="number" min={1} max={20} value={finalTopK} onChange={(event) => setFinalTopK(event.target.value)} />
            </label>
          </div>
        </details>
        <div className="form-actions">
          <button type="submit" disabled={pageState === "loading"}>
            {pageState === "loading" ? "Retrieving…" : "Run Retrieval"}
          </button>
        </div>
      </form>

      {pageState === "initial" && <p className="state-message">Enter a query and snapshot to begin.</p>}
      {pageState === "loading" && <p className="state-message">Retrieving…</p>}
      {pageState === "error" && error && (
        <section className="error-panel" role="alert">
          <strong>{error.code}</strong>
          <span>{error.message}</span>
        </section>
      )}
      {pageState === "empty" && <p className="state-message">No evidence found for this snapshot.</p>}

      {result && (
        <>
          <section className="playground-overview" aria-label="Retrieval summary">
            <div>
              <span>Snapshot</span>
              <strong>{shortId(result.snapshot_id)}</strong>
            </div>
            <div>
              <span>Total</span>
              <strong>{result.total_latency_ms.toFixed(1)} ms</strong>
            </div>
            <div>
              <span>Final</span>
              <strong>{result.evidence.length} chunks</strong>
            </div>
          </section>
          <section className="stage-grid" aria-label="Retrieval trace stages">
            {stageLabels.map(([key, label]) => (
              <StageCard key={key} label={label} stage={result.stages[key]} />
            ))}
          </section>
          <section className="playground-panel evidence-panel">
            <div className="panel-heading">
              <div>
                <p className="eyebrow">FINAL EVIDENCE</p>
                <h2>Reranked chunks</h2>
              </div>
              <span className="stage-count">{result.evidence.length} chunks</span>
            </div>
            <div className="evidence-list">
              {result.evidence.map((item, index) => (
                <article className="evidence-item" key={item.chunk_id}>
                  <div className="evidence-rank">#{index + 1}</div>
                  <div className="evidence-body">
                    <div className="evidence-meta">
                      <strong>{item.source}</strong>
                      <span>{formatLocator(item.locator)}</span>
                      <span>retrieval {item.retrieval_score.toFixed(4)}</span>
                      <span>rerank {item.rerank_score?.toFixed(4) ?? "—"}</span>
                    </div>
                    <p>{item.snippet}</p>
                  </div>
                </article>
              ))}
            </div>
          </section>
        </>
      )}
    </main>
  );
}
