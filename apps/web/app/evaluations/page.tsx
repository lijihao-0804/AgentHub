"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import MetricCard from "@/components/ui/metric-card";
import { EmptyState, ErrorState, LoadingState, Panel, SessionRequired } from "@/components/ui/states";
import { ApiError, toApiError } from "@/lib/api/client";
import {
  PricingSnapshot,
  ReleaseGatePolicy,
  listDatasets,
  listExperiments,
  listPricingSnapshots,
  listReleaseGatePolicies,
} from "@/lib/api/evaluation";
import { useFrontendSession } from "@/components/providers/session-provider";
import { useI18n } from "@/i18n/provider";

export default function EvaluationOverviewPage() {
  const { t, formatNumber } = useI18n();
  const { workspaceId, accessToken, connected, sessionId } = useFrontendSession();
  const [datasetTotal, setDatasetTotal] = useState<number | null>(null);
  const [experimentTotal, setExperimentTotal] = useState<number | null>(null);
  const [pricing, setPricing] = useState<PricingSnapshot[] | null>(null);
  const [policies, setPolicies] = useState<ReleaseGatePolicy[] | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const activeSessionRef = useRef(sessionId);
  activeSessionRef.current = sessionId;

  useEffect(() => {
    setDatasetTotal(null);
    setExperimentTotal(null);
    setPricing(null);
    setPolicies(null);
    setError(null);
    setLoading(false);
    setLoaded(false);
  }, [sessionId]);

  useEffect(() => {
    if (!connected || loaded) return;
    const requestSessionId = sessionId;
    setLoaded(true);
    setLoading(true);
    setError(null);
    const input = { workspaceId, accessToken };
    Promise.all([
      // The overview only needs counts: limit=1 keeps four cards cheap.
      listDatasets(input, { limit: 1 }),
      listExperiments(input, { limit: 1 }),
      listPricingSnapshots(input),
      listReleaseGatePolicies(input),
    ])
      .then(([d, e, p, g]) => {
        if (activeSessionRef.current !== requestSessionId) return;
        setDatasetTotal(d.total);
        setExperimentTotal(e.total);
        setPricing(p);
        setPolicies(g);
      })
      .catch((caught) => {
        if (activeSessionRef.current === requestSessionId) setError(toApiError(caught, ""));
      })
      .finally(() => {
        if (activeSessionRef.current === requestSessionId) setLoading(false);
      });
  }, [connected, loaded, workspaceId, accessToken, sessionId]);

  if (!connected) {
    return (
      <div className="page">
        <header className="page-header">
          <p className="eyebrow">{t("evaluation.eyebrow")}</p>
          <h1>{t("evaluation.overview.title")}</h1>
        </header>
        <SessionRequired contextKey="session.context.evaluations" />
      </div>
    );
  }

  return (
    <div className="page">
      <header className="page-header">
        <p className="eyebrow">{t("evaluation.overview.eyebrow")}</p>
        <h1>{t("evaluation.overview.title")}</h1>
        <p className="page-lede">{t("evaluation.overview.lede")}</p>
      </header>

      {error && (
        <ErrorState
          code={error.code}
          message={error.message || t("errors.loadEvaluation")}
          onRetry={() => setLoaded(false)}
        />
      )}
      {loading && !error && <LoadingState />}

      {!loading && !error && (
        <>
          <section className="kpi-grid" aria-label={t("evaluation.overview.title")}>
            <MetricCard
              label={t("evaluation.overview.datasets")}
              value={formatNumber(datasetTotal ?? 0)}
              hint={t("evaluation.overview.countsHint")}
              href="/evaluations/datasets"
            />
            <MetricCard
              label={t("evaluation.overview.experiments")}
              value={formatNumber(experimentTotal ?? 0)}
              href="/evaluations/experiments"
            />
            <MetricCard
              label={t("evaluation.overview.pricing")}
              value={pricing?.length ?? 0}
              href="/evaluations/pricing"
            />
            <MetricCard
              label={t("evaluation.overview.policies")}
              value={policies?.length ?? 0}
              href="/evaluations/release-gates"
            />
          </section>

          <Panel title={t("evaluation.overview.quick")} eyebrow={t("evaluation.eyebrow")}>
            {datasetTotal !== null && experimentTotal !== null && pricing !== null && policies !== null && (
              <div className="overview-grid">
                <Link className="overview-card" href="/evaluations/datasets">
                  <span className="overview-card-title">{t("evaluation.overview.newDataset")}</span>
                  <span className="overview-card-description">{t("evaluation.datasets.lede")}</span>
                  <span className="overview-card-cta" aria-hidden="true">{t("evaluation.overview.open")} →</span>
                </Link>
                <Link className="overview-card" href="/evaluations/experiments">
                  <span className="overview-card-title">{t("evaluation.overview.newExperiment")}</span>
                  <span className="overview-card-description">{t("evaluation.experiments.lede")}</span>
                  <span className="overview-card-cta" aria-hidden="true">{t("evaluation.overview.open")} →</span>
                </Link>
                <Link className="overview-card" href="/evaluations/release-gates">
                  <span className="overview-card-title">{t("evaluation.overview.managePolicies")}</span>
                  <span className="overview-card-description">{t("evaluation.gates.lede")}</span>
                  <span className="overview-card-cta" aria-hidden="true">{t("evaluation.overview.open")} →</span>
                </Link>
              </div>
            )}
            {datasetTotal === 0 &&
              experimentTotal === 0 &&
              pricing?.length === 0 &&
              policies?.length === 0 && (
                <EmptyState
                  title={t("evaluation.datasets.empty")}
                  hint={t("evaluation.datasets.emptyHint")}
                />
              )}
          </Panel>
        </>
      )}
    </div>
  );
}
