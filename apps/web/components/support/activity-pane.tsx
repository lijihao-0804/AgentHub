"use client";

import Link from "next/link";

import StatusBadge from "@/components/ui/status-badge";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/states";
import type { WorkspaceQuery } from "@/hooks/use-workspace-data";
import { errorHintKey } from "@/lib/api/client";
import type { ThreadTurn } from "@/lib/api/threads";
import { useI18n } from "@/i18n/provider";

const PENDING_STATUSES = new Set(["RUNNING", "WAITING_APPROVAL", "CANCEL_REQUESTED"]);

/**
 * What the agent was asked and where that went, in one column.
 *
 * The conversation pane already shows the answers. This is the audit view of
 * the same turns: the ask, how it ended, and the run that holds the lookups
 * and the writes behind it — which is the only place the actual tool calls
 * can be checked.
 */
export default function ActivityPane({ turns }: { turns: WorkspaceQuery<ThreadTurn[]> }) {
  const { t, formatDateTime } = useI18n();

  const completed = [...(turns.data ?? [])]
    .filter((turn) => turn.status !== null && !PENDING_STATUSES.has(turn.status))
    .sort((left, right) => left.sequence - right.sequence);

  return (
    <section className="research-pane" aria-label={t("support.activity.paneTitle")}>
      <div className="research-pane-head">
        <h2>{t("support.activity.paneTitle")}</h2>
        <button type="button" className="button button-ghost" onClick={turns.reload} disabled={turns.loading}>
          {t("common.refresh")}
        </button>
      </div>

      <div className="research-pane-body">
        {turns.error && (
          <ErrorState
            code={turns.error.code}
            message={turns.error.message || t("errors.loadTurns")}
            hint={errorHintKey(turns.error) ? t(errorHintKey(turns.error)!) : undefined}
            onRetry={turns.reload}
          />
        )}
        {turns.loading && completed.length === 0 && !turns.error && <LoadingState />}
        {turns.loaded && !turns.error && completed.length === 0 && (
          <EmptyState title={t("support.activity.empty")} hint={t("support.activity.emptyHint")} />
        )}

        <ul className="string-list-items">
          {completed.map((turn) => (
            <li className="support-activity-item" key={turn.id}>
              <span className="conversation-text">{turn.user_input}</span>
              <span className="artifact-card-meta">
                {turn.status && <StatusBadge status={turn.status} />}
                <span>{formatDateTime(turn.created_at)}</span>
                {turn.agent_run_id ? (
                  <Link href={`/runs/${encodeURIComponent(turn.agent_run_id)}`} title={turn.agent_run_id}>
                    {t("appThread.conversation.viewRun")}
                  </Link>
                ) : (
                  <span>{t("appThread.conversation.noRun")}</span>
                )}
              </span>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}
