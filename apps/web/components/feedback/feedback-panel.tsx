"use client";
import Link from "next/link";
import { useCallback, useRef, useState, type FormEvent } from "react";
import { useFrontendSession } from "@/components/providers/session-provider";
import { useWorkspaceData, useWorkspaceMutation } from "@/hooks/use-workspace-data";
import { EmptyState, ErrorState, InlineError, LoadingState } from "@/components/ui/states";
import { useI18n } from "@/i18n/provider";
import { FEEDBACK_CATEGORIES, importFeedback, listFeedback, reviewFeedback, submitFeedback, type Feedback, type FeedbackCategory } from "@/lib/api/feedback";
import { listDatasets, listDatasetVersions } from "@/lib/api/evaluation";
import type { AuthInput } from "@/lib/api/client";

export default function FeedbackPanel(props: { runId: string; turnId?: string }) {
  const { sessionId } = useFrontendSession();
  return <FeedbackBody key={`${sessionId}:${props.runId}:${props.turnId ?? ""}`} {...props} />;
}

function FeedbackBody({ runId, turnId }: { runId: string; turnId?: string }) {
  const { t } = useI18n();
  const { permissions } = useFrontendSession();
  const [open, setOpen] = useState(false);
  const [rating, setRating] = useState<-1 | 1>(-1);
  const [category, setCategory] = useState<FeedbackCategory>("FACTUAL");
  const [comment, setComment] = useState("");
  const [answer, setAnswer] = useState("");
  const token = useRef<{ fingerprint: string; key: string } | null>(null);
  const load = useCallback((auth: AuthInput) => listFeedback(auth, runId), [runId]);
  const feedback = useWorkspaceData(load, `feedback:${runId}`, { enabled: open });
  const mutation = useWorkspaceMutation(`feedback-submit:${runId}`);
  async function submit(event: FormEvent) {
    event.preventDefault();
    const body = { turn_id: turnId, rating, category, comment, corrected_answer: answer.trim() || null };
    const fingerprint = JSON.stringify(body);
    if (token.current?.fingerprint !== fingerprint) token.current = { fingerprint, key: crypto.randomUUID() };
    await mutation.run((auth) => submitFeedback(auth, runId, { ...body, client_key: token.current!.key }), () => {
      setComment(""); setAnswer(""); token.current = null; feedback.reload();
    });
  }
  return <section className="feedback-panel">
    <button type="button" className="button button-ghost" aria-expanded={open} onClick={() => setOpen(!open)}>{t("feedback.title")}</button>
    {open && <>
      <p className="state-hint">{t("feedback.hint")}</p>
      <form onSubmit={submit} className="form-stack">
        <label>{t("feedback.rating")}<select value={rating} onChange={(e) => setRating(Number(e.target.value) as -1 | 1)}><option value={-1}>{t("feedback.negative")}</option><option value={1}>{t("feedback.positive")}</option></select></label>
        <label>{t("feedback.category")}<select value={category} onChange={(e) => setCategory(e.target.value as FeedbackCategory)}>{FEEDBACK_CATEGORIES.map((c) => <option key={c} value={c}>{t(`feedback.categories.${c}`)}</option>)}</select></label>
        <label>{t("feedback.comment")}<textarea maxLength={4000} value={comment} onChange={(e) => setComment(e.target.value)} /></label>
        <label>{t("feedback.correction")}<textarea maxLength={16000} value={answer} onChange={(e) => setAnswer(e.target.value)} /></label>
        <InlineError error={mutation.error} />
        <button className="button button-primary" disabled={mutation.pending}>{t("feedback.submit")}</button>
      </form>
      {feedback.loading && <LoadingState label={t("common.loading")} />}
      {feedback.error && <ErrorState code={feedback.error.code} message={feedback.error.message} onRetry={feedback.reload} />}
      {feedback.loaded && !feedback.data?.length && <EmptyState title={t("feedback.empty")} />}
      {feedback.loaded && !!feedback.data?.length && <p className="state-hint">{t("feedback.historyLimit")}</p>}
      {(feedback.data ?? []).map((row) => <FeedbackReviewCard key={row.id} feedback={row} reload={feedback.reload} canReview={permissions?.includes("evaluation_manage") === true} />)}
    </>}
  </section>;
}

