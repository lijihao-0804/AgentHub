"use client";

import Link from "next/link";
import { useMemo, useState, type FormEvent } from "react";

import { EmptyState, ErrorState, InlineError, LoadingState } from "@/components/ui/states";
import { useWorkspaceMutation, type WorkspaceQuery } from "@/hooks/use-workspace-data";
import { errorHintKey } from "@/lib/api/client";
import {
  createThreadArtifact,
  INCIDENT_REPORT,
  INCIDENT_TIMELINE,
  mergeTimelines,
  readIncidentReportContent,
  timelineEntryPayload,
  type Artifact,
  type TimelineEntry,
} from "@/lib/api/artifacts";
import { useI18n } from "@/i18n/provider";

const HYPOTHESIS_STATUSES = ["INSUFFICIENT", "SUPPORTED"] as const;

type HypothesisStatus = (typeof HYPOTHESIS_STATUSES)[number];

type Hypothesis = {
  id: string;
  name: string;
  evidence: string[];
  status: HypothesisStatus;
};

function localId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `${Date.now().toString(16)}-${Math.random().toString(16).slice(2)}`;
}

function entryLabel(entry: TimelineEntry): string {
  return `${entry.at} · ${entry.summary}`;
}

/**
 * A repeatable list of short strings, used for actions and risks alike.
 *
 * The two report fields that are lists are lists of sentences a person wrote,
 * not of anything the run recorded, so they get a plain editor rather than
 * anything that pretends to be evidence.
 */
