"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState, type ReactNode } from "react";

import type { AppThreadCopy } from "@/components/apps/app-thread-copy";
import AppConversationPane from "@/components/apps/app-conversation-pane";
import { InlineConfirm } from "@/components/evaluation/hash-value";
import { ErrorState, InlineError, LoadingState, SessionRequired } from "@/components/ui/states";
import { useFrontendSession } from "@/components/providers/session-provider";
import { useWorkspaceData, useWorkspaceMutation, type WorkspaceQuery } from "@/hooks/use-workspace-data";
import { ApiError, errorHintKey, type AuthInput } from "@/lib/api/client";
import { listThreadArtifacts, type Artifact } from "@/lib/api/artifacts";
import {
  deleteThread,
  getThread,
  listThreads,
  listThreadTurns,
  patchThread,
  type Thread,
  type ThreadTurn,
} from "@/lib/api/threads";
import { useI18n } from "@/i18n/provider";

/** What an application's own panes get from the shell. */
export type AppThreadContext = {
  threadId: string;
  thread: Thread | null;
  artifacts: WorkspaceQuery<Artifact[]>;
  turns: WorkspaceQuery<ThreadTurn[]>;
};

/**
 * Threads, the conversation, and whatever the application draws beside it.
 *
 * The first two panes are identical for every application because the runtime
 * underneath them is identical. What differs is the third column, which is
 * `children` — an incident draws a timeline there, an analysis draws its
 * queries, a case draws the customer. Nothing here branches on which
 * application it is serving.
 */
