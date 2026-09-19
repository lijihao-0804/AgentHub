"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";

import HashValue from "../../../components/evaluation/hash-value";
import { EmptyState, ErrorState, LoadingState, Panel, SessionRequired } from "../../../components/states";
import TechnicalDetails from "../../../components/technical-details";
import { ApiError, toApiError } from "../../../lib/api-client";
import {
  GateRule,
  ReleaseGatePolicy,
  createReleaseGatePolicy,
  listReleaseGatePolicies,
} from "../../../lib/evaluation";
import { useFrontendSession } from "../../../components/session-provider";
import { useI18n } from "../../../i18n/provider";

const RULE_TYPES = [
  "NO_REGRESSION",
  "MAX_ABSOLUTE_REGRESSION",
  "MAX_RELATIVE_REGRESSION",
  "MIN_VALUE",
  "MAX_VALUE",
  "TRADEOFF",
] as const;

const TOLERANCE_RULES = new Set(["MAX_ABSOLUTE_REGRESSION", "MAX_RELATIVE_REGRESSION", "TRADEOFF"]);
const THRESHOLD_RULES = new Set(["MIN_VALUE", "MAX_VALUE"]);

type DraftRule = {
  metric: string;
  rule: (typeof RULE_TYPES)[number];
  required: boolean;
  safety: boolean;
  tolerance: string;
  threshold: string;
  compensationMetric: string;
  minCompensationGain: string;
};

function newRule(): DraftRule {
  return {
    metric: "",
    rule: "NO_REGRESSION",
    required: true,
    safety: false,
    tolerance: "",
    threshold: "",
    compensationMetric: "",
    minCompensationGain: "",
  };
}

/** Client-side validation per the documented backend contract; the backend remains the authority. */
function buildPolicyJson(
  rules: DraftRule[],
  translate: (key: string) => string,
): { policy_json: { rules: GateRule[] } } | { error: string } {
  if (rules.length === 0) return { error: translate("evaluation.gates.builder.needOneRule") };
  const out: GateRule[] = [];
  const seen = new Set<string>();
  for (const rule of rules) {
    const metric = rule.metric.trim();
    if (!metric) return { error: translate("evaluation.gates.builder.metricRequired") };
    if (seen.has(metric)) return { error: translate("evaluation.gates.builder.metricRequired") + ` (${metric})` };
    seen.add(metric);
    if (rule.safety && rule.rule === "TRADEOFF") {
      return { error: translate("evaluation.gates.builder.safetyTradeoffError") };
    }
    const gate: GateRule = { metric, rule: rule.rule, required: rule.required, safety: rule.safety };
    if (TOLERANCE_RULES.has(rule.rule)) {
      const tolerance = Number(rule.tolerance);
      if (rule.tolerance.trim() === "" || !Number.isFinite(tolerance) || tolerance < 0) {
        return { error: translate("evaluation.gates.builder.toleranceError") };
      }
      gate.tolerance = tolerance;
    }
    if (THRESHOLD_RULES.has(rule.rule)) {
      const threshold = Number(rule.threshold);
      if (rule.threshold.trim() === "" || !Number.isFinite(threshold)) {
        return { error: translate("evaluation.gates.builder.thresholdError") };
      }
      gate.threshold = threshold;
    }
    if (rule.rule === "TRADEOFF") {
      const compensation = rule.compensationMetric.trim();
      const gain = Number(rule.minCompensationGain);
      if (!compensation || compensation === metric) {
        return { error: translate("evaluation.gates.builder.sameMetricError") };
      }
      if (rule.minCompensationGain.trim() === "" || !Number.isFinite(gain) || gain < 0) {
        return { error: translate("evaluation.gates.builder.gainError") };
      }
      gate.compensation_metric = compensation;
      gate.min_compensation_gain = gain;
    }
    out.push(gate);
  }
  return { policy_json: { rules: out } };
}

