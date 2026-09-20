"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import MetricCard from "../../components/metric-card";
import { EmptyState, ErrorState, LoadingState, Panel, SessionRequired } from "../../components/states";
import { ApiError, toApiError } from "../../lib/api-client";
import {
  EvaluationDataset,
  PricingSnapshot,
  ReleaseGatePolicy,
  EvaluationExperiment,
  listDatasets,
  listExperiments,
  listPricingSnapshots,
  listReleaseGatePolicies,
} from "../../lib/evaluation";
import { useFrontendSession } from "../../components/session-provider";
import { useI18n } from "../../i18n/provider";

export default function EvaluationOverviewPage() {
  const { t } = useI18n();
  const { workspaceId, accessToken, connected, sessionId } = useFrontendSession();
  const [datasets, setDatasets] = useState<EvaluationDataset[] | null>(null);
  const [experiments, setExperiments] = useState<EvaluationExperiment[] | null>(null);
  const [pricing, setPricing] = useState<PricingSnapshot[] | null>(null);
  const [policies, setPolicies] = useState<ReleaseGatePolicy[] | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const activeSessionRef = useRef(sessionId);
  activeSessionRef.current = sessionId;

  useEffect(() => {
    setDatasets(null);
    setExperiments(null);
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
      listDatasets(input),
      listExperiments(input),
      listPricingSnapshots(input),
      listReleaseGatePolicies(input),
    ])
      .then(([d, e, p, g]) => {
        if (activeSessionRef.current !== requestSessionId) return;
        setDatasets(d);
        setExperiments(e);
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
              value={datasets?.length ?? 0}
              hint={t("evaluation.overview.countsHint")}
              href="/evaluations/datasets"
            />
            <MetricCard
              label={t("evaluation.overview.experiments")}
              value={experiments?.length ?? 0}
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
            {datasets !== null && experiments !== null && pricing !== null && policies !== null && (
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
            {datasets?.length === 0 && experiments?.length === 0 && pricing?.length === 0 && (
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
