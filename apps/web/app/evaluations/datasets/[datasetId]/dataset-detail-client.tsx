"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import HashValue from "@/components/evaluation/hash-value";
import StatusBadge from "@/components/ui/status-badge";
import { EmptyState, ErrorState, InlineError, LoadingState, Panel, SessionRequired } from "@/components/ui/states";
import { ApiError, toApiError } from "@/lib/api/client";
import {
  EvaluationDataset,
  EvaluationDatasetItemInput,
  EvaluationDatasetVersion,
  createDatasetVersion,
  listDatasetVersions,
} from "@/lib/api/evaluation";
import { useFrontendSession } from "@/components/providers/session-provider";
import { useI18n } from "@/i18n/provider";

type ParsedImport = {
  items: EvaluationDatasetItemInput[];
  devCount: number;
  holdoutCount: number;
  categories: string[];
};

const ITEM_FIELDS = ["case_key", "split", "category", "input", "expected", "tags", "source_provenance", "ordinal"];

/** Client-side JSON validation; the backend remains the authority. */
function parseItemsJson(text: string): ParsedImport | { error: string } {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch (error) {
    return { error: String((error as Error).message) };
  }
  const items = Array.isArray(parsed) ? parsed : (parsed as { items?: unknown })?.items;
  if (!Array.isArray(items) || items.length === 0) return { error: "__invalid_items__" };
  const normalized: EvaluationDatasetItemInput[] = [];
  for (let index = 0; index < items.length; index += 1) {
    const item = items[index];
    if (typeof item !== "object" || item === null || Array.isArray(item)) {
      return { error: `__item__${index}` };
    }
    const raw = item as Record<string, unknown>;
    for (const field of ITEM_FIELDS) {
      if (raw[field] === undefined) return { error: `__field__${index}:${field}` };
    }
    if (
      typeof raw.input !== "object" ||
      raw.input === null ||
      Array.isArray(raw.input) ||
      typeof raw.expected !== "object" ||
      raw.expected === null ||
      Array.isArray(raw.expected) ||
      typeof raw.source_provenance !== "object" ||
      raw.source_provenance === null ||
      Array.isArray(raw.source_provenance)
    ) {
      return { error: `__item__${index}` };
    }
    normalized.push({
      case_key: String(raw.case_key),
      split: String(raw.split),
      category: String(raw.category),
      input: raw.input as Record<string, unknown>,
      expected: raw.expected as Record<string, unknown>,
      tags: Array.isArray(raw.tags) ? raw.tags.map(String) : [],
      source_provenance: raw.source_provenance as Record<string, unknown>,
      ordinal: Number(raw.ordinal),
    });
  }
  return {
    items: normalized,
    devCount: normalized.filter((i) => i.split === "DEV").length,
    holdoutCount: normalized.filter((i) => i.split === "HOLDOUT").length,
    categories: [...new Set(normalized.map((i) => i.category))],
  };
}