export default function ReleaseGatePoliciesPage() {
  const { t, formatDateTime } = useI18n();
  const { workspaceId, accessToken, connected } = useFrontendSession();
  const [policies, setPolicies] = useState<ReleaseGatePolicy[]>([]);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);

  const [showForm, setShowForm] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [rules, setRules] = useState<DraftRule[]>([newRule()]);
  const [creating, setCreating] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [createdNotice, setCreatedNotice] = useState(false);

  const input = { workspaceId, accessToken };

  const refresh = useCallback(async () => {
    setError(null);
    if (!connected) return;
    setLoading(true);
    try {
      setPolicies(await listReleaseGatePolicies(input));
      setLoaded(true);
    } catch (caught) {
      setError(toApiError(caught, ""));
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [connected, workspaceId, accessToken]);

  useEffect(() => {
    if (connected && !loaded) void refresh();
  }, [connected, loaded, refresh]);

  useEffect(() => {
    if (!connected) setLoaded(false);
  }, [connected]);

  function updateRule(index: number, patch: Partial<DraftRule>) {
    setRules((current) => current.map((rule, i) => (i === index ? { ...rule, ...patch } : rule)));
  }

  async function submitCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setFormError(null);
    const built = buildPolicyJson(rules, (key) => t(key as never));
    if ("error" in built) {
      setFormError(built.error);
      return;
    }
    setCreating(true);
    try {
      await createReleaseGatePolicy(input, {
        name: name.trim(),
        description: description.trim() || null,
        policy_json: built.policy_json,
      });
      setName("");
      setDescription("");
      setRules([newRule()]);
      setShowForm(false);
      setCreatedNotice(true);
      await refresh();
    } catch (caught) {
      const apiError = toApiError(caught, "");
      setFormError(apiError.message || apiError.code);
    } finally {
      setCreating(false);
    }
  }

  if (!connected) {
    return (
      <div className="page">
        <header className="page-header">
          <p className="eyebrow">{t("evaluation.gates.eyebrow")}</p>
          <h1>{t("evaluation.gates.title")}</h1>
        </header>
        <SessionRequired contextKey="session.context.evaluations" />
      </div>
    );
  }

  return (
    <div className="page">
      <header className="page-header">
        <p className="eyebrow">{t("evaluation.gates.eyebrow")}</p>
        <h1>{t("evaluation.gates.title")}</h1>
        <p className="page-lede">{t("evaluation.gates.lede")}</p>
      </header>

      <Panel
        title={t("evaluation.gates.title")}
        eyebrow={t("evaluation.gates.eyebrow")}
        actions={
          <button type="button" className="button button-primary" onClick={() => setShowForm((v) => !v)}>
            {t("evaluation.gates.create")}
          </button>
        }
      >
        {createdNotice && <p className="inline-notice">{t("evaluation.gates.createdNotice")}</p>}
        <p className="state-hint">{t("evaluation.gates.immutableNotice")}</p>

        {showForm && (
          <form className="eval-form" onSubmit={submitCreate} noValidate>
            <div className="eval-form-title">{t("evaluation.gates.formTitle")}</div>
            <div className="form-grid">
              <label>
                {t("evaluation.gates.labels.name")}
                <input required value={name} onChange={(e) => setName(e.target.value)} spellCheck={false} />
              </label>
              <label>
                {t("evaluation.gates.labels.description")}
                <textarea rows={2} value={description} onChange={(e) => setDescription(e.target.value)} />
              </label>
            </div>

            <p className="eyebrow">{t("evaluation.gates.labels.rules")}</p>
            {rules.map((rule, index) => (
              <fieldset className="rule-builder" key={index}>
                <legend>
                  Rule {index + 1}
                  {rules.length > 1 && (
                    <button
                      type="button"
                      className="button button-ghost rule-remove"
                      onClick={() => setRules((current) => current.filter((_, i) => i !== index))}
                    >
                      {t("evaluation.gates.builder.remove")}
                    </button>
                  )}
                </legend>
                <div className="form-grid">
                  <label>
                    {t("evaluation.gates.builder.metric")}
                    <input
                      required
                      value={rule.metric}
                      onChange={(e) => updateRule(index, { metric: e.target.value })}
                      spellCheck={false}
                    />
                    <span className="state-hint">{t("evaluation.gates.builder.metricHint")}</span>
                  </label>
                  <label>
                    {t("evaluation.gates.builder.ruleType")}
                    <select
                      value={rule.rule}
                      onChange={(e) => updateRule(index, { rule: e.target.value as DraftRule["rule"] })}
                    >
                      {RULE_TYPES.map((type) => (
                        <option value={type} key={type}>
                          {type}
                        </option>
                      ))}
                    </select>
                    <span className="state-hint">{t(`evaluation.gates.builder.ruleDescriptions.${rule.rule}` as never)}</span>
                  </label>
                  {TOLERANCE_RULES.has(rule.rule) && (
                    <label>
                      {t("evaluation.gates.builder.tolerance")}
                      <input
                        required
                        inputMode="decimal"
                        value={rule.tolerance}
                        onChange={(e) => updateRule(index, { tolerance: e.target.value })}
                        spellCheck={false}
                      />
                    </label>
                  )}
                  {THRESHOLD_RULES.has(rule.rule) && (
                    <label>
                      {t("evaluation.gates.builder.threshold")}
                      <input
                        required
                        inputMode="decimal"
                        value={rule.threshold}
                        onChange={(e) => updateRule(index, { threshold: e.target.value })}
                        spellCheck={false}
                      />
                    </label>
                  )}
                  {rule.rule === "TRADEOFF" && (
                    <>
                      <label>
                        {t("evaluation.gates.builder.compensationMetric")}
                        <input
                          required
                          value={rule.compensationMetric}
                          onChange={(e) => updateRule(index, { compensationMetric: e.target.value })}
                          spellCheck={false}
                        />
                      </label>
                      <label>
                        {t("evaluation.gates.builder.minCompensationGain")}
                        <input
                          required
                          inputMode="decimal"
                          value={rule.minCompensationGain}
                          onChange={(e) => updateRule(index, { minCompensationGain: e.target.value })}
                          spellCheck={false}
                        />
                      </label>
                    </>
                  )}
                  <label className="rule-checkbox">
                    <input
                      type="checkbox"
                      checked={rule.required}
                      onChange={(e) => updateRule(index, { required: e.target.checked })}
                    />
                    {t("evaluation.gates.builder.required")}
                  </label>
                  <label className="rule-checkbox">
                    <input
                      type="checkbox"
                      checked={rule.safety}
                      onChange={(e) => updateRule(index, { safety: e.target.checked })}
                    />
                    {t("evaluation.gates.builder.safety")}
                  </label>
                </div>
                {rule.safety && rule.rule === "TRADEOFF" && (
                  <p className="session-error" role="alert">{t("evaluation.gates.builder.safetyTradeoffError")}</p>
                )}
              </fieldset>
            ))}
            <button
              type="button"
              className="button button-ghost"
              onClick={() => setRules((current) => [...current, newRule()])}
            >
              {t("evaluation.gates.builder.add")}
            </button>

            {formError && <p className="session-error" role="alert">{formError}</p>}
            <div className="form-actions">
              <button type="submit" className="button button-primary" disabled={creating}>
                {creating ? t("evaluation.gates.creating") : t("evaluation.gates.create")}
              </button>
            </div>
          </form>
        )}

        {error && (
          <ErrorState code={error.code} message={error.message || t("errors.loadEvaluation")} onRetry={() => void refresh()} />
        )}
        {loading && policies.length === 0 && !error && <LoadingState />}
        {loaded && policies.length === 0 && !error && (
          <EmptyState title={t("evaluation.gates.empty")} hint={t("evaluation.gates.emptyHint")} />
        )}

        {policies.length > 0 && (
          <div className="data-table">
            <table>
              <thead>
                <tr>
                  <th scope="col">{t("evaluation.gates.columns.name")}</th>
                  <th scope="col">{t("evaluation.gates.columns.description")}</th>
                  <th scope="col">{t("evaluation.gates.columns.rules")}</th>
                  <th scope="col">{t("evaluation.gates.columns.hash")}</th>
                  <th scope="col">{t("evaluation.gates.columns.created")}</th>
                </tr>
              </thead>
              <tbody>
                {policies.map((policy) => (
                  <tr key={policy.id}>
                    <td data-label={t("evaluation.gates.columns.name")}>{policy.name}</td>
                    <td data-label={t("evaluation.gates.columns.description")} className="muted">
                      {policy.description ?? "—"}
                    </td>
                    <td data-label={t("evaluation.gates.columns.rules")}>
                      {t("evaluation.gates.ruleCount", { count: policy.policy_json.rules.length })}
                      <TechnicalDetails value={policy.policy_json} summary={t("common.technicalDetails")} />
                    </td>
                    <td data-label={t("evaluation.gates.columns.hash")}>
                      <HashValue value={policy.policy_hash} label={t("evaluation.gates.columns.hash")} />
                    </td>
                    <td data-label={t("evaluation.gates.columns.created")}>{formatDateTime(policy.created_at)}</td>
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
