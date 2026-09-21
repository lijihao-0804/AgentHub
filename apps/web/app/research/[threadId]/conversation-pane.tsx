"use client";

import Link from "next/link";
import { useEffect, useRef, useState, type FormEvent } from "react";

import StatusBadge from "@/components/ui/status-badge";
import { EmptyState, ErrorState, InlineError, LoadingState } from "@/components/ui/states";
import { useWorkspaceMutation, type WorkspaceQuery } from "@/hooks/use-workspace-data";
import { errorHintKey } from "@/lib/api/client";
import { submitThreadTurn, type ThreadTurn } from "@/lib/api/threads";
import { useI18n } from "@/i18n/provider";

const PENDING_STATUSES = new Set(["RUNNING", "WAITING_APPROVAL", "CANCEL_REQUESTED"]);

function shortId(id: string): string {
  return id.slice(0, 8);
}

/**
 * One submission identity, reused by every retry of the same message.
 *
 * The backend treats a repeated `client_token` as the same turn, so a retry
 * after a timeout returns the run that already exists instead of starting a
 * second one for the same question.
 */
function newSubmissionToken(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `${Date.now().toString(16)}-${Math.random().toString(16).slice(2)}`;
}

export default function ConversationPane({
  threadId,
  turns,
  onTurnCompleted,
}: {
  threadId: string;
  turns: WorkspaceQuery<ThreadTurn[]>;
  onTurnCompleted: () => void;
}) {
  const { t, formatDateTime } = useI18n();
  const mutation = useWorkspaceMutation(`research-turns:${threadId}`);
  const tokenRef = useRef<string | null>(null);
  const [draft, setDraft] = useState("");
  const [pendingInput, setPendingInput] = useState<string | null>(null);

  // Another thread is another conversation: nothing in flight carries over.
  useEffect(() => {
    tokenRef.current = null;
    setDraft("");
    setPendingInput(null);
  }, [threadId]);

  const turnList = [...(turns.data ?? [])].sort((left, right) => left.sequence - right.sequence);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const text = draft.trim();
    if (!text || mutation.pending) return;
    if (!tokenRef.current) tokenRef.current = newSubmissionToken();
    setPendingInput(text);
    const result = await mutation.run((auth) =>
      submitThreadTurn(auth, threadId, { input_text: text, client_token: tokenRef.current ?? undefined }),
    );
    setPendingInput(null);
    if (result) {
      // A fresh question deserves a fresh identity; a failed one keeps its own.
      tokenRef.current = null;
      setDraft("");
      turns.reload();
      onTurnCompleted();
    }
  }

  return (
    <section className="research-pane research-pane-conversation" aria-label={t("research.conversation.paneTitle")}>
      <div className="research-pane-head">
        <h2>{t("research.conversation.paneTitle")}</h2>
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
        {turns.loading && turnList.length === 0 && !turns.error && <LoadingState />}
        {turns.loaded && !turns.error && turnList.length === 0 && pendingInput === null && (
          <EmptyState title={t("research.conversation.empty")} hint={t("research.conversation.emptyHint")} />
        )}

        <ol className="conversation-list">
          {turnList.map((turn) => {
            const pending = turn.status === null || PENDING_STATUSES.has(turn.status);
            return (
              <li className="conversation-turn" key={turn.id}>
                <div className="conversation-message conversation-message-user">
                  <p className="conversation-role">{t("research.conversation.user")}</p>
                  <p className="conversation-text">{turn.user_input}</p>
                  <p className="conversation-meta">{formatDateTime(turn.created_at)}</p>
                </div>
                <div className="conversation-message conversation-message-agent">
                  <div className="conversation-role-line">
                    <p className="conversation-role">{t("research.conversation.agent")}</p>
                    {turn.status && <StatusBadge status={turn.status} />}
                  </div>
                  {pending ? (
                    <p className="conversation-text conversation-pending">{t("research.conversation.pending")}</p>
                  ) : (
                    <p className="conversation-text">
                      {turn.final_output ?? t("research.conversation.noOutput")}
                    </p>
                  )}
                  {turn.failure_code && (
                    <p className="conversation-meta">
                      {t("research.conversation.failure")}: <code>{turn.failure_code}</code>
                    </p>
                  )}
                  {/*
                    The chat view never replaces the engineering view: every
                    answer keeps a way into the run that produced it.
                  */}
                  {turn.agent_run_id ? (
                    <p className="conversation-run">
                      <Link
                        className="button button-ghost"
                        href={`/runs/${encodeURIComponent(turn.agent_run_id)}`}
                        title={turn.agent_run_id}
                      >
                        {t("research.conversation.runLabel", { id: shortId(turn.agent_run_id) })}
                      </Link>
                      <Link href={`/runs/${encodeURIComponent(turn.agent_run_id)}`}>
                        {t("research.conversation.viewRun")}
                      </Link>
                    </p>
                  ) : (
                    <p className="conversation-meta">{t("research.conversation.noRun")}</p>
                  )}
                </div>
              </li>
            );
          })}

          {pendingInput !== null && (
            <li className="conversation-turn" key="pending">
              <div className="conversation-message conversation-message-user">
                <p className="conversation-role">{t("research.conversation.user")}</p>
                <p className="conversation-text">{pendingInput}</p>
              </div>
              <div className="conversation-message conversation-message-agent">
                <p className="conversation-role">{t("research.conversation.agent")}</p>
                <p className="conversation-text conversation-pending">{t("research.conversation.pending")}</p>
                <p className="conversation-meta">{t("research.conversation.pendingHint")}</p>
              </div>
            </li>
          )}
        </ol>
      </div>

      <form className="conversation-composer" onSubmit={submit} noValidate>
        <label>
          {t("research.conversation.composerLabel")}
          <textarea
            rows={3}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder={t("research.conversation.composerPlaceholder")}
            maxLength={32000}
            disabled={mutation.pending}
          />
        </label>
        <InlineError error={mutation.error} fallback={t("errors.requestFailed")} />
        {mutation.error && <p className="state-hint">{t("research.conversation.retryHint")}</p>}
        <div className="form-actions">
          <button type="submit" className="button button-primary" disabled={mutation.pending || !draft.trim()}>
            {mutation.pending ? t("research.conversation.sending") : t("research.conversation.send")}
          </button>
        </div>
      </form>
    </section>
  );
}
