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

type ToolEvent = Tools["events"][number];

function eventKind(event: ToolEvent): "requested" | "started" | "completed" | "failed" {
  const kind = event.type.split(".")[1];
  return kind === "completed" || kind === "failed" || kind === "started" ? kind : "requested";
}

function eventTime(event: ToolEvent): string | null {
  const value = event.payload.occurred_at ?? event.payload.recorded_at ?? event.payload.at;
  return typeof value === "string" ? value : null;
}

/**
 * One recorded tool event, collapsed to a single summary row: tool name,
 * lifecycle stage, duration, error code. Detail (argument shape, raw
 * status) lives in an expandable <details> — it is debug information,
 * not something the reader needs to see for every call.
 */
function ToolEventRow({ event }: { event: ToolEvent }) {
  const { t, formatDateTime, formatDurationMs } = useI18n();
  const kind = eventKind(event);
  const failed = kind === "failed" || typeof event.payload.error_code === "string";
  const tool = String(event.payload.tool_identity ?? "");
  const time = eventTime(event);
  const summary = typeof event.payload.arguments_summary === "string" ? event.payload.arguments_summary : null;
  const detailParts = [
    summary ? `${t("runEvidence.arguments")}: ${summary}` : null,
    typeof event.payload.status === "string" ? `${t("runEvidence.status")}: ${event.payload.status}` : null,
  ].filter((part): part is string => Boolean(part));

  return (
    <details className={`tool-event-row${failed ? " tool-event-row-failed" : ""}`}>
      <summary>
        <span className={`tool-event-dot tool-event-dot-${kind}`} aria-hidden="true" />
        <span className="tool-event-name" title={tool}>{tool || event.type}</span>
        <span className="tool-event-kind">{t(`runEvidence.events.${kind}`)}</span>
        {typeof event.payload.duration_ms === "number" && (
          <span className="tool-event-meta">{formatDurationMs(event.payload.duration_ms)}</span>
        )}
        {typeof event.payload.error_code === "string" && <code className="tool-event-error">{event.payload.error_code}</code>}
        {time && <span className="tool-event-meta tool-event-time">{formatDateTime(time)}</span>}
      </summary>
      {detailParts.length > 0 && (
        <div className="tool-event-detail">
          {detailParts.map((part) => (
            <p key={part}>{part}</p>
          ))}
        </div>
      )}
    </details>
  );
}

export default function ToolEvidencePanel({ runId }: { runId: string }) {
  const { sessionId } = useFrontendSession();
  return <Body key={`${sessionId}:${runId}`} runId={runId} />;
}
function Body({ runId }: { runId: string }) {
  const { t } = useI18n();
  const load = useCallback((auth: AuthInput) => apiRequest<Tools>(`${base(auth, runId)}/tools`, auth.accessToken), [runId]);
  const tools = useWorkspaceData(load, `tools:${runId}`);
  const [selected, setSelected] = useState<Ref | null>(null);
  const events = tools.data?.events ?? [];
  return <Panel title={t("runEvidence.title")}>
    {tools.loading && <LoadingState label={t("common.loading")} />}
    {tools.error && <ErrorState code={tools.error.code} message={tools.error.message} onRetry={tools.reload} />}
    {tools.loaded && !events.length && <EmptyState title={t("runEvidence.empty")} />}
    {events.length > 0 && (
      <div className="tool-event-list">
        {events.map((event) => <ToolEventRow key={event.sequence} event={event} />)}
      </div>
    )}
    {tools.data?.truncated && <p className="state-hint">{t("runEvidence.truncated")}</p>}
    {tools.data?.evidence_refs.length ? (
      <>
        <h3>{t("runEvidence.evidence")}</h3>
        <div className="tool-event-list">
          {tools.data.evidence_refs.map((ref) => (
            <div key={`${ref.snapshot_id}:${ref.chunk_id}`} className="tool-event-row tool-evidence-row">
              <code title={ref.snapshot_id}>{ref.chunk_id.slice(0, 12)} · {ref.snapshot_id.slice(0, 8)}</code>
              <button
                type="button"
                className="button button-ghost"
                disabled={!tools.data?.content_allowed}
                title={tools.data?.content_allowed ? undefined : t("runEvidence.permission")}
                onClick={() => setSelected(ref)}
              >
                {t("runEvidence.open")}
              </button>
            </div>
          ))}
        </div>
      </>
    ) : null}
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