function FeedbackReviewCard({ feedback, reload, canReview }: { feedback: Feedback; reload: () => void; canReview: boolean }) {
  const { t, formatDateTime } = useI18n();
  const [datasetId, setDatasetId] = useState("");
  const [baseId, setBaseId] = useState("");
  const [created, setCreated] = useState<{ id: string; dataset_id: string } | null>(null);
  const mutation = useWorkspaceMutation(`feedback-review:${feedback.id}`);
  const datasets = useWorkspaceData((auth) => listDatasets(auth, { limit: 200 }), `feedback-datasets:${feedback.id}`, { enabled: canReview && feedback.status === "APPROVED" });
  return <article className="state-block">
    <p>{t(`feedback.categories.${feedback.category}`)} · {t(feedback.rating === 1 ? "feedback.positive" : "feedback.negative")} · {t(`feedback.status.${feedback.status}`)}</p>
    <p className="state-hint">{formatDateTime(feedback.created_at)}</p>
    {feedback.comment && <p style={{ whiteSpace: "pre-wrap" }}>{feedback.comment}</p>}
    {feedback.corrected_answer && <><strong>{t("feedback.correction")}</strong><p style={{ whiteSpace: "pre-wrap" }}>{feedback.corrected_answer}</p></>}
    {!canReview && <p className="state-hint">{t("feedback.reviewPermission")}</p>}
    {canReview && feedback.status === "PENDING" && <div className="form-actions">
      <button type="button" className="button button-primary" disabled={mutation.pending} onClick={() => void mutation.run((auth) => reviewFeedback(auth, feedback, "APPROVED"), reload)}>{t("feedback.approve")}</button>
      <button type="button" className="button button-ghost" disabled={mutation.pending} onClick={() => void mutation.run((auth) => reviewFeedback(auth, feedback, "REJECTED"), reload)}>{t("feedback.reject")}</button>
    </div>}
    {feedback.imported_version_id ? <p>{t("feedback.imported")}</p> : canReview && feedback.status === "APPROVED" && <>
      <p className="state-hint">{t("feedback.devHint")}</p>
      <label>{t("feedback.dataset")}<select value={datasetId} onChange={(e) => { setDatasetId(e.target.value); setBaseId(""); }}><option value="">{t("feedback.choose")}</option>{(datasets.data?.items ?? []).map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}</select></label>
      {datasets.data && datasets.data.total > datasets.data.items.length && <p>{t("feedback.datasetLimit")}</p>}
      {datasets.loading && <LoadingState label={t("common.loading")} />}
      <VersionSelector key={datasetId} datasetId={datasetId} baseId={baseId} setBaseId={setBaseId} />
      <InlineError error={datasets.error} />
      <button type="button" className="button button-primary" disabled={mutation.pending || !baseId || !feedback.corrected_answer} onClick={() => void mutation.run((auth) => importFeedback(auth, feedback, datasetId, baseId), (version) => { setCreated(version); reload(); })}>{t("feedback.importToDev")}</button>
    </>}
    {created && <Link href={`/evaluations/datasets/${encodeURIComponent(created.dataset_id)}/versions/${encodeURIComponent(created.id)}`}>{t("feedback.openDraft")}</Link>}
    <InlineError error={mutation.error} />
  </article>;
}

function VersionSelector({ datasetId, baseId, setBaseId }: { datasetId: string; baseId: string; setBaseId: (value: string) => void }) {
  const { t } = useI18n();
  const versions = useWorkspaceData((auth) => listDatasetVersions(auth, datasetId), `feedback-versions:${datasetId}`, { enabled: !!datasetId });
  return <>
    <label>{t("feedback.baseVersion")}<select value={baseId} disabled={!datasetId || versions.loading} onChange={(e) => setBaseId(e.target.value)}><option value="">{t("feedback.choose")}</option>{(versions.data ?? []).map((v) => <option key={v.id} value={v.id}>{v.version_number} · {v.status}</option>)}</select></label>
    {versions.loading && <LoadingState label={t("common.loading")} />}
    <InlineError error={versions.error} />
  </>;
}
