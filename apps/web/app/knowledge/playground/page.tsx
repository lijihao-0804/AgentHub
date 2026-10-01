"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";

import { EmptyState, ErrorState, LoadingState, Panel, SessionRequired } from "@/components/ui/states";
import {
  RetrievalPlaygroundError,
  runRetrievalPlayground,
  type PlaygroundStage,
  type RetrievalPlaygroundResponse,
} from "@/lib/api/retrieval-playground";
import { formatLocator } from "@/lib/format/locator";
import { listKnowledgeBases, listSnapshots, type KnowledgeBase, type KnowledgeSnapshot } from "@/lib/api/knowledge";
import type { AuthInput } from "@/lib/api/client";
import { useFrontendSession } from "@/components/providers/session-provider";
import { useWorkspaceData } from "@/hooks/use-workspace-data";
import { useI18n } from "@/i18n/provider";

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
  const { t, formatNumber } = useI18n();
  return (
    <section className="playground-stage" aria-labelledby={`${label}-stage`}>
      <div className="playground-stage-header">
        <div>
          <p className="eyebrow">{t("playground.stage.eyebrow")}</p>
          <h3 id={`${label}-stage`}>{label}</h3>
        </div>
        {/* Stage latency and scores stay in a neutral dot-decimal format for
            developer comparison across locales. */}
        <span className="stage-latency">{stage.latency_ms.toFixed(1)} ms</span>
      </div>
      <p className="stage-count">{t("playground.stage.results", { count: formatNumber(stage.results.length) })}</p>
      <div className="stage-results">
        {stage.results.length === 0 ? (
          <p className="muted">{t("playground.stage.noResults")}</p>
        ) : (
          stage.results.map((item) => (
            <div className="stage-result" key={`${item.chunk_id}-${item.rank}`}>
              <div className="stage-result-topline">
                <strong>#{item.rank}</strong>
                <span>{item.score.toFixed(4)}</span>
              </div>
              <code>{shortId(item.chunk_id)}</code>
              <span className="stage-result-meta">
                {t("playground.stage.revision", { id: shortId(item.document_revision_id) })} ·{" "}
                {formatLocator(item.locator, t)}
              </span>
            </div>
          ))
        )}
      </div>
    </section>
  );
}

