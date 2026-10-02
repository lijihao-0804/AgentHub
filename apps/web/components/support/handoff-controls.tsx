"use client";

import { useEffect, useState } from "react";
import { useFrontendSession } from "@/components/providers/session-provider";
import StatusBadge from "@/components/ui/status-badge";
import HashValue from "@/components/evaluation/hash-value";
import { InlineError } from "@/components/ui/states";
import { useWorkspaceData, useWorkspaceMutation } from "@/hooks/use-workspace-data";
import { changeHandoff, getArtifactHandoff, listHandoffAssignees, openHandoff } from "@/lib/api/handoffs";
import { useI18n } from "@/i18n/provider";

export default function HandoffControls({ artifactId }: { artifactId: string }) {
  const { workspaceId, sessionId } = useFrontendSession();
  return <HandoffControlsInner key={`${sessionId}:${workspaceId}:${artifactId}`} artifactId={artifactId} />;
}

function HandoffControlsInner({ artifactId }: { artifactId: string }) {
  const { t } = useI18n();
  const { workspaceId, userId, permissions, sessionId } = useFrontendSession();
  const canHandle = permissions?.includes("handoff_handle") ?? false;
  const canAssign = permissions?.includes("workspace_administration") ?? false;
  const query = useWorkspaceData(auth => getArtifactHandoff(auth, artifactId), `handoff:${workspaceId}:${artifactId}`);
  const mutation = useWorkspaceMutation(`handoff:${workspaceId}:${artifactId}`);
  const row = query.data;
  const assignees = useWorkspaceData(listHandoffAssignees, `handoff-assignees:${workspaceId}`, { enabled: canAssign && !!row && row.status !== "CLOSED" });
  const [assignee, setAssignee] = useState("");
  const [reason, setReason] = useState("");
  const [unresolved, setUnresolved] = useState("");
  useEffect(() => { setAssignee(""); setReason(""); setUnresolved(""); }, [artifactId, workspaceId, sessionId]);
  const labels = { OPEN: t("handoffLifecycle.open"), ASSIGNED: t("handoffLifecycle.assigned"), IN_PROGRESS: t("handoffLifecycle.inProgress"), CLOSED: t("handoffLifecycle.closed") };

  async function act(action: "assign" | "claim" | "close") {
    if (!row || mutation.pending) return;
    const result = await mutation.run(auth => changeHandoff(auth, row.id, action, { expected_version: row.version,
      ...(action === "assign" ? { assignee_id: assignee } : {}),
      ...(action === "close" ? { reason: reason.trim(), unresolved_items: unresolved.split("\n").map(v => v.trim()).filter(Boolean) } : {}),
    }));
    if (result?.status === "CLOSED") { setReason(""); setUnresolved(""); }
    query.reload();
  }

  return <section className="eval-form" aria-label={t("handoffLifecycle.title")}>
    <h4>{t("handoffLifecycle.title")}</h4>
    <p className="state-hint">{t("handoffLifecycle.hint")}</p>
    <InlineError error={query.error} fallback={t("errors.requestFailed")} />
    <InlineError error={mutation.error} fallback={t("errors.requestFailed")} />
    {mutation.error?.status === 409 && <p role="status">{t("handoffLifecycle.conflict")}</p>}
    <button type="button" className="button button-ghost" disabled={query.loading || mutation.pending} onClick={query.reload}>{t("common.refresh")}</button>
    {!query.loaded && <p className="state-hint">{query.loading ? t("common.loading") : t("handoffLifecycle.unavailable")}</p>}
    {query.loaded && !row && <button type="button" className="button button-primary" disabled={!canHandle || mutation.pending} onClick={async () => { await mutation.run(auth => openHandoff(auth, artifactId)); query.reload(); }}>{t("handoffLifecycle.start")}</button>}
    {!canHandle && <p className="state-hint">{t("handoffLifecycle.permission")}</p>}
    {row && <>
      <StatusBadge status={row.status} label={labels[row.status]} />
      <p>{t("handoffLifecycle.assignee")}: {row.assignee_id ? <HashValue value={row.assignee_id} /> : t("common.none")}</p>
      {canAssign && row.status !== "CLOSED" && <div className="form-grid">
        <label>{t("handoffLifecycle.assignee")}<select value={assignee} onChange={e => setAssignee(e.target.value)} disabled={mutation.pending || assignees.loading}>
          <option value="">{t("handoffLifecycle.choose")}</option>
          {(assignees.data ?? []).map(v => <option key={v.user_id} value={v.user_id}>{v.email}</option>)}
        </select></label>
        <InlineError error={assignees.error} fallback={t("errors.requestFailed")} />
        <button type="button" className="button button-ghost" disabled={!assignee || mutation.pending || !assignees.loaded} onClick={() => act("assign")}>{t("handoffLifecycle.assign")}</button>
      </div>}
      {(row.status === "OPEN" || (row.status === "ASSIGNED" && row.assignee_id === userId)) && <button type="button" className="button button-primary" disabled={!canHandle || mutation.pending} onClick={() => act("claim")}>{t("handoffLifecycle.claim")}</button>}
      {row.status === "IN_PROGRESS" && row.claimed_by === userId && <>
        <label>{t("handoffLifecycle.reason")}<textarea value={reason} onChange={e => setReason(e.target.value)} maxLength={4000} disabled={!canHandle || mutation.pending} /></label>
        <label>{t("handoffLifecycle.unresolved")}<textarea value={unresolved} onChange={e => setUnresolved(e.target.value)} maxLength={50000} disabled={!canHandle || mutation.pending} /></label>
        <button type="button" className="button button-primary" disabled={!canHandle || !reason.trim() || mutation.pending} onClick={() => act("close")}>{t("handoffLifecycle.close")}</button>
      </>}
      {row.status === "CLOSED" && <div><p>{t("handoffLifecycle.reason")}: {row.closure_reason}</p><p>{t("handoffLifecycle.unresolved")}</p>{row.unresolved_items.length ? <ul>{row.unresolved_items.map((v, i) => <li key={i}>{v}</li>)}</ul> : <p>{t("common.none")}</p>}</div>}
    </>}
  </section>;
}
