"use client";

import Link from "next/link";
import { useEffect, useRef, useState, type FormEvent } from "react";

import StatusBadge from "@/components/ui/status-badge";
import { EmptyState, ErrorState, InlineError, LoadingState } from "@/components/ui/states";
import { useFrontendSession } from "@/components/providers/session-provider";
import { useWorkspaceMutation, type WorkspaceQuery } from "@/hooks/use-workspace-data";
import { errorHintKey } from "@/lib/api/client";
import {
  cancelAgentRun,
  payloadString,
  streamThreadTurn,
} from "@/lib/api/agent-runtime";
import type { ThreadTurn } from "@/lib/api/threads";
import { useI18n } from "@/i18n/provider";

const PENDING_STATUSES = new Set(["RUNNING", "WAITING_APPROVAL", "CANCEL_REQUESTED"]);
const TURN_POLL_MS = 2500;

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
  const { workspaceId, accessToken, permissions } = useFrontendSession();
  const cannotRun = permissions !== null && !permissions.includes("agent_run");
  const mutation = useWorkspaceMutation(`research-turns:${threadId}`);
  const tokenRef = useRef<string | null>(null);
  const [draft, setDraft] = useState("");
  const [pendingInput, setPendingInput] = useState<string | null>(null);
  /** Live answer text from message.delta events, shown inside the pending bubble. */
  const [streamText, setStreamText] = useState<string | null>(null);
  const [streamRunId, setStreamRunId] = useState<string | null>(null);
  const streamAbortRef = useRef<AbortController | null>(null);
  const [stoppingRunId, setStoppingRunId] = useState<string | null>(null);

  // Another thread is another conversation: nothing in flight carries over.
  useEffect(() => {
    tokenRef.current = null;
    setDraft("");
    setPendingInput(null);
    setStreamText(null);
    setStreamRunId(null);
    setStoppingRunId(null);
    streamAbortRef.current = null;
    return () => {
      // Leave the backend run untouched, but stop this pane's stream so a
      // response from the previous thread cannot update the next conversation.
      streamAbortRef.current?.abort();
      streamAbortRef.current = null;
    };
  }, [threadId]);

  const turnList = [...(turns.data ?? [])].sort((left, right) => left.sequence - right.sequence);
  const hasActiveRun = turnList.some(
    (turn) => PENDING_STATUSES.has(turn.status ?? "") && turn.agent_run_id,
  );

  // A reload loses the original SSE request. Poll durable active states so a
  // waiting approval or cancellation also resolves without reopening the page.
  useEffect(() => {
    if (!hasActiveRun) return;
    const interval = window.setInterval(() => {
      if (!document.hidden) void turns.reload();
    }, TURN_POLL_MS);
    return () => window.clearInterval(interval);
  }, [hasActiveRun, turns.reload]);

  // The optimistic copy stays up until the reloaded list actually contains
  // the submitted turn (a just-submitted turn is always the list's last
  // entry). Clearing before the reload lands makes the message blink out
  // and back in for one reload round-trip.
  const lastTurnInput = turnList.length > 0 ? turnList[turnList.length - 1].user_input : null;
  const pendingVisible = pendingInput !== null && lastTurnInput !== pendingInput;
  useEffect(() => {
    if (pendingInput !== null && !mutation.pending && !pendingVisible) {
      setPendingInput(null);
    }
  }, [pendingInput, mutation.pending, pendingVisible]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const text = draft.trim();
    if (!text || mutation.pending || cannotRun) return;
    if (!tokenRef.current) tokenRef.current = newSubmissionToken();
    setPendingInput(text);
    setStreamText("");
    setStreamRunId(null);
    const controller = new AbortController();
    streamAbortRef.current = controller;
    const result = await mutation.run((auth) =>
      streamThreadTurn(
        { workspaceId: auth.workspaceId, accessToken: auth.accessToken },
        {
          threadId,
          inputText: text,
          clientToken: tokenRef.current ?? undefined,
          signal: controller.signal,
          onStarted: (runId) => setStreamRunId(runId),
          onEvent: (event) => {
            if (event.type === "message.delta") {
              const delta = payloadString(event.payload, "delta");
              if (delta !== null) setStreamText((current) => (current ?? "") + delta);
            } else if (event.type === "run.started") {
              setStreamRunId(event.run_id);
            }
          },
        },
      ),
    );
    if (streamAbortRef.current !== controller) return;
    streamAbortRef.current = null;
    if (result) {
      // The stream closed (completed, paused for approval or cancelled): the
      // reloaded turn list now carries the authoritative answer.
      tokenRef.current = null;
      setDraft("");
      setStreamText(null);
      setStreamRunId(null);
      turns.reload();
      onTurnCompleted();
    } else if (controller.signal.aborted) {
      // A deliberate stop is not an error to report.
      mutation.clearError();
      setStreamText(null);
      setStreamRunId(null);
      turns.reload();
    }
    // A failed submission keeps the pending bubble (and its token) up so the
    // same message can be retried.
  }

  async function stopGeneration(runId: string) {
    setStoppingRunId(runId);
    try {
      await cancelAgentRun({ workspaceId, accessToken }, runId);
    } catch {
      // The abort below already ends the local stream; the run's own state
      // stays authoritative and is picked up by the reload.
    } finally {
      if (streamRunId === runId) streamAbortRef.current?.abort();
      setStoppingRunId(null);
      void turns.reload();
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
            const contentRestricted = cannotRun || turn.user_input === null;
            return (
              <li className="conversation-turn" key={turn.id}>
                <div className="conversation-message conversation-message-user">
                  <p className="conversation-role">{t("research.conversation.user")}</p>
                  <p className="conversation-text">
                    {contentRestricted ? t("research.conversation.contentRestricted") : turn.user_input}
                  </p>
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
                      {contentRestricted
                        ? t("research.conversation.contentRestricted")
                        : turn.final_output ?? t("research.conversation.noOutput")}
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
                      {turn.status === "RUNNING" && (
                        <button
                          type="button"
                          className="button button-ghost"
                          onClick={() => void stopGeneration(turn.agent_run_id!)}
                          disabled={cannotRun || stoppingRunId === turn.agent_run_id}
                        >
                          {stoppingRunId === turn.agent_run_id
                            ? t("research.conversation.stopping")
                            : t("research.conversation.stop")}
                        </button>
                      )}
                    </p>
                  ) : (
                    <p className="conversation-meta">{t("research.conversation.noRun")}</p>
                  )}
                </div>
              </li>
            );
          })}

          {pendingVisible && (
            <li className="conversation-turn" key="pending">
              <div className="conversation-message conversation-message-user">
                <p className="conversation-role">{t("research.conversation.user")}</p>
                <p className="conversation-text">{pendingInput}</p>
              </div>
              <div className="conversation-message conversation-message-agent">
                <p className="conversation-role">{t("research.conversation.agent")}</p>
                {streamText ? (
                  <p className="conversation-text">{streamText}</p>
                ) : (
                  <p className="conversation-text conversation-pending">{t("research.conversation.pending")}</p>
                )}
                <p className="conversation-meta">{t("research.conversation.pendingHint")}</p>
                {streamRunId && (
                  <button
                    type="button"
                    className="button button-ghost"
                    onClick={() => void stopGeneration(streamRunId)}
                    disabled={cannotRun || stoppingRunId === streamRunId}
                  >
                    {stoppingRunId === streamRunId
                      ? t("research.conversation.stopping")
                      : t("research.conversation.stop")}
                  </button>
                )}
              </div>
            </li>
          )}
        </ol>
      </div>

      <form className="conversation-composer" onSubmit={submit} noValidate>
        {(cannotRun || turnList.some((turn) => turn.user_input === null)) && (
          <p className="state-hint" role="note">{t("research.conversation.readOnlyHint")}</p>
        )}
        <label>
          {t("research.conversation.composerLabel")}
          <textarea
            rows={3}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder={t("research.conversation.composerPlaceholder")}
            maxLength={32000}
            disabled={mutation.pending || cannotRun}
          />
        </label>
        <InlineError error={mutation.error} fallback={t("errors.requestFailed")} />
        {mutation.error && <p className="state-hint">{t("research.conversation.retryHint")}</p>}
        <div className="form-actions">
          <button type="submit" className="button button-primary" disabled={mutation.pending || cannotRun || !draft.trim()}>
            {mutation.pending ? t("research.conversation.sending") : t("research.conversation.send")}
          </button>
        </div>
      </form>
    </section>
  );
}