export default function DatasetDetailPage({ datasetId }: { datasetId: string }) {
  const { t, formatNumber, formatDateTime } = useI18n();
  const { workspaceId, accessToken, connected, sessionId } = useFrontendSession();
  const [versions, setVersions] = useState<EvaluationDatasetVersion[]>([]);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);

  const [showForm, setShowForm] = useState(false);
  const [jsonText, setJsonText] = useState("");
  const [schemaVersion, setSchemaVersion] = useState("1");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<ApiError | null>(null);
  const [createdNotice, setCreatedNotice] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const activeSessionRef = useRef(sessionId);
  activeSessionRef.current = sessionId;

  useEffect(() => {
    setVersions([]);
    setError(null);
    setLoading(false);
    setLoaded(false);
    setShowForm(false);
    setJsonText("");
    setSchemaVersion("1");
    setCreating(false);
    setCreateError(null);
    setCreatedNotice(null);
  }, [sessionId]);

  const input = { workspaceId, accessToken };

  const refresh = useCallback(async () => {
    setError(null);
    if (!connected) return;
    const requestSessionId = sessionId;
    setLoading(true);
    try {
      const nextVersions = await listDatasetVersions(input, datasetId);
      if (activeSessionRef.current !== requestSessionId) return;
      setVersions(nextVersions);
      setLoaded(true);
    } catch (caught) {
      if (activeSessionRef.current === requestSessionId) setError(toApiError(caught, ""));
    } finally {
      if (activeSessionRef.current === requestSessionId) setLoading(false);
    }
  }, [connected, workspaceId, accessToken, datasetId, sessionId]);

  useEffect(() => {
    if (connected && !loaded) void refresh();
  }, [connected, loaded, refresh]);

  const parsed: ParsedImport | { error: string } | null =
    jsonText.trim() === "" ? null : parseItemsJson(jsonText);

  async function handleFile(file: File | undefined) {
    if (!file) return;
    setJsonText(await file.text());
  }

  async function submitVersion(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setCreateError(null);
    if (!parsed || "error" in parsed) return;
    const requestSessionId = sessionId;
    setCreating(true);
    try {
      const version = await createDatasetVersion(input, datasetId, {
        schema_version: Number(schemaVersion) || 1,
        items: parsed.items,
      });
      if (activeSessionRef.current !== requestSessionId) return;
      setJsonText("");
      setShowForm(false);
      setCreatedNotice(t("evaluation.datasets.version.title", { number: version.version_number }));
      await refresh();
    } catch (caught) {
      const apiError = toApiError(caught, "");
      if (activeSessionRef.current === requestSessionId) setCreateError(apiError);
    } finally {
      if (activeSessionRef.current === requestSessionId) setCreating(false);
    }
  }

  if (!connected) {
    return (
      <div className="page">
        <header className="page-header">
          <p className="eyebrow">{t("evaluation.datasets.eyebrow")}</p>
          <h1>{t("evaluation.datasets.title")}</h1>
        </header>
        <SessionRequired contextKey="session.context.evaluations" />
      </div>
    );
  }

  return (
    <div className="page">
      <header className="page-header">
        <p className="eyebrow">{t("evaluation.datasets.eyebrow")}</p>
        <h1>{t("evaluation.datasets.title")}</h1>
        <p className="page-lede">
          <Link href="/evaluations/datasets">{t("evaluation.overview.datasets")} /</Link>{" "}
          <code>{datasetId}</code>
        </p>
      </header>

      <Panel
        title={t("evaluation.datasets.versions.title")}
        eyebrow={t("evaluation.datasets.eyebrow")}
        actions={
          <button type="button" className="button button-primary" onClick={() => setShowForm((v) => !v)}>
            {t("evaluation.datasets.versions.create")}
          </button>
        }
      >
        {createdNotice && (
          <p className="inline-notice">
            {t("evaluation.datasets.createdNotice")} {createdNotice}
          </p>
        )}

        {showForm && (
          <form className="eval-form" onSubmit={submitVersion} noValidate>
            <div className="eval-form-title">{t("evaluation.datasets.versions.formTitle")}</div>
            <label>
              {t("evaluation.datasets.versions.fileLabel")}
              <input
                type="file"
                accept=".json,application/json"
                ref={fileInputRef}
                onChange={(event) => void handleFile(event.target.files?.[0])}
              />
              <span className="state-hint">{t("evaluation.datasets.versions.fileHint")}</span>
            </label>
            <label>
              {t("evaluation.datasets.versions.jsonLabel")}
              <textarea
                rows={8}
                value={jsonText}
                onChange={(event) => setJsonText(event.target.value)}
                spellCheck={false}
                className="mono-area"
              />
              <span className="state-hint">{t("evaluation.datasets.versions.jsonHint")}</span>
            </label>
            {parsed && !("error" in parsed) && (
              <p className="inline-notice">
                {t("evaluation.datasets.versions.parsed", { count: formatNumber(parsed.items.length) })} ·{" "}
                {t("evaluation.datasets.versions.parsedDev", { count: parsed.devCount })} ·{" "}
                {t("evaluation.datasets.versions.parsedHoldout", { count: parsed.holdoutCount })} ·{" "}
                {t("evaluation.datasets.versions.parsedCategories", { categories: parsed.categories.join(", ") || "—" })}
              </p>
            )}
            {parsed && "error" in parsed && (
              <p className="session-error" role="alert">
                {parsed.error === "__invalid_items__"
                  ? t("evaluation.datasets.versions.invalidItems")
                  : parsed.error.startsWith("__item__")
                    ? `${t("evaluation.datasets.versions.invalidItems")} (${parsed.error.slice(8)})`
                  : parsed.error.startsWith("__field__")
                    ? `${t("evaluation.datasets.versions.invalidItems")} (${parsed.error.slice(9)})`
                    : t("evaluation.datasets.versions.invalidJson", { message: parsed.error })}
              </p>
            )}
            <label>
              {t("evaluation.datasets.versions.schemaVersion")}
              <input type="number" min={1} value={schemaVersion} onChange={(e) => setSchemaVersion(e.target.value)} />
            </label>
            <InlineError error={createError} fallback={t("errors.requestFailed")} />
            <div className="form-actions">
              <button
                type="submit"
                className="button button-primary"
                disabled={creating || !parsed || "error" in parsed}
              >
                {creating ? t("evaluation.datasets.versions.creating") : t("evaluation.datasets.versions.create")}
              </button>
            </div>
          </form>
        )}

        {error && (
          <ErrorState code={error.code} message={error.message || t("errors.loadEvaluation")} onRetry={() => void refresh()} />
        )}
        {loading && versions.length === 0 && !error && <LoadingState />}
        {loaded && versions.length === 0 && !error && <EmptyState title={t("evaluation.datasets.versions.empty")} />}

        {versions.length > 0 && (
          <div className="data-table">
            <table>
              <thead>
                <tr>
                  <th scope="col">{t("evaluation.datasets.versions.columns.version")}</th>
                  <th scope="col">{t("evaluation.datasets.versions.columns.status")}</th>
                  <th scope="col">{t("evaluation.datasets.versions.columns.items")}</th>
                  <th scope="col">{t("evaluation.datasets.versions.columns.contentHash")}</th>
                  <th scope="col">{t("evaluation.datasets.versions.columns.created")}</th>
                  <th scope="col">{t("evaluation.datasets.versions.columns.publishedAt")}</th>
                </tr>
              </thead>
              <tbody>
                {versions.map((version) => (
                  <tr key={version.id}>
                    <td data-label={t("evaluation.datasets.versions.columns.version")}>
                      <Link href={`/evaluations/datasets/${encodeURIComponent(datasetId)}/versions/${encodeURIComponent(version.id)}`}>
                        v{version.version_number}
                      </Link>
                    </td>
                    <td data-label={t("evaluation.datasets.versions.columns.status")}>
                      <StatusBadge status={version.status} />
                    </td>
                    <td data-label={t("evaluation.datasets.versions.columns.items")}>
                      {version.item_count ?? "—"}
                    </td>
                    <td data-label={t("evaluation.datasets.versions.columns.contentHash")}>
                      <HashValue value={version.content_hash} label={t("evaluation.datasets.versions.columns.contentHash")} />
                    </td>
                    <td data-label={t("evaluation.datasets.versions.columns.created")}>{formatDateTime(version.created_at)}</td>
                    <td data-label={t("evaluation.datasets.versions.columns.publishedAt")}>
                      {version.published_at ? formatDateTime(version.published_at) : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}