export default function RetrievalPlaygroundPage() {
  const { t, formatNumber, formatDateTime } = useI18n();
  const { workspaceId, accessToken, connected, sessionId } = useFrontendSession();
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

  const loadKnowledgeBases = useCallback((auth: AuthInput) => listKnowledgeBases(auth), []);
  const knowledgeBases = useWorkspaceData<KnowledgeBase[]>(
    loadKnowledgeBases,
    `retrieval-playground:bases:${workspaceId}`,
  );
  const loadSnapshots = useCallback(
    (auth: AuthInput) => listSnapshots(auth, knowledgeBaseId),
    [knowledgeBaseId],
  );
  const snapshots = useWorkspaceData<KnowledgeSnapshot[]>(
    loadSnapshots,
    knowledgeBaseId ? `retrieval-playground:snapshots:${workspaceId}:${knowledgeBaseId}` : "",
    { enabled: Boolean(knowledgeBaseId) },
  );

  // Deep links from a knowledge base (or snapshot) page preselect the target:
  // arriving with the fields blank throws away the context the user came from.
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const kb = params.get("knowledge_base_id");
    if (kb) setKnowledgeBaseId(kb);
    const snapshot = params.get("snapshot_id");
    if (snapshot) setSnapshotId(snapshot);
  }, []);

  useEffect(() => {
    if (!knowledgeBases.loaded || !knowledgeBaseId) return;
    if (!knowledgeBases.data?.some((base) => base.id === knowledgeBaseId)) {
      setKnowledgeBaseId("");
      setSnapshotId("");
    }
  }, [knowledgeBases.loaded, knowledgeBases.data, knowledgeBaseId, sessionId]);

  useEffect(() => {
    if (!snapshots.loaded || !snapshotId) return;
    if (!snapshots.data?.some((snapshot) => snapshot.id === snapshotId)) setSnapshotId("");
  }, [snapshots.loaded, snapshots.data, snapshotId, knowledgeBaseId, sessionId]);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setResult(null);
    setError(null);
    if (!connected) {
      setError({ code: "SESSION_REQUIRED", message: t("playground.sessionRequired") });
      setPageState("error");
      return;
    }
    if (!knowledgeBaseId.trim() || !snapshotId.trim() || !query.trim()) {
      setError({ code: "INVALID_REQUEST", message: t("playground.invalidRequest") });
      setPageState("error");
      return;
    }
    const topKValues = [
      { value: denseTopK, max: 100 },
      { value: sparseTopK, max: 100 },
      { value: candidateTopK, max: 100 },
      { value: finalTopK, max: 20 },
    ];
    if (topKValues.some(({ value, max }) => !/^\d+$/.test(value) || Number(value) < 1 || Number(value) > max)) {
      setError({ code: "INVALID_REQUEST", message: t("playground.invalidTopK") });
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
          : new RetrievalPlaygroundError("REQUEST_FAILED", t("errors.retrieval"));
      setError({ code: nextError.code, message: nextError.message });
      setPageState("error");
    }
  }

  if (!connected) {
    return (
      <div className="page">
        <header className="page-header">
          <p className="eyebrow">{t("playground.eyebrow")}</p>
          <h1>{t("playground.title")}</h1>
          <p className="page-lede">{t("playground.lede")}</p>
        </header>
        <SessionRequired contextKey="session.context.playground" />
      </div>
    );
  }

  return (
    <div className="page">
      <header className="page-header">
        <p className="eyebrow">{t("playground.eyebrow")}</p>
        <h1>{t("playground.title")}</h1>
        <p className="page-lede">{t("playground.lede")}</p>
      </header>

      <form className="panel query-panel" onSubmit={handleSubmit} noValidate>
        <div className="panel-heading">
          <div>
            <p className="eyebrow">{t("playground.queryEyebrow")}</p>
            <h2>{t("playground.queryTitle")}</h2>
          </div>
        </div>
        <div className="form-grid">
          <label>
            {t("playground.knowledgeBaseId")}
            <select
              required
              value={knowledgeBaseId}
              onChange={(event) => {
                setKnowledgeBaseId(event.target.value);
                setSnapshotId("");
                setResult(null);
                setError(null);
                setPageState("initial");
              }}
              disabled={knowledgeBases.loading || Boolean(knowledgeBases.error)}
            >
              <option value="">
                {knowledgeBases.loaded && (knowledgeBases.data ?? []).length === 0
                  ? t("playground.noKnowledgeBases")
                  : t("playground.selectKnowledgeBase")}
              </option>
              {(knowledgeBases.data ?? []).map((base) => (
                <option key={base.id} value={base.id}>{base.name}</option>
              ))}
            </select>
          </label>
          <label>
            {t("playground.snapshotId")}
            <select
              required
              value={snapshotId}
              onChange={(event) => {
                setSnapshotId(event.target.value);
                setResult(null);
                setError(null);
                setPageState("initial");
              }}
              disabled={!knowledgeBaseId || snapshots.loading || Boolean(snapshots.error)}
            >
              <option value="">
                {snapshots.loaded && (snapshots.data ?? []).length === 0
                  ? t("playground.noSnapshots")
                  : t("playground.selectSnapshot")}
              </option>
              {(snapshots.data ?? []).map((snapshot) => (
                <option key={snapshot.id} value={snapshot.id}>
                  {`${formatNumber(snapshot.item_count)} · ${snapshot.content_hash.slice(0, 8)} · ${formatDateTime(snapshot.created_at)}`}
                </option>
              ))}
            </select>
          </label>
          <label className="query-field">
            {t("playground.query")}
            <textarea
              required
              rows={4}
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder={t("playground.queryPlaceholder")}
            />
          </label>
        </div>
        <details className="advanced-options">
          <summary>{t("playground.advancedConfig")}</summary>
          <div className="form-grid top-k-grid">
            <label>
              {t("playground.denseTopK")}
              <input type="number" min={1} max={100} value={denseTopK} onChange={(event) => setDenseTopK(event.target.value)} />
            </label>
            <label>
              {t("playground.sparseTopK")}
              <input type="number" min={1} max={100} value={sparseTopK} onChange={(event) => setSparseTopK(event.target.value)} />
            </label>
            <label>
              {t("playground.candidateTopK")}
              <input type="number" min={1} max={100} value={candidateTopK} onChange={(event) => setCandidateTopK(event.target.value)} />
            </label>
            <label>
              {t("playground.finalTopK")}
              <input type="number" min={1} max={20} value={finalTopK} onChange={(event) => setFinalTopK(event.target.value)} />
            </label>
          </div>
        </details>
        <div className="form-actions">
          <button type="submit" className="button button-primary" disabled={pageState === "loading"}>
            {pageState === "loading" ? t("playground.running") : t("playground.run")}
          </button>
        </div>
      </form>

      {knowledgeBases.error && (
        <ErrorState
          code={knowledgeBases.error.code}
          message={knowledgeBases.error.message || t("errors.loadKnowledgeBases")}
          onRetry={knowledgeBases.reload}
        />
      )}
      {snapshots.error && (
        <ErrorState
          code={snapshots.error.code}
          message={snapshots.error.message || t("errors.loadSnapshots")}
          onRetry={snapshots.reload}
        />
      )}

      {pageState === "initial" && <EmptyState title={t("playground.initial")} />}
      {pageState === "loading" && <LoadingState label={t("playground.running")} />}
      {pageState === "error" && error && <ErrorState code={error.code} message={error.message} />}
      {pageState === "empty" && (
        <EmptyState title={t("playground.empty")} hint={t("playground.emptyHint")} />
      )}

      {result && (
        <>
          <section className="playground-overview" aria-label={t("playground.overview.finalEvidence")}>
            <div>
              <span>{t("playground.overview.snapshot")}</span>
              <strong>{shortId(result.snapshot_id)}</strong>
            </div>
            <div>
              <span>{t("playground.overview.totalLatency")}</span>
              <strong>{result.total_latency_ms.toFixed(1)} ms</strong>
            </div>
            <div>
              <span>{t("playground.overview.finalEvidence")}</span>
              <strong>{formatNumber(result.evidence.length)}</strong>
            </div>
          </section>
          <section className="stage-grid" aria-label={t("playground.title")}>
            {stageLabels.map(([key, label]) => (
              <StageCard key={key} label={label} stage={result.stages[key]} />
            ))}
          </section>
          <Panel
            title={t("playground.evidence.title")}
            eyebrow={t("playground.evidence.eyebrow")}
            actions={<span className="state-hint">{t("playground.evidence.chunks", { count: formatNumber(result.evidence.length) })}</span>}
          >
            <div className="evidence-list">
              {result.evidence.map((item, index) => (
                <article className="evidence-item" key={item.chunk_id}>
                  <div className="evidence-rank">#{index + 1}</div>
                  <div className="evidence-body">
                    <div className="evidence-meta">
                      <strong>{item.source}</strong>
                      <span>{formatLocator(item.locator, t)}</span>
                      <span>
                        {t("playground.evidence.retrievalScore", { score: item.retrieval_score.toFixed(4) })}
                      </span>
                      <span>
                        {t("playground.evidence.rerankScore", { score: item.rerank_score?.toFixed(4) ?? "—" })}
                      </span>
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
