"use client";

import type { FormEvent } from "react";
import { useState } from "react";

import { EmptyState, ErrorState, LoadingState, Panel, SessionRequired } from "../../../components/states";
import {
  RetrievalPlaygroundError,
  runRetrievalPlayground,
  type PlaygroundStage,
  type RetrievalPlaygroundResponse,
} from "../../../lib/api";
import { formatLocator } from "../../../lib/locator";
import { useFrontendSession } from "../../../components/session-provider";

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

function StageCard({ label, stage }: { label: string; stage: PlaygroundStage }) {
  return (
    <section className="playground-stage" aria-labelledby={`${label}-stage`}>
      <div className="playground-stage-header">
        <div>
          <p className="eyebrow">STAGE</p>
          <h3 id={`${label}-stage`}>{label}</h3>
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
  const { workspaceId, accessToken, connected } = useFrontendSession();
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
    setResult(null);
    setError(null);
    if (!connected) {
      setError({
        code: "SESSION_REQUIRED",
        message: "Configure a workspace session to run the playground.",
      });
      setPageState("error");
      return;
    }
    if (!knowledgeBaseId.trim() || !snapshotId.trim() || !query.trim()) {
      setError({ code: "INVALID_REQUEST", message: "Complete the required fields first." });
      setPageState("error");
      return;
    }
    setPageState("loading");
    try {
      const nextResult = await runRetrievalPlayground({
        workspaceId,
        knowledgeBaseId,
        accessToken,
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

  if (!connected) {
    return (
      <div className="page">
        <header className="page-header">
          <p className="eyebrow">KNOWLEDGE</p>
          <h1>Retrieval Playground</h1>
          <p className="page-lede">
            Inspect dense, sparse, fused and reranked evidence for one concrete knowledge snapshot.
          </p>
        </header>
        <SessionRequired context="the retrieval playground" />
      </div>
    );
  }

  return (
    <div className="page">
      <header className="page-header">
        <p className="eyebrow">KNOWLEDGE</p>
        <h1>Retrieval Playground</h1>
        <p className="page-lede">
          Inspect dense, sparse, fused and reranked evidence for one concrete knowledge snapshot.
          Retrieval runs against the workspace from the active session.
        </p>
      </header>

      <form className="panel query-panel" onSubmit={handleSubmit} noValidate>
        <div className="panel-heading">
          <div>
            <p className="eyebrow">QUERY</p>
            <h2>Run snapshot-scoped retrieval</h2>
          </div>
        </div>
        <div className="form-grid">
          <label>
            Knowledge Base ID
            <input
              required
              value={knowledgeBaseId}
              onChange={(event) => setKnowledgeBaseId(event.target.value)}
              spellCheck={false}
            />
          </label>
          <label>
            Snapshot ID
            <input required value={snapshotId} onChange={(event) => setSnapshotId(event.target.value)} spellCheck={false} />
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
          <summary>Advanced retrieval config</summary>
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
          <button type="submit" className="button button-primary" disabled={pageState === "loading"}>
            {pageState === "loading" ? "Retrieving…" : "Run retrieval"}
          </button>
        </div>
      </form>

      {pageState === "initial" && <EmptyState title="Enter a query and snapshot to begin." />}
      {pageState === "loading" && <LoadingState label="Retrieving…" />}
      {pageState === "error" && error && (
        <ErrorState code={error.code} message={error.message} />
      )}
      {pageState === "empty" && (
        <EmptyState title="No evidence found for this snapshot." hint="The retrieval stages ran but returned no chunks." />
      )}

      {result && (
        <>
          <section className="playground-overview" aria-label="Retrieval summary">
            <div>
              <span>Snapshot</span>
              <strong>{shortId(result.snapshot_id)}</strong>
            </div>
            <div>
              <span>Total latency</span>
              <strong>{result.total_latency_ms.toFixed(1)} ms</strong>
            </div>
            <div>
              <span>Final evidence</span>
              <strong>{result.evidence.length} chunks</strong>
            </div>
          </section>
          <section className="stage-grid" aria-label="Retrieval trace stages">
            {stageLabels.map(([key, label]) => (
              <StageCard key={key} label={label} stage={result.stages[key]} />
            ))}
          </section>
          <Panel title="Reranked evidence" eyebrow="FINAL EVIDENCE" actions={<span className="state-hint">{result.evidence.length} chunks</span>}>
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
          </Panel>
        </>
      )}
    </div>
  );
}
