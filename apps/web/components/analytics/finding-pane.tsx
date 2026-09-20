"use client";

import { useEffect, useState, type FormEvent } from "react";

import { queryResults } from "@/components/analytics/queries-pane";
import { EmptyState, InlineError } from "@/components/ui/states";
import { useFrontendSession } from "@/components/providers/session-provider";
import { useWorkspaceMutation, type WorkspaceQuery } from "@/hooks/use-workspace-data";
import {
  createThreadArtifact,
  queryResultPayload,
  readAnalysisFindingContent,
  ANALYSIS_FINDING,
  type Artifact,
} from "@/lib/api/artifacts";
import { useI18n } from "@/i18n/provider";

const TITLE_LIMIT = 200;

/**
 * The finding: the one thing on an analysis thread a person writes.
 *
 * The conclusion is prose, and it is prose a person is accountable for. What
 * sits under it is not: the attached queries are copied out of the recorded
 * `analysis.query_result` artifacts, so the finding keeps saying what the
 * numbers said on the day it was written.
 */
export default function FindingPane({
  threadId,
  artifacts,
}: {
  threadId: string;
  artifacts: WorkspaceQuery<Artifact[]>;
}) {
  const { t, formatDateTime, formatNumber } = useI18n();
  const { workspaceId } = useFrontendSession();
  const mutation = useWorkspaceMutation(`analytics-finding:${workspaceId}:${threadId}`);

  const [question, setQuestion] = useState("");
  const [conclusion, setConclusion] = useState("");
  const [evidence, setEvidence] = useState<string[]>([]);
  const [evidenceDraft, setEvidenceDraft] = useState("");
  const [attachQueries, setAttachQueries] = useState(true);

  // Another thread is another question: nothing half-written carries over.
  useEffect(() => {
    setQuestion("");
    setConclusion("");
    setEvidence([]);
    setEvidenceDraft("");
    setAttachQueries(true);
  }, [threadId]);

  const artifactList = artifacts.data ?? [];
  const recorded = queryResults(artifactList);
  const findings = artifactList
    .filter((artifact) => artifact.type === ANALYSIS_FINDING)
    .map((artifact) => ({ artifact, finding: readAnalysisFindingContent(artifact.content) }));

  const ready = question.trim() !== "" && conclusion.trim() !== "";

  function addEvidence() {
    const item = evidenceDraft.trim();
    if (!item) return;
    setEvidence((current) => [...current, item]);
    setEvidenceDraft("");
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!ready || mutation.pending) return;
    const trimmed = question.trim();
    const created = await mutation.run((auth) =>
      createThreadArtifact(auth, threadId, {
        type: ANALYSIS_FINDING,
        title: trimmed.slice(0, TITLE_LIMIT),
        content: {
          question: trimmed,
          conclusion: conclusion.trim(),
          evidence,
          /*
           * The model never writes an artifact, and neither does this form
           * re-type one. Every `analysis.query_result` on the thread was
           * recorded by the backend from a tool result, provenance included;
           * attaching means copying those recorded contents through
           * `queryResultPayload` unchanged. A later run may supersede the
           * query — the copy in the finding is what was true when the
           * conclusion was drawn, which is the only thing that makes the
           * conclusion checkable afterwards.
           */
          queries: attachQueries ? recorded.map(({ query }) => queryResultPayload(query)) : [],
        },
      }),
    );
    if (created) {
      setQuestion("");
      setConclusion("");
      setEvidence([]);
      setEvidenceDraft("");
      artifacts.reload();
    }
  }

  return (
    <section className="research-pane" aria-label={t("analytics.finding.paneTitle")}>
      <div className="research-pane-head">
        <h2>{t("analytics.finding.paneTitle")}</h2>
      </div>

      <div className="research-pane-body">
        <form className="eval-form" onSubmit={submit} noValidate>
          <p className="eval-form-title">{t("analytics.finding.compose")}</p>
          <label>
            {t("analytics.finding.question")}
            <input
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              placeholder={t("analytics.finding.questionPlaceholder")}
              maxLength={2000}
            />
          </label>
          <label>
            {t("analytics.finding.conclusion")}
            <textarea
              rows={3}
              value={conclusion}
              onChange={(event) => setConclusion(event.target.value)}
              placeholder={t("analytics.finding.conclusionPlaceholder")}
              maxLength={4000}
            />
          </label>

          <div className="string-list">
            <label>
              {t("analytics.finding.evidence")}
              <input
                value={evidenceDraft}
                onChange={(event) => setEvidenceDraft(event.target.value)}
                placeholder={t("analytics.finding.evidencePlaceholder")}
                maxLength={1000}
              />
            </label>
            <button
              type="button"
              className="button button-ghost"
              onClick={addEvidence}
              disabled={!evidenceDraft.trim()}
            >
              {t("analytics.finding.add")}
            </button>
            {evidence.length > 0 && (
              <ul className="string-list-items">
                {evidence.map((item, index) => (
                  <li key={`${index}:${item}`}>
                    <span>{item}</span>
                    <button
                      type="button"
                      className="button button-ghost"
                      onClick={() => setEvidence((current) => current.filter((_, at) => at !== index))}
                    >
                      {t("common.remove")}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <label className="checkbox-label">
            <input
              type="checkbox"
              checked={attachQueries}
              onChange={(event) => setAttachQueries(event.target.checked)}
            />
            <span>
              {t("analytics.finding.attachQueries")} —{" "}
              {t("analytics.finding.queryCount", { count: formatNumber(recorded.length) })}
            </span>
          </label>
          <p className="state-hint">{t("analytics.finding.attachHint")}</p>

          <InlineError error={mutation.error} fallback={t("errors.requestFailed")} />
          {!ready && <p className="state-hint">{t("analytics.finding.needsFields")}</p>}
          <div className="form-actions">
            <button type="submit" className="button button-primary" disabled={!ready || mutation.pending}>
              {mutation.pending ? t("analytics.finding.saving") : t("analytics.finding.save")}
            </button>
          </div>
        </form>

        {artifacts.loaded && findings.length === 0 && (
          <EmptyState title={t("analytics.finding.empty")} hint={t("analytics.finding.emptyHint")} />
        )}

        <div className="artifact-list">
          {findings.map(({ artifact, finding }) => (
            <article className="artifact-card" key={artifact.id}>
              <header className="artifact-card-head">
                <div>
                  <h3>{finding.question || artifact.title}</h3>
                  <p className="artifact-card-meta">
                    <span>{t("analytics.finding.queryCount", { count: formatNumber(finding.queries.length) })}</span>
                    <span>{formatDateTime(artifact.created_at)}</span>
                  </p>
                </div>
              </header>
              <p className="artifact-note">{finding.conclusion}</p>
              {finding.evidence.length > 0 && (
                <ul className="string-list-items">
                  {finding.evidence.map((item, index) => (
                    <li key={`${artifact.id}:${index}`}>
                      <span>{item}</span>
                    </li>
                  ))}
                </ul>
              )}
            </article>
          ))}
        </div>
      </div>
    </section>
  );
}
