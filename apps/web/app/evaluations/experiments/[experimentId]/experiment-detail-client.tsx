"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState, type FormEvent } from "react";

import HashValue, { InlineConfirm } from "../../../../components/evaluation/hash-value";
import StatusBadge from "../../../../components/status-badge";
import { EmptyState, ErrorState, LoadingState, Panel, SessionRequired } from "../../../../components/states";
import TechnicalDetails from "../../../../components/technical-details";
import { ApiError, toApiError } from "../../../../lib/api-client";
import {
  AgentSummary,
  AgentVersionSummary,
  EvaluationExperimentDetail,
  PricingSnapshot,
  addExperimentVariant,
  finalizeExperiment,
  getExperiment,
  listAgentVersions,
  listAgents,
  listPricingSnapshots,
  startExperimentRun,
} from "../../../../lib/evaluation";
import { useFrontendSession } from "../../../../components/session-provider";
import { useI18n } from "../../../../i18n/provider";

export default function ExperimentDetailClient({ experimentId }: { experimentId: string }) {
  const { t, statusLabel, purposeLabel, formatDateTime, formatNumber } = useI18n();
  const router = useRouter();
  const { workspaceId, accessToken, connected } = useFrontendSession();
  const [experiment, setExperiment] = useState<EvaluationExperimentDetail | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [pricing, setPricing] = useState<PricingSnapshot[]>([]);

  const [showVariantForm, setShowVariantForm] = useState(false);
  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [agentId, setAgentId] = useState("");
  const [agentVersions, setAgentVersions] = useState<AgentVersionSummary[]>([]);
  const [versionsLoading, setVersionsLoading] = useState(false);
  const [agentVersionId, setAgentVersionId] = useState("");
  const [label, setLabel] = useState("");
  const [pricingId, setPricingId] = useState("");
  const [ordinal, setOrdinal] = useState("0");
  const [metadataText, setMetadataText] = useState("");
  const [variantError, setVariantError] = useState<string | null>(null);
  const [addingVariant, setAddingVariant] = useState(false);

  const [showFinalizeConfirm, setShowFinalizeConfirm] = useState(false);
  const [finalizing, setFinalizing] = useState(false);
  const [finalizeError, setFinalizeError] = useState<string | null>(null);

  const [startingRun, setStartingRun] = useState(false);
  const [startRunError, setStartRunError] = useState<string | null>(null);

  const input = { workspaceId, accessToken };

  const load = useCallback(async () => {
    setError(null);
    if (!connected) return;
    setLoading(true);
    try {
      setExperiment(await getExperiment(input, experimentId));
      setLoaded(true);
    } catch (caught) {
      setError(toApiError(caught, ""));
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connected, workspaceId, accessToken, experimentId]);

  useEffect(() => {
    if (connected && !loaded) void load();
  }, [connected, loaded, load]);

  useEffect(() => {
    if (!connected) setLoaded(false);
  }, [connected]);

  useEffect(() => {
    if (!connected) return;
    listAgents(input)
      .then(setAgents)
      .catch(() => setAgents([]));
    listPricingSnapshots(input)
      .then(setPricing)
      .catch(() => setPricing([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connected]);

  useEffect(() => {
    if (!agentId) {
      setAgentVersions([]);
      setAgentVersionId("");
      return;
    }
    setVersionsLoading(true);
    setAgentVersionId("");
    listAgentVersions(input, agentId)
      .then(setAgentVersions)
      .catch(() => setAgentVersions([]))
      .finally(() => setVersionsLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId]);

  const pricingName = (id: string) => pricing.find((p) => p.id === id)?.name ?? null;

  async function submitVariant(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setVariantError(null);
    if (!label.trim() || !agentVersionId || !pricingId) return;
    let metadata: Record<string, unknown> = {};
    if (metadataText.trim()) {
      try {
        metadata = JSON.parse(metadataText) as Record<string, unknown>;
      } catch {
        setVariantError(t("evaluation.experiment.variants.invalidMetadata"));
        return;
      }
    }
    setAddingVariant(true);
    try {
      await addExperimentVariant(input, experimentId, {
        label: label.trim(),
        agent_version_id: agentVersionId,
        pricing_snapshot_id: pricingId,
        ordinal: Number(ordinal) || 0,
        variant_metadata: metadata,
      });
      setLabel("");
      setMetadataText("");
      setShowVariantForm(false);
      await load();
    } catch (caught) {
      const apiError = toApiError(caught, "");
      setVariantError(apiError.message || apiError.code);
    } finally {
      setAddingVariant(false);
    }
  }

  async function finalize() {
    setFinalizeError(null);
    setFinalizing(true);
    try {
      setExperiment(await finalizeExperiment(input, experimentId));
      setShowFinalizeConfirm(false);
    } catch (caught) {
      const apiError = toApiError(caught, "");
      setFinalizeError(apiError.message || apiError.code);
    } finally {
      setFinalizing(false);
    }
  }

  async function startRun() {
    setStartRunError(null);
    setStartingRun(true);
    try {
      const run = await startExperimentRun(input, experimentId);
      router.push(`/evaluations/runs/${encodeURIComponent(run.id)}`);
    } catch (caught) {
      const apiError = toApiError(caught, "");
      setStartRunError(apiError.message || apiError.code);
      setStartingRun(false);
    }
  }

  if (!connected) {
    return (
      <div className="page">
        <header className="page-header">
          <p className="eyebrow">{t("evaluation.experiment.eyebrow")}</p>
        </header>
        <SessionRequired contextKey="session.context.evaluations" />
      </div>
    );
  }

  const isDraft = experiment?.status === "DRAFT";

  return (
    <div className="page">
      <header className="page-header">
        <p className="eyebrow">{t("evaluation.experiment.eyebrow")}</p>
        <h1>
          {experiment?.name ?? ""}{" "}
          {experiment && <StatusBadge status={experiment.status} />}
          {experiment && !isDraft && <span className="immutable-tag">{t("evaluation.immutable")}</span>}
        </h1>
        <p className="page-lede">
          <Link href="/evaluations/experiments">{t("evaluation.experiments.title")} /</Link>{" "}
          <code>{experimentId}</code>
        </p>
      </header>

      {error && (
        <ErrorState code={error.code} message={error.message || t("errors.loadEvaluation")} onRetry={() => void load()} />
      )}
      {loading && !experiment && !error && <LoadingState />}
      {loaded && !experiment && !error && (
        <EmptyState title={t("evaluation.experiment.notFound")} hint={t("evaluation.experiment.notFoundHint")} />
      )}

      {experiment && (
        <>
          <Panel ariaLabel={t("evaluation.experiment.eyebrow")}>
            <div className="run-facts">
              <span>{t("evaluation.experiment.facts.status")}<strong><StatusBadge status={experiment.status} /></strong></span>
              <span>{t("evaluation.experiment.facts.split")}<strong>{experiment.split}</strong></span>
              <span>{t("evaluation.experiment.facts.purpose")}<strong>{purposeLabel(experiment.purpose)}</strong></span>
              <span>{t("evaluation.experiment.facts.repetitions")}<strong>{experiment.repetitions}</strong></span>
              <span>{t("evaluation.experiment.facts.schemaVersion")}<strong>{experiment.dataset_schema_version}</strong></span>
              <span>{t("evaluation.experiment.facts.holdoutExposureCount")}<strong>{experiment.holdout_exposure_count ?? "—"}</strong></span>
              <span>{t("evaluation.experiment.facts.buildSha")}<strong><code>{experiment.build_sha.slice(0, 12)}</code></strong></span>
              <span>
                {t("evaluation.experiment.facts.datasetHash")}
                <strong><HashValue value={experiment.dataset_content_hash} label={t("evaluation.experiment.facts.datasetHash")} /></strong>
              </span>
              <span>
                {t("evaluation.experiment.facts.specHash")}
                <strong>{experiment.spec_hash ? <HashValue value={experiment.spec_hash} label={t("evaluation.experiment.facts.specHash")} /> : "—"}</strong>
              </span>
              <span>{t("evaluation.experiment.facts.createdAt")}<strong>{formatDateTime(experiment.created_at)}</strong></span>
            </div>
            <p className="state-hint">
              {isDraft ? t("evaluation.experiment.draftNotice") : t("evaluation.experiment.readyNotice")}
            </p>
            {experiment.description && <p className="state-hint">{experiment.description}</p>}

            <div className="form-actions">
              {isDraft && !showFinalizeConfirm && (
                <button
                  type="button"
                  className="button button-primary"
                  onClick={() => setShowFinalizeConfirm(true)}
                  disabled={experiment.variants.length === 0}
                >
                  {t("evaluation.experiment.finalize")}
                </button>
              )}
              {!isDraft && (
                <button type="button" className="button button-primary" onClick={() => void startRun()} disabled={startingRun}>
                  {startingRun ? t("evaluation.experiment.starting") : t("evaluation.experiment.startRun")}
                </button>
              )}
            </div>
            {showFinalizeConfirm && (
              <InlineConfirm
                title={t("evaluation.experiment.finalizeConfirmTitle")}
                text={t("evaluation.experiment.finalizeConfirmText")}
                confirmLabel={t("evaluation.experiment.finalize")}
                pendingLabel={t("evaluation.experiment.finalizing")}
                cancelLabel={t("evaluation.experiment.cancel")}
                onConfirm={() => void finalize()}
                onCancel={() => setShowFinalizeConfirm(false)}
                pending={finalizing}
              />
            )}
            {finalizeError && <p className="session-error" role="alert">{finalizeError}</p>}
            {startRunError && <p className="session-error" role="alert">{startRunError}</p>}
          </Panel>

          <Panel
            title={t("evaluation.experiment.variants.title")}
            eyebrow={t("evaluation.experiment.eyebrow")}
            actions={
              isDraft && (
                <button type="button" className="button button-ghost" onClick={() => setShowVariantForm((v) => !v)}>
                  {t("evaluation.experiment.variants.addVariant")}
                </button>
              )
            }
          >
            {showVariantForm && (
              <form className="eval-form" onSubmit={submitVariant} noValidate>
                <div className="eval-form-title">{t("evaluation.experiment.variants.formTitle")}</div>
                <div className="form-grid">
                  <label>
                    {t("evaluation.experiment.variants.labels.label")}
                    <input required value={label} onChange={(e) => setLabel(e.target.value)} spellCheck={false} />
                  </label>
                  <label>
                    {t("evaluation.experiment.variants.labels.agent")}
                    {agents.length === 0 ? (
                      <span className="state-hint">{t("evaluation.experiment.variants.noAgents")}</span>
                    ) : (
                      <select required value={agentId} onChange={(e) => setAgentId(e.target.value)}>
                        <option value="">{t("evaluation.experiment.variants.selectAgent")}</option>
                        {agents.map((agent) => (
                          <option value={agent.id} key={agent.id}>
                            {agent.name}
                          </option>
                        ))}
                      </select>
                    )}
                  </label>
                  <label>
                    {t("evaluation.experiment.variants.labels.version")}
                    {versionsLoading ? (
                      <span className="state-hint">{t("evaluation.experiment.variants.versionSelectorLoading")}</span>
                    ) : (
                      <select required value={agentVersionId} onChange={(e) => setAgentVersionId(e.target.value)} disabled={!agentId}>
                        <option value="">{t("evaluation.experiment.variants.selectVersion")}</option>
                        {agentVersions.map((version) => (
                          <option value={version.id} key={version.id}>
                            v{version.version_number} · {version.resolved_spec_hash.slice(0, 8)}…
                          </option>
                        ))}
                      </select>
                    )}
                  </label>
                  <label>
                    {t("evaluation.experiment.variants.labels.pricingSnapshot")}
                    <select required value={pricingId} onChange={(e) => setPricingId(e.target.value)}>
                      <option value="">{t("evaluation.experiment.variants.selectPricing")}</option>
                      {pricing.map((snapshot) => (
                        <option value={snapshot.id} key={snapshot.id}>
                          {snapshot.name} · {snapshot.provider}/{snapshot.model} · {snapshot.currency}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    {t("evaluation.experiment.variants.labels.ordinal")}
                    <input type="number" min={0} max={4} value={ordinal} onChange={(e) => setOrdinal(e.target.value)} />
                  </label>
                  <label>
                    {t("evaluation.experiment.variants.labels.metadata")}
                    <textarea
                      rows={2}
                      value={metadataText}
                      onChange={(e) => setMetadataText(e.target.value)}
                      spellCheck={false}
                      className="mono-area"
                    />
                    <span className="state-hint">{t("evaluation.experiment.variants.metadataHint")}</span>
                  </label>
                </div>
                {variantError && <p className="session-error" role="alert">{variantError}</p>}
                <div className="form-actions">
                  <button
                    type="submit"
                    className="button button-primary"
                    disabled={addingVariant || !label.trim() || !agentVersionId || !pricingId}
                  >
                    {addingVariant ? t("evaluation.experiment.variants.adding") : t("evaluation.experiment.variants.addVariant")}
                  </button>
                </div>
              </form>
            )}

            {experiment.variants.length === 0 ? (
              <EmptyState title={t("evaluation.experiment.variants.empty")} />
            ) : (
              <div className="data-table">
                <table>
                  <thead>
                    <tr>
                      <th scope="col">{t("evaluation.experiment.variants.columns.label")}</th>
                      <th scope="col">{t("evaluation.experiment.variants.columns.ordinal")}</th>
                      <th scope="col">{t("evaluation.experiment.variants.columns.agentVersion")}</th>
                      <th scope="col">{t("evaluation.experiment.variants.columns.specHash")}</th>
                      <th scope="col">{t("evaluation.experiment.variants.columns.pricing")}</th>
                      <th scope="col">{t("evaluation.experiment.variants.columns.knowledgeSnapshots")}</th>
                      <th scope="col">{t("evaluation.experiment.variants.columns.variantHash")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {experiment.variants
                      .slice()
                      .sort((a, b) => a.ordinal - b.ordinal)
                      .map((variant) => (
                        <tr key={variant.id}>
                          <td data-label={t("evaluation.experiment.variants.columns.label")}>{variant.label}</td>
                          <td data-label={t("evaluation.experiment.variants.columns.ordinal")}>{variant.ordinal}</td>
                          <td data-label={t("evaluation.experiment.variants.columns.agentVersion")}>
                            <code>{variant.agent_version_id.slice(0, 8)}…</code>
                          </td>
                          <td data-label={t("evaluation.experiment.variants.columns.specHash")}>
                            <HashValue value={variant.resolved_spec_hash} label={t("evaluation.experiment.variants.columns.specHash")} />
                          </td>
                          <td data-label={t("evaluation.experiment.variants.columns.pricing")}>
                            {pricingName(variant.pricing_snapshot_id) ?? (
                              <code>{variant.pricing_snapshot_id.slice(0, 8)}…</code>
                            )}{" "}
                            <HashValue value={variant.pricing_snapshot_hash} label={t("evaluation.experiment.variants.columns.pricingHash")} />
                          </td>
                          <td data-label={t("evaluation.experiment.variants.columns.knowledgeSnapshots")}>
                            {formatNumber(variant.effective_knowledge_snapshots?.length ?? 0)}
                          </td>
                          <td data-label={t("evaluation.experiment.variants.columns.variantHash")}>
                            <HashValue value={variant.variant_hash} label={t("evaluation.experiment.variants.columns.variantHash")} />
                          </td>
                        </tr>
                      ))}
                  </tbody>
                </table>
              </div>
            )}
            {experiment.spec_json && <TechnicalDetails value={experiment.spec_json} summary={t("common.technicalDetails")} />}
          </Panel>
        </>
      )}
    </div>
  );
}
