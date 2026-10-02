"use client";
import { useCallback, useState } from "react";
import { useFrontendSession } from "@/components/providers/session-provider";
import { useWorkspaceData } from "@/hooks/use-workspace-data";
import { useI18n } from "@/i18n/provider";
import { EmptyState, ErrorState, LoadingState, Panel } from "@/components/ui/states";
import { apiRequest, type AuthInput } from "@/lib/api/client";

type Ref = { snapshot_id: string; knowledge_base_id: string; document_revision_id: string; chunk_id: string };
type Tools = { events: { sequence: number; type: string; payload: Record<string, unknown> }[]; evidence_refs: Ref[]; content_allowed: boolean; truncated: boolean };
function base(auth: AuthInput, runId: string) { return `/api/v1/workspaces/${encodeURIComponent(auth.workspaceId)}/runs/${encodeURIComponent(runId)}`; }
export default function ToolEvidencePanel({ runId }: { runId: string }) {
  const { sessionId } = useFrontendSession();
  return <Body key={`${sessionId}:${runId}`} runId={runId} />;
}
function Body({ runId }: { runId: string }) {
  const { t, formatDurationMs } = useI18n();
  const load = useCallback((auth: AuthInput) => apiRequest<Tools>(`${base(auth, runId)}/tools`, auth.accessToken), [runId]);
  const tools = useWorkspaceData(load, `tools:${runId}`);
  const [selected, setSelected] = useState<Ref | null>(null);
  return <Panel title={t("runEvidence.title")}>
    <p className="state-hint">{t("runEvidence.replayHint")}</p>
    {tools.loading && <LoadingState label={t("common.loading")} />}
    {tools.error && <ErrorState code={tools.error.code} message={tools.error.message} onRetry={tools.reload} />}
    {tools.loaded && !tools.data?.events.length && <EmptyState title={t("runEvidence.empty")} />}
    {(tools.data?.events ?? []).map((event) => <article key={event.sequence} className="state-block">
      <strong>{String(event.payload.tool_identity ?? "")}</strong> · {t(`runEvidence.events.${event.type.split(".")[1] as "requested" | "started" | "completed" | "failed"}`)}
      {typeof event.payload.status === "string" && <span> · {event.payload.status}</span>}
      {typeof event.payload.duration_ms === "number" && <p>{formatDurationMs(event.payload.duration_ms)}</p>}
      {typeof event.payload.error_code === "string" && <p><code>{event.payload.error_code}</code></p>}
      {event.type === "tool.requested" && <p><strong>{t("runEvidence.arguments")}</strong>: {typeof event.payload.arguments_summary === "string" ? event.payload.arguments_summary : t("runEvidence.summaryUnavailable")}</p>}
    </article>)}
    {tools.data?.truncated && <p>{t("runEvidence.truncated")}</p>}
    <h3>{t("runEvidence.evidence")}</h3>
    {!tools.data?.evidence_refs.length && <p>{t("runEvidence.noEvidence")}</p>}
    {tools.data?.content_allowed === false && <p>{t("runEvidence.permission")}</p>}
    {(tools.data?.evidence_refs ?? []).map((ref) => <div key={`${ref.snapshot_id}:${ref.chunk_id}`}>
      <code title={ref.snapshot_id}>{ref.chunk_id.slice(0, 12)} · {ref.snapshot_id.slice(0, 8)}</code>
      <button type="button" className="button button-ghost" disabled={!tools.data?.content_allowed} onClick={() => setSelected(ref)}>{t("runEvidence.open")}</button>
    </div>)}
    {selected && <EvidenceExcerpt key={`${selected.snapshot_id}:${selected.chunk_id}`} runId={runId} selected={selected} />}
  </Panel>;
}

function EvidenceExcerpt({ runId, selected }: { runId: string; selected: Ref }) {
  const { t } = useI18n();
  const evidence = useWorkspaceData((auth) => apiRequest<{ text: string; truncated: boolean }>(`${base(auth, runId)}/evidence/${encodeURIComponent(selected.snapshot_id)}/${encodeURIComponent(selected.chunk_id)}`, auth.accessToken), `evidence:${runId}:${selected.snapshot_id}:${selected.chunk_id}`);
  return <>
    {evidence.loading && <LoadingState label={t("common.loading")} />}
    {evidence.error && <ErrorState code={evidence.error.code} message={evidence.error.message} onRetry={evidence.reload} />}
    {evidence.data && <><pre style={{ whiteSpace: "pre-wrap" }}>{evidence.data.text}</pre>{evidence.data.truncated && <p>{t("runEvidence.excerpt")}</p>}</>}
  </>;
}