export default function AppThreadShell({
  copy,
  threadId,
  children,
}: {
  copy: AppThreadCopy;
  threadId: string;
  children: (context: AppThreadContext) => ReactNode;
}) {
  const { t, formatDateTime } = useI18n();
  const { connected, sessionId, workspaceId, permissions } = useFrontendSession();
  // Deleting a thread runs through the run-scoped permission; a viewer sees
  // the control disabled instead of discovering the 403 by submit.
  const cannotDelete = permissions !== null && !permissions.includes("agent_run");
  const router = useRouter();

  const scope = `${copy.basePath}:${workspaceId}:${threadId}`;

  const loadThread = useCallback(async (auth: AuthInput) => {
    const result = await getThread(auth, threadId);
    if (copy.agentId && (result.agent_id !== copy.agentId || result.kind !== copy.kind)) {
      throw new ApiError("THREAD_NOT_FOUND", "The thread was not found for this agent.", 404);
    }
    return result;
  }, [threadId, copy.agentId, copy.kind]);
  const loadThreadList = useCallback(
    (auth: AuthInput) => listThreads(auth, { kind: copy.kind, agentId: copy.agentId, limit: 50 }),
    [copy.kind, copy.agentId],
  );
  const loadTurns = useCallback((auth: AuthInput) => listThreadTurns(auth, threadId), [threadId]);
  const loadArtifacts = useCallback(
    (auth: AuthInput) => listThreadArtifacts(auth, threadId, { limit: 200 }),
    [threadId],
  );

  const thread = useWorkspaceData<Thread>(loadThread, `app-thread:${scope}`);
  const threads = useWorkspaceData<Thread[]>(
    loadThreadList,
    `app-threads:${copy.basePath}:${workspaceId}`,
  );
  const turns = useWorkspaceData<ThreadTurn[]>(loadTurns, `app-turns:${scope}`);
  const artifacts = useWorkspaceData<Artifact[]>(loadArtifacts, `app-artifacts:${scope}`);

  const threadMutation = useWorkspaceMutation(`app-thread:${scope}`);

  const [renaming, setRenaming] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);
  // The application-specific side rail (papers, tickets, timelines) is
  // toggleable: reading a long answer full-width beats keeping three columns
  // on screen at all times.
  const [sideOpen, setSideOpen] = useState(true);

  useEffect(() => {
    setRenaming(false);
    setTitleDraft("");
    setConfirmDelete(false);
  }, [sessionId, threadId]);

  const onTurnCompleted = useCallback(() => {
    artifacts.reload();
    thread.reload();
    threads.reload();
  }, [artifacts, thread, threads]);

  if (!connected) {
    return (
      <div className="page">
        <header className="page-header">
          <h1>{t(copy.title)}</h1>
        </header>
        <SessionRequired contextKey={copy.sessionContext} />
      </div>
    );
  }

  const current = thread.data ?? null;
  const threadList = threads.data ?? [];

  async function rename() {
    const title = titleDraft.trim();
    if (!title) return;
    const result = await threadMutation.run((auth) => patchThread(auth, threadId, { title }));
    if (result) {
      setRenaming(false);
      thread.reload();
      threads.reload();
    }
  }

  async function removeThread() {
    const result = await threadMutation.run(async (auth) => {
      await deleteThread(auth, threadId);
      return true;
    });
    if (result) {
      setConfirmDelete(false);
      router.push(copy.basePath);
    }
  }

  return (
    <div className="thread-page">
      <header className="thread-topbar">
        <Link className="thread-back" href={copy.basePath}>
          <span aria-hidden="true">←</span> {t(copy.title)}
        </Link>
        <h1 className="thread-title">{current?.title ?? t(copy.title)}</h1>
        <div className="thread-topbar-actions">
          <button
            type="button"
            className="button button-ghost"
            aria-pressed={sideOpen}
            onClick={() => setSideOpen((open) => !open)}
          >
            {t("appThread.railToggle")}
          </button>
          {current && (
            <button type="button" className="button button-ghost" onClick={turns.reload} disabled={turns.loading}>
              {t("common.refresh")}
            </button>
          )}
        </div>
      </header>

      {thread.error && (
        <ErrorState
          code={thread.error.code}
          message={thread.error.message || t("errors.loadThread")}
          hint={errorHintKey(thread.error) ? t(errorHintKey(thread.error)!) : undefined}
          onRetry={thread.reload}
        />
      )}

      <div className="thread-frame" data-side={current && sideOpen ? "open" : "closed"}>
        <nav className="thread-rail" aria-label={t("appThread.threads.paneTitle")}>
          <Link className="button button-ghost thread-new" href={copy.basePath}>
            {t(copy.newThread)}
          </Link>
          {threads.error && (
            <ErrorState
              code={threads.error.code}
              message={threads.error.message || t("errors.loadThreads")}
              hint={errorHintKey(threads.error) ? t(errorHintKey(threads.error)!) : undefined}
              onRetry={threads.reload}
            />
          )}
          {threads.loading && threadList.length === 0 && !threads.error && <LoadingState />}

          <ul className="thread-list">
            {threadList.map((item) => {
              const active = item.id === threadId;
              return (
                <li key={item.id}>
                  <Link
                    className={`thread-list-item${active ? " thread-list-item-active" : ""}`}
                    href={`${copy.basePath}/${encodeURIComponent(item.id)}`}
                    aria-current={active ? "page" : undefined}
                  >
                    <span className="thread-list-title">{item.title}</span>
                    <span className="thread-list-meta">{formatDateTime(item.updated_at)}</span>
                  </Link>
                </li>
              );
            })}
          </ul>

          <div className="thread-admin">
            {renaming ? (
              <div className="thread-rename">
                <label>
                  {t("appThread.threads.renameLabel")}
                  <input
                    value={titleDraft}
                    onChange={(event) => setTitleDraft(event.target.value)}
                    maxLength={200}
                  />
                </label>
                <div className="thread-admin-actions">
                  <button
                    type="button"
                    className="button button-primary"
                    onClick={() => void rename()}
                    disabled={threadMutation.pending || !titleDraft.trim()}
                  >
                    {t("appThread.threads.saveTitle")}
                  </button>
                  <button
                    type="button"
                    className="button button-ghost"
                    onClick={() => setRenaming(false)}
                    disabled={threadMutation.pending}
                  >
                    {t("appThread.cancel")}
                  </button>
                </div>
              </div>
            ) : (
              <div className="thread-admin-actions">
                <button
                  type="button"
                  className="button button-ghost"
                  onClick={() => {
                    setRenaming(true);
                    setTitleDraft(current?.title ?? "");
                  }}
                  disabled={!current}
                >
                  {t("appThread.threads.rename")}
                </button>
                <button
                  type="button"
                  className="button button-ghost"
                  onClick={() => setConfirmDelete(true)}
                  disabled={!current || cannotDelete}
                  title={cannotDelete ? t("appThread.noPermissionHint") : undefined}
                >
                  {t("appThread.threads.delete")}
                </button>
                {cannotDelete && <span className="state-hint" role="note">{t("appThread.noPermissionHint")}</span>}
              </div>
            )}

            {confirmDelete && (
              <InlineConfirm
                title={t("appThread.threads.delete")}
                text={t("appThread.threads.confirmDelete")}
                confirmLabel={t("appThread.threads.confirm")}
                pendingLabel={t("common.loading")}
                cancelLabel={t("appThread.threads.keep")}
                pending={threadMutation.pending}
                onConfirm={() => void removeThread()}
                onCancel={() => setConfirmDelete(false)}
              />
            )}

            <InlineError error={threadMutation.error} fallback={t("errors.requestFailed")} />
          </div>
        </nav>

        {current ? (
          <AppConversationPane key={`${sessionId}:${threadId}`} threadId={threadId} turns={turns} onTurnCompleted={onTurnCompleted} />
        ) : (
          <div className="thread-chat">
            {!thread.error && <LoadingState />}
          </div>
        )}
        {current && sideOpen && (
          <aside className="thread-side" aria-label={t("appThread.railToggle")}>
            {children({ threadId, thread: current, artifacts, turns })}
          </aside>
        )}
      </div>
    </div>
  );
}