function StringList({
  label,
  placeholder,
  addLabel,
  removeLabel,
  values,
  onChange,
  disabled,
}: {
  label: string;
  placeholder: string;
  addLabel: string;
  removeLabel: string;
  values: string[];
  onChange: (next: string[]) => void;
  disabled: boolean;
}) {
  const [draft, setDraft] = useState("");

  function add() {
    const text = draft.trim();
    if (!text) return;
    onChange([...values, text]);
    setDraft("");
  }

  return (
    <div className="string-list">
      <label>
        {label}
        <input
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder={placeholder}
          maxLength={500}
          disabled={disabled}
        />
      </label>
      <button
        type="button"
        className="button button-ghost"
        onClick={add}
        disabled={disabled || !draft.trim()}
      >
        {addLabel}
      </button>
      {values.length > 0 && (
        <ul className="string-list-items">
          {values.map((value, index) => (
            <li key={`${index}-${value}`}>
              <span>{value}</span>
              <button
                type="button"
                className="button button-ghost"
                onClick={() => onChange(values.filter((_, position) => position !== index))}
                disabled={disabled}
              >
                {removeLabel}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * The two panes beside an incident conversation: what was observed, and what
 * the investigator concluded from it.
 *
 * The split is the point. Timeline entries are recorded by the runtime from
 * tool results and are not editable here; hypotheses and the report are
 * written by a person. Nothing a model said becomes an artifact on its own.
 */
export default function IncidentPanes({
  threadId,
  artifacts,
}: {
  threadId: string;
  artifacts: WorkspaceQuery<Artifact[]>;
}) {
  const { t, formatDateTime, formatNumber } = useI18n();
  const mutation = useWorkspaceMutation(`app-artifacts:incident:${threadId}`);

  const artifactList = useMemo(() => artifacts.data ?? [], [artifacts.data]);
  const timeline = useMemo(() => mergeTimelines(artifactList), [artifactList]);
  const recordedCount = artifactList.filter((item) => item.type === INCIDENT_TIMELINE).length;
  const reports = artifactList.filter((item) => item.type === INCIDENT_REPORT);

  // Hypotheses are a thinking aid, not a record. They stay in the browser
  // until the investigator commits to them in the report, which is the only
  // thing in this pane that becomes an artifact.
  const [hypotheses, setHypotheses] = useState<Hypothesis[]>([]);
  const [hypothesisName, setHypothesisName] = useState("");
  const [evidencePick, setEvidencePick] = useState<Record<string, string>>({});

  const [composing, setComposing] = useState(false);
  const [incidentRef, setIncidentRef] = useState("");
  const [severity, setSeverity] = useState("");
  const [summary, setSummary] = useState("");
  const [impact, setImpact] = useState("");
  const [rootCause, setRootCause] = useState("");
  const [actions, setActions] = useState<string[]>([]);
  const [risks, setRisks] = useState<string[]>([]);
  const [includeTimeline, setIncludeTimeline] = useState(true);

  function addHypothesis() {
    const name = hypothesisName.trim();
    if (!name) return;
    setHypotheses((current) => [
      ...current,
      { id: localId(), name, evidence: [], status: "INSUFFICIENT" },
    ]);
    setHypothesisName("");
  }

  function updateHypothesis(id: string, patch: Partial<Hypothesis>) {
    setHypotheses((current) =>
      current.map((item) => (item.id === id ? { ...item, ...patch } : item)),
    );
  }

  async function saveReport(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const text = summary.trim();
    if (!text) return;
    const content: Record<string, unknown> = {
      incident_ref: incidentRef.trim() || null,
      severity: severity.trim() || null,
      summary: text,
      impact: impact.trim() || null,
      // A copy, deliberately. The thread keeps running after the report is
      // filed, and a report that silently grew new evidence afterwards would
      // no longer be the thing that was reviewed.
      timeline: includeTimeline ? timeline.map(timelineEntryPayload) : [],
      root_cause: rootCause.trim() || null,
      actions_taken: actions,
      remaining_risks: risks,
    };
    const created = await mutation.run((auth) =>
      createThreadArtifact(auth, threadId, {
        type: INCIDENT_REPORT,
        title: (incidentRef.trim() || text).slice(0, 200),
        content,
      }),
    );
    if (created) {
      setComposing(false);
      setIncidentRef("");
      setSeverity("");
      setSummary("");
      setImpact("");
      setRootCause("");
      setActions([]);
      setRisks([]);
      artifacts.reload();
    }
  }

  return (
    // One grid column, two panes in it: the workbench frame is three columns
    // wide for every application, and an application that needs more surface
    // stacks inside its own column rather than widening the frame.
    <div className="app-pane-stack">
      <section className="research-pane research-pane-artifacts" aria-label={t("incidents.timeline.paneTitle")}>
        <div className="research-pane-head">
          <h2>{t("incidents.timeline.paneTitle")}</h2>
          <button
            type="button"
            className="button button-ghost"
            onClick={artifacts.reload}
            disabled={artifacts.loading}
          >
            {t("common.refresh")}
          </button>
        </div>
        <div className="research-pane-body">
          {artifacts.error && (
            <ErrorState
              code={artifacts.error.code}
              message={artifacts.error.message || t("errors.loadArtifacts")}
              hint={errorHintKey(artifacts.error) ? t(errorHintKey(artifacts.error)!) : undefined}
              onRetry={artifacts.reload}
            />
          )}
          {artifacts.loading && artifactList.length === 0 && !artifacts.error && <LoadingState />}
          {artifacts.loaded && !artifacts.error && timeline.length === 0 && (
            <EmptyState
              title={t("incidents.timeline.empty")}
              hint={t("incidents.timeline.emptyHint")}
            />
          )}

          {timeline.length > 0 && (
            <>
              <p className="state-hint">
                {t("incidents.timeline.entries", { count: formatNumber(timeline.length) })} ·{" "}
                {t("incidents.timeline.merged", { count: formatNumber(recordedCount) })}
              </p>
              <ol className="incident-timeline">
                {timeline.map((entry) => (
                  <li className="incident-timeline-entry" key={`${entry.at}|${entry.kind}|${entry.summary}`}>
                    <div className="incident-timeline-head">
                      <span className={`incident-kind incident-kind-${entry.kind.toLowerCase()}`}>
                        {t(`incidents.timeline.kinds.${entry.kind}`)}
                      </span>
                      <code className="incident-timeline-at">{entry.at}</code>
                    </div>
                    <p className="incident-timeline-summary">{entry.summary}</p>
                    {entry.detail && <p className="incident-timeline-detail">{entry.detail}</p>}
                    {entry.provenance && (
                      <p className="conversation-meta">
                        {t("appThread.artifacts.provenanceTool")}: <code>{entry.provenance.tool_identity}</code>
                        {" · "}
                        <Link href={`/runs/${encodeURIComponent(entry.provenance.run_id)}`}>
                          {t("appThread.artifacts.provenanceRun")}
                        </Link>
                      </p>
                    )}
                  </li>
                ))}
              </ol>
            </>
          )}

          <div className="incident-hypotheses">
            <h3>{t("incidents.hypotheses.paneTitle")}</h3>
            <p className="state-hint">{t("incidents.hypotheses.note")}</p>
            <p className="state-hint">{t("incidents.hypotheses.localOnly")}</p>

            <div className="string-list">
              <label>
                {t("incidents.hypotheses.name")}
                <input
                  value={hypothesisName}
                  onChange={(event) => setHypothesisName(event.target.value)}
                  placeholder={t("incidents.hypotheses.namePlaceholder")}
                  maxLength={300}
                />
              </label>
              <button
                type="button"
                className="button button-ghost"
                onClick={addHypothesis}
                disabled={!hypothesisName.trim()}
              >
                {t("incidents.hypotheses.add")}
              </button>
            </div>

            {hypotheses.length === 0 ? (
              <EmptyState
                title={t("incidents.hypotheses.empty")}
                hint={t("incidents.hypotheses.emptyHint")}
              />
            ) : (
              <ul className="hypothesis-list">
                {hypotheses.map((hypothesis) => (
                  <li className="hypothesis-card" key={hypothesis.id}>
                    <p className="hypothesis-name">{hypothesis.name}</p>

                    <ul className="hypothesis-evidence">
                      {hypothesis.evidence.map((item, index) => (
                        <li key={`${index}-${item}`}>{item}</li>
                      ))}
                    </ul>

                    <div className="string-list">
                      <label>
                        {t("incidents.hypotheses.evidence")}
                        <select
                          value={evidencePick[hypothesis.id] ?? ""}
                          onChange={(event) =>
                            setEvidencePick((current) => ({
                              ...current,
                              [hypothesis.id]: event.target.value,
                            }))
                          }
                          disabled={timeline.length === 0}
                        >
                          <option value="">{t("incidents.hypotheses.evidencePlaceholder")}</option>
                          {timeline.map((entry) => {
                            const label = entryLabel(entry);
                            return (
                              <option value={label} key={label}>
                                {label}
                              </option>
                            );
                          })}
                        </select>
                      </label>
                      <button
                        type="button"
                        className="button button-ghost"
                        onClick={() => {
                          const picked = evidencePick[hypothesis.id];
                          if (!picked || hypothesis.evidence.includes(picked)) return;
                          updateHypothesis(hypothesis.id, {
                            evidence: [...hypothesis.evidence, picked],
                          });
                          setEvidencePick((current) => ({ ...current, [hypothesis.id]: "" }));
                        }}
                        disabled={!evidencePick[hypothesis.id]}
                      >
                        {t("incidents.hypotheses.addEvidence")}
                      </button>
                    </div>

                    <div className="hypothesis-actions">
                      <label>
                        {t("incidents.hypotheses.status")}
                        <select
                          value={hypothesis.status}
                          onChange={(event) =>
                            updateHypothesis(hypothesis.id, {
                              status: event.target.value as HypothesisStatus,
                            })
                          }
                        >
                          <option value="INSUFFICIENT">{t("incidents.hypotheses.insufficient")}</option>
                          <option value="SUPPORTED">{t("incidents.hypotheses.supported")}</option>
                        </select>
                      </label>
                      <button
                        type="button"
                        className="button button-ghost"
                        onClick={() =>
                          setHypotheses((current) =>
                            current.filter((item) => item.id !== hypothesis.id),
                          )
                        }
                      >
                        {t("incidents.hypotheses.remove")}
                      </button>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      </section>

      <section className="research-pane research-pane-report" aria-label={t("incidents.report.paneTitle")}>
        <div className="research-pane-head">
          <h2>{t("incidents.report.paneTitle")}</h2>
          <button
            type="button"
            className="button button-primary"
            onClick={() => setComposing((open) => !open)}
          >
            {t("incidents.report.compose")}
          </button>
        </div>
        <div className="research-pane-body">
          {composing && (
            <form className="eval-form" onSubmit={saveReport} noValidate>
              <div className="form-grid">
                <label>
                  {t("incidents.report.incidentRef")}
                  <input
                    value={incidentRef}
                    onChange={(event) => setIncidentRef(event.target.value)}
                    placeholder={t("incidents.report.incidentRefPlaceholder")}
                    maxLength={200}
                  />
                </label>
                <label>
                  {t("incidents.report.severity")}
                  <input
                    value={severity}
                    onChange={(event) => setSeverity(event.target.value)}
                    placeholder={t("incidents.report.severityPlaceholder")}
                    maxLength={50}
                  />
                </label>
              </div>
              <label>
                {t("incidents.report.summary")}
                <textarea
                  rows={3}
                  value={summary}
                  onChange={(event) => setSummary(event.target.value)}
                  placeholder={t("incidents.report.summaryPlaceholder")}
                  maxLength={4000}
                />
              </label>
              <label>
                {t("incidents.report.impact")}
                <textarea
                  rows={2}
                  value={impact}
                  onChange={(event) => setImpact(event.target.value)}
                  placeholder={t("incidents.report.impactPlaceholder")}
                  maxLength={4000}
                />
              </label>
              <label>
                {t("incidents.report.rootCause")}
                <textarea
                  rows={2}
                  value={rootCause}
                  onChange={(event) => setRootCause(event.target.value)}
                  placeholder={t("incidents.report.rootCausePlaceholder")}
                  maxLength={4000}
                />
              </label>

              <StringList
                label={t("incidents.report.actionsTaken")}
                placeholder={t("incidents.report.actionPlaceholder")}
                addLabel={t("incidents.report.add")}
                removeLabel={t("incidents.hypotheses.remove")}
                values={actions}
                onChange={setActions}
                disabled={mutation.pending}
              />
              <StringList
                label={t("incidents.report.remainingRisks")}
                placeholder={t("incidents.report.riskPlaceholder")}
                addLabel={t("incidents.report.add")}
                removeLabel={t("incidents.hypotheses.remove")}
                values={risks}
                onChange={setRisks}
                disabled={mutation.pending}
              />

              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={includeTimeline}
                  onChange={(event) => setIncludeTimeline(event.target.checked)}
                />
                {t("incidents.report.includeTimeline", { count: formatNumber(timeline.length) })}
              </label>
              <p className="state-hint">{t("incidents.report.includeTimelineHint")}</p>

              <InlineError error={mutation.error} fallback={t("errors.requestFailed")} />
              {!summary.trim() && <p className="state-hint">{t("incidents.report.needsSummary")}</p>}
              <div className="form-actions">
                <button
                  type="submit"
                  className="button button-primary"
                  disabled={mutation.pending || !summary.trim()}
                >
                  {mutation.pending ? t("incidents.report.saving") : t("incidents.report.save")}
                </button>
                <button
                  type="button"
                  className="button button-ghost"
                  onClick={() => setComposing(false)}
                  disabled={mutation.pending}
                >
                  {t("appThread.cancel")}
                </button>
              </div>
            </form>
          )}

          {reports.length === 0 && !composing && (
            <EmptyState title={t("incidents.report.empty")} hint={t("incidents.report.emptyHint")} />
          )}

          {reports.map((artifact) => {
            const report = readIncidentReportContent(artifact.content);
            return (
              <article className="artifact-card" key={artifact.id}>
                <header className="artifact-card-head">
                  <h3>{artifact.title}</h3>
                  <span className="artifact-card-meta">{formatDateTime(artifact.created_at)}</span>
                </header>
                {report.severity && (
                  <p className="artifact-card-meta">
                    {t("incidents.report.severity")}: {report.severity}
                  </p>
                )}
                <p>{report.summary}</p>
                {report.impact && (
                  <p>
                    <strong>{t("incidents.report.impact")}</strong>: {report.impact}
                  </p>
                )}
                {report.root_cause && (
                  <p>
                    <strong>{t("incidents.report.rootCause")}</strong>: {report.root_cause}
                  </p>
                )}
                {report.actions_taken.length > 0 && (
                  <>
                    <p>
                      <strong>{t("incidents.report.actionsTaken")}</strong>
                    </p>
                    <ul className="string-list-items">
                      {report.actions_taken.map((action, index) => (
                        <li key={`${index}-${action}`}>{action}</li>
                      ))}
                    </ul>
                  </>
                )}
                {report.remaining_risks.length > 0 && (
                  <>
                    <p>
                      <strong>{t("incidents.report.remainingRisks")}</strong>
                    </p>
                    <ul className="string-list-items">
                      {report.remaining_risks.map((risk, index) => (
                        <li key={`${index}-${risk}`}>{risk}</li>
                      ))}
                    </ul>
                  </>
                )}
                {report.timeline.length > 0 && (
                  <p className="artifact-card-meta">
                    {t("incidents.timeline.entries", { count: formatNumber(report.timeline.length) })}
                  </p>
                )}
              </article>
            );
          })}
        </div>
      </section>
    </div>
  );
}
