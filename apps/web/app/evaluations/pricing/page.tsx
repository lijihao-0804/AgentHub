"use client";

import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";

import HashValue from "../../../components/evaluation/hash-value";
import { EmptyState, ErrorState, LoadingState, Panel, SessionRequired } from "../../../components/states";
import { ApiError, toApiError } from "../../../lib/api-client";
import { PricingSnapshot, createPricingSnapshot, listPricingSnapshots } from "../../../lib/evaluation";
import { useFrontendSession } from "../../../components/session-provider";
import { useI18n } from "../../../i18n/provider";

function nowLocalIso(): string {
  const now = new Date();
  now.setSeconds(0, 0);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}T${pad(now.getHours())}:${pad(now.getMinutes())}`;
}

function initialPricingForm() {
  return {
    name: "",
    provider: "",
    model: "",
    currency: "USD",
    inputPrice: "",
    outputPrice: "",
    cachedPrice: "",
    effectiveAt: nowLocalIso(),
    sourceNote: "",
  };
}

export default function EvaluationPricingPage() {
  const { t, formatDateTime } = useI18n();
  const { workspaceId, accessToken, connected, sessionId } = useFrontendSession();
  const [snapshots, setSnapshots] = useState<PricingSnapshot[]>([]);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);

  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState(initialPricingForm);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [createdNotice, setCreatedNotice] = useState(false);
  const activeSessionRef = useRef(sessionId);
  activeSessionRef.current = sessionId;

  useEffect(() => {
    setSnapshots([]);
    setError(null);
    setLoading(false);
    setLoaded(false);
    setShowForm(false);
    setForm(initialPricingForm());
    setCreating(false);
    setCreateError(null);
    setCreatedNotice(false);
  }, [sessionId]);

  const input = { workspaceId, accessToken };

  const refresh = useCallback(async () => {
    setError(null);
    if (!connected) return;
    const requestSessionId = sessionId;
    setLoading(true);
    try {
      const nextSnapshots = await listPricingSnapshots(input);
      if (activeSessionRef.current !== requestSessionId) return;
      setSnapshots(nextSnapshots);
      setLoaded(true);
    } catch (caught) {
      if (activeSessionRef.current === requestSessionId) setError(toApiError(caught, ""));
    } finally {
      if (activeSessionRef.current === requestSessionId) setLoading(false);
    }
  }, [connected, workspaceId, accessToken, sessionId]);

  useEffect(() => {
    if (connected && !loaded) void refresh();
  }, [connected, loaded, refresh]);

  function update<K extends keyof typeof form>(key: K, value: string) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  /** Prices are submitted as strings so the backend Decimal parses them exactly. */
  async function submitCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setCreateError(null);
    const requestSessionId = sessionId;
    setCreating(true);
    try {
      await createPricingSnapshot(input, {
        name: form.name.trim(),
        provider: form.provider.trim(),
        model: form.model.trim(),
        currency: form.currency.trim().toUpperCase().slice(0, 3),
        input_price_per_1m: form.inputPrice.trim(),
        output_price_per_1m: form.outputPrice.trim(),
        cached_input_price_per_1m: form.cachedPrice.trim() || null,
        effective_at: new Date(form.effectiveAt).toISOString(),
        source_note: form.sourceNote.trim(),
      });
      if (activeSessionRef.current !== requestSessionId) return;
      setForm((current) => ({ ...current, name: "", provider: "", model: "", inputPrice: "", outputPrice: "", cachedPrice: "", sourceNote: "" }));
      setShowForm(false);
      setCreatedNotice(true);
      await refresh();
    } catch (caught) {
      const apiError = toApiError(caught, "");
      if (activeSessionRef.current === requestSessionId) setCreateError(apiError.message || apiError.code);
    } finally {
      if (activeSessionRef.current === requestSessionId) setCreating(false);
    }
  }

  if (!connected) {
    return (
      <div className="page">
        <header className="page-header">
          <p className="eyebrow">{t("evaluation.pricing.eyebrow")}</p>
          <h1>{t("evaluation.pricing.title")}</h1>
        </header>
        <SessionRequired contextKey="session.context.evaluations" />
      </div>
    );
  }

  return (
    <div className="page">
      <header className="page-header">
        <p className="eyebrow">{t("evaluation.pricing.eyebrow")}</p>
        <h1>{t("evaluation.pricing.title")}</h1>
        <p className="page-lede">{t("evaluation.pricing.lede")}</p>
      </header>

      <Panel
        title={t("evaluation.pricing.title")}
        eyebrow={t("evaluation.pricing.eyebrow")}
        actions={
          <button type="button" className="button button-primary" onClick={() => setShowForm((v) => !v)}>
            {t("evaluation.pricing.create")}
          </button>
        }
      >
        {createdNotice && <p className="inline-notice">{t("evaluation.pricing.createdNotice")}</p>}

        {showForm && (
          <form className="eval-form" onSubmit={submitCreate} noValidate>
            <div className="eval-form-title">{t("evaluation.pricing.formTitle")}</div>
            <div className="form-grid">
              <label>
                {t("evaluation.pricing.labels.name")}
                <input required value={form.name} onChange={(e) => update("name", e.target.value)} spellCheck={false} />
              </label>
              <label>
                {t("evaluation.pricing.labels.provider")}
                <input required value={form.provider} onChange={(e) => update("provider", e.target.value)} spellCheck={false} />
              </label>
              <label>
                {t("evaluation.pricing.labels.model")}
                <input required value={form.model} onChange={(e) => update("model", e.target.value)} spellCheck={false} />
              </label>
              <label>
                {t("evaluation.pricing.labels.currency")}
                <input
                  required
                  minLength={3}
                  maxLength={3}
                  value={form.currency}
                  onChange={(e) => update("currency", e.target.value)}
                  spellCheck={false}
                />
              </label>
              <label>
                {t("evaluation.pricing.labels.inputPrice")}
                <input
                  required
                  inputMode="decimal"
                  value={form.inputPrice}
                  onChange={(e) => update("inputPrice", e.target.value)}
                  placeholder={t("evaluation.pricing.pricePlaceholder")}
                  spellCheck={false}
                />
              </label>
              <label>
                {t("evaluation.pricing.labels.outputPrice")}
                <input
                  required
                  inputMode="decimal"
                  value={form.outputPrice}
                  onChange={(e) => update("outputPrice", e.target.value)}
                  placeholder={t("evaluation.pricing.pricePlaceholder")}
                  spellCheck={false}
                />
              </label>
              <label>
                {t("evaluation.pricing.labels.cachedPrice")}
                <input
                  inputMode="decimal"
                  value={form.cachedPrice}
                  onChange={(e) => update("cachedPrice", e.target.value)}
                  placeholder={t("evaluation.pricing.pricePlaceholder")}
                  spellCheck={false}
                />
              </label>
              <label>
                {t("evaluation.pricing.labels.effectiveAt")}
                <input
                  required
                  type="datetime-local"
                  value={form.effectiveAt}
                  onChange={(e) => update("effectiveAt", e.target.value)}
                />
              </label>
              <label>
                {t("evaluation.pricing.labels.sourceNote")}
                <textarea
                  required
                  rows={2}
                  value={form.sourceNote}
                  onChange={(e) => update("sourceNote", e.target.value)}
                />
              </label>
            </div>
            {createError && <p className="session-error" role="alert">{createError}</p>}
            <div className="form-actions">
              <button type="submit" className="button button-primary" disabled={creating}>
                {creating ? t("evaluation.pricing.creating") : t("evaluation.pricing.create")}
              </button>
            </div>
          </form>
        )}

        {error && (
          <ErrorState code={error.code} message={error.message || t("errors.loadEvaluation")} onRetry={() => void refresh()} />
        )}
        {loading && snapshots.length === 0 && !error && <LoadingState />}
        {loaded && snapshots.length === 0 && !error && (
          <EmptyState title={t("evaluation.pricing.empty")} hint={t("evaluation.pricing.emptyHint")} />
        )}

        {snapshots.length > 0 && (
          <div className="data-table">
            <table>
              <thead>
                <tr>
                  <th scope="col">{t("evaluation.pricing.columns.name")}</th>
                  <th scope="col">{t("evaluation.pricing.columns.provider")}</th>
                  <th scope="col">{t("evaluation.pricing.columns.model")}</th>
                  <th scope="col">{t("evaluation.pricing.columns.currency")}</th>
                  <th scope="col">{t("evaluation.pricing.columns.input")}</th>
                  <th scope="col">{t("evaluation.pricing.columns.output")}</th>
                  <th scope="col">{t("evaluation.pricing.columns.cached")}</th>
                  <th scope="col">{t("evaluation.pricing.columns.effectiveAt")}</th>
                  <th scope="col">{t("evaluation.pricing.columns.sourceNote")}</th>
                  <th scope="col">{t("evaluation.pricing.columns.hash")}</th>
                </tr>
              </thead>
              <tbody>
                {snapshots.map((snapshot) => (
                  <tr key={snapshot.id}>
                    <td data-label={t("evaluation.pricing.columns.name")}>{snapshot.name}</td>
                    <td data-label={t("evaluation.pricing.columns.provider")}>{snapshot.provider}</td>
                    <td data-label={t("evaluation.pricing.columns.model")}><code>{snapshot.model}</code></td>
                    <td data-label={t("evaluation.pricing.columns.currency")}>{snapshot.currency}</td>
                    <td data-label={t("evaluation.pricing.columns.input")}>{snapshot.input_price_per_1m}</td>
                    <td data-label={t("evaluation.pricing.columns.output")}>{snapshot.output_price_per_1m}</td>
                    <td data-label={t("evaluation.pricing.columns.cached")}>
                      {snapshot.cached_input_price_per_1m ?? "—"}
                    </td>
                    <td data-label={t("evaluation.pricing.columns.effectiveAt")}>{formatDateTime(snapshot.effective_at)}</td>
                    <td data-label={t("evaluation.pricing.columns.sourceNote")} className="muted">{snapshot.source_note}</td>
                    <td data-label={t("evaluation.pricing.columns.hash")}>
                      <HashValue value={snapshot.content_hash} label={t("evaluation.pricing.columns.hash")} />
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
