"use client";

import { useEffect, useState, type FormEvent } from "react";

import { latestHandoff } from "@/components/support/context-pane";
import { EmptyState, InlineError } from "@/components/ui/states";
import { useFrontendSession } from "@/components/providers/session-provider";
import { useWorkspaceMutation, type WorkspaceQuery } from "@/hooks/use-workspace-data";
import {
  createThreadArtifact,
  readSupportHandoffContent,
  SUPPORT_HANDOFF,
  type Artifact,
} from "@/lib/api/artifacts";
import { useI18n } from "@/i18n/provider";

const TITLE_LIMIT = 200;

/**
 * Agent → human escalation, written down.
 *
 * The handoff is the case's most valuable artifact because it is the one that
 * stops the work being repeated: what was asked, what was already checked,
 * what that turned up, and what the person picking it up should do. The
 * customer and case references are carried over from the last handoff so a
 * second escalation on the same case does not lose them.
 */
export default function HandoffPane({
  threadId,
  artifacts,
}: {
  threadId: string;
  artifacts: WorkspaceQuery<Artifact[]>;
}) {
  const { t, formatDateTime } = useI18n();
  const { workspaceId } = useFrontendSession();
  const mutation = useWorkspaceMutation(`support-handoff:${workspaceId}:${threadId}`);

  const [customerRef, setCustomerRef] = useState("");
  const [caseRef, setCaseRef] = useState("");
  const [problem, setProblem] = useState("");
  const [checked, setChecked] = useState<string[]>([]);
  const [checkedDraft, setCheckedDraft] = useState("");
  const [findings, setFindings] = useState<string[]>([]);
  const [findingsDraft, setFindingsDraft] = useState("");
  const [recommendedAction, setRecommendedAction] = useState("");
  const [reason, setReason] = useState("");

  // Another case is another escalation: nothing half-written carries over.
  useEffect(() => {
    setCustomerRef("");
    setCaseRef("");
    setProblem("");
    setChecked([]);
    setCheckedDraft("");
    setFindings([]);
    setFindingsDraft("");
    setRecommendedAction("");
    setReason("");
  }, [threadId]);

  const artifactList = artifacts.data ?? [];
  const handoffs = artifactList
    .filter((artifact) => artifact.type === SUPPORT_HANDOFF)
    .map((artifact) => ({ artifact, handoff: readSupportHandoffContent(artifact.content) }));
  const previous = latestHandoff(artifactList);
  const carried = previous ? readSupportHandoffContent(previous.content) : null;

  // A second escalation on the same case starts from the first one's
  // references, but they stay editable: the first escalation has nothing to
  // inherit from, and a case that was opened against the wrong customer has to
  // be correctable without leaving the composer.
  const carriedCustomer = carried?.customer_ref ?? null;
  const carriedCase = carried?.case_ref ?? null;
  useEffect(() => {
    if (carriedCustomer) setCustomerRef((current) => current || carriedCustomer);
    if (carriedCase) setCaseRef((current) => current || carriedCase);
  }, [carriedCustomer, carriedCase]);

  const ready = problem.trim() !== "" && recommendedAction.trim() !== "";

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!ready || mutation.pending) return;
    const trimmed = problem.trim();
    const created = await mutation.run((auth) =>
      createThreadArtifact(auth, threadId, {
        type: SUPPORT_HANDOFF,
        title: trimmed.slice(0, TITLE_LIMIT),
        content: {
          customer_ref: customerRef.trim() === "" ? null : customerRef.trim(),
          case_ref: caseRef.trim() === "" ? null : caseRef.trim(),
          problem: trimmed,
          checked,
          findings,
          recommended_action: recommendedAction.trim(),
          reason: reason.trim() === "" ? null : reason.trim(),
        },
      }),
    );
    if (created) {
      setProblem("");
      setChecked([]);
      setCheckedDraft("");
      setFindings([]);
      setFindingsDraft("");
      setRecommendedAction("");
      setReason("");
      artifacts.reload();
    }
  }

  return (
    <section className="research-pane" aria-label={t("support.handoff.paneTitle")}>
      <div className="research-pane-head">
        <h2>{t("support.handoff.paneTitle")}</h2>
      </div>

      <div className="research-pane-body">
        <form className="eval-form" onSubmit={submit} noValidate>
          <p className="eval-form-title">{t("support.handoff.compose")}</p>
          <p className="state-hint">{t("support.handoff.composeHint")}</p>

          <div className="form-grid">
            <label>
              {t("support.context.customerRef")}
              <input
                value={customerRef}
                onChange={(event) => setCustomerRef(event.target.value)}
                maxLength={200}
              />
            </label>
            <label>
              {t("support.context.caseRef")}
              <input
                value={caseRef}
                onChange={(event) => setCaseRef(event.target.value)}
                maxLength={200}
              />
            </label>
          </div>

          <label>
            {t("support.handoff.problem")}
            <textarea
              rows={2}
              value={problem}
              onChange={(event) => setProblem(event.target.value)}
              placeholder={t("support.handoff.problemPlaceholder")}
              maxLength={4000}
            />
          </label>

          <div className="string-list">
            <label>
              {t("support.handoff.checked")}
              <input
                value={checkedDraft}
                onChange={(event) => setCheckedDraft(event.target.value)}
                placeholder={t("support.handoff.checkedPlaceholder")}
                maxLength={1000}
              />
            </label>
            <button
              type="button"
              className="button button-ghost"
              onClick={() => {
                const item = checkedDraft.trim();
                if (!item) return;
                setChecked((current) => [...current, item]);
                setCheckedDraft("");
              }}
              disabled={!checkedDraft.trim()}
            >
              {t("support.handoff.add")}
            </button>
            {checked.length > 0 && (
              <ul className="string-list-items">
                {checked.map((item, index) => (
                  <li key={`checked:${index}:${item}`}>
                    <span>{item}</span>
                    <button
                      type="button"
                      className="button button-ghost"
                      onClick={() => setChecked((current) => current.filter((_, at) => at !== index))}
                    >
                      {t("common.remove")}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="string-list">
            <label>
              {t("support.handoff.findings")}
              <input
                value={findingsDraft}
                onChange={(event) => setFindingsDraft(event.target.value)}
                placeholder={t("support.handoff.findingsPlaceholder")}
                maxLength={1000}
              />
            </label>
            <button
              type="button"
              className="button button-ghost"
              onClick={() => {
                const item = findingsDraft.trim();
                if (!item) return;
                setFindings((current) => [...current, item]);
                setFindingsDraft("");
              }}
              disabled={!findingsDraft.trim()}
            >
              {t("support.handoff.add")}
            </button>
            {findings.length > 0 && (
              <ul className="string-list-items">
                {findings.map((item, index) => (
                  <li key={`finding:${index}:${item}`}>
                    <span>{item}</span>
                    <button
                      type="button"
                      className="button button-ghost"
                      onClick={() => setFindings((current) => current.filter((_, at) => at !== index))}
                    >
                      {t("common.remove")}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <label>
            {t("support.handoff.recommendedAction")}
            <textarea
              rows={2}
              value={recommendedAction}
              onChange={(event) => setRecommendedAction(event.target.value)}
              placeholder={t("support.handoff.recommendedActionPlaceholder")}
              maxLength={4000}
            />
          </label>
          <label>
            {t("support.handoff.reason")}
            <input
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              placeholder={t("support.handoff.reasonPlaceholder")}
              maxLength={1000}
            />
          </label>

          <InlineError error={mutation.error} fallback={t("errors.requestFailed")} />
          {!ready && <p className="state-hint">{t("support.handoff.needsFields")}</p>}
          <div className="form-actions">
            <button type="submit" className="button button-primary" disabled={!ready || mutation.pending}>
              {mutation.pending ? t("support.handoff.saving") : t("support.handoff.save")}
            </button>
          </div>
        </form>

        {artifacts.loaded && handoffs.length === 0 && (
          <EmptyState title={t("support.handoff.empty")} hint={t("support.handoff.emptyHint")} />
        )}

        <div className="artifact-list">
          {handoffs.map(({ artifact, handoff }) => (
            <article className="artifact-card" key={artifact.id}>
              <header className="artifact-card-head">
                <div>
                  <h3>{handoff.problem || artifact.title}</h3>
                  <p className="artifact-card-meta">
                    <span>{formatDateTime(artifact.created_at)}</span>
                  </p>
                </div>
              </header>
              <dl className="artifact-facts">
                <div>
                  <dt>{t("support.context.customerRef")}</dt>
                  <dd>{handoff.customer_ref ?? t("common.none")}</dd>
                </div>
                <div>
                  <dt>{t("support.context.caseRef")}</dt>
                  <dd>{handoff.case_ref ?? t("common.none")}</dd>
                </div>
                <div>
                  <dt>{t("support.handoff.recommendedAction")}</dt>
                  <dd>{handoff.recommended_action}</dd>
                </div>
                {handoff.reason && (
                  <div>
                    <dt>{t("support.handoff.reason")}</dt>
                    <dd>{handoff.reason}</dd>
                  </div>
                )}
              </dl>
              {handoff.checked.length > 0 && (
                <>
                  <p className="artifact-card-meta">{t("support.handoff.checked")}</p>
                  <ul className="string-list-items">
                    {handoff.checked.map((item, index) => (
                      <li key={`${artifact.id}:checked:${index}`}>
                        <span>{item}</span>
                      </li>
                    ))}
                  </ul>
                </>
              )}
              {handoff.findings.length > 0 && (
                <>
                  <p className="artifact-card-meta">{t("support.handoff.findings")}</p>
                  <ul className="string-list-items">
                    {handoff.findings.map((item, index) => (
                      <li key={`${artifact.id}:finding:${index}`}>
                        <span>{item}</span>
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </article>
          ))}
        </div>
      </div>
    </section>
  );
}
