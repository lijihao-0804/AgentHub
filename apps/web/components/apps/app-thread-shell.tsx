"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState, type ReactNode } from "react";

import type { AppThreadCopy } from "@/components/apps/app-thread-copy";
import AppConversationPane from "@/components/apps/app-conversation-pane";
import Breadcrumbs from "@/components/layout/breadcrumbs";
import { InlineConfirm } from "@/components/evaluation/hash-value";
import { ErrorState, InlineError, LoadingState, SessionRequired } from "@/components/ui/states";
import { useFrontendSession } from "@/components/providers/session-provider";
import { useWorkspaceData, useWorkspaceMutation, type WorkspaceQuery } from "@/hooks/use-workspace-data";
import { errorHintKey, type AuthInput } from "@/lib/api/client";
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
  const { connected, sessionId, workspaceId } = useFrontendSession();
  const router = useRouter();

  const scope = `${copy.basePath}:${workspaceId}:${threadId}`;

  const loadThread = useCallback((auth: AuthInput) => getThread(auth, threadId), [threadId]);
  const loadThreadList = useCallback(
    (auth: AuthInput) => listThreads(auth, { kind: copy.kind, limit: 50 }),
    [copy.kind],
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
          <p className="eyebrow">{t(copy.eyebrow)}</p>
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
    <div className="page">
      <Breadcrumbs
        items={[
          { label: t(copy.title), href: copy.basePath },
          { label: current?.title ?? t("common.loading") },
        ]}
      />
      <header className="page-header">
        <p className="eyebrow">{t(copy.eyebrow)}</p>
        <h1>{current?.title ?? t(copy.title)}</h1>
        {current && (
          <p className="page-lede">
            {t("appThread.updated", { time: formatDateTime(current.updated_at) })}
          </p>
        )}
      </header>

      {thread.error && (
        <ErrorState
          code={thread.error.code}
          message={thread.error.message || t("errors.loadThread")}
          hint={errorHintKey(thread.error) ? t(errorHintKey(thread.error)!) : undefined}
          onRetry={thread.reload}
        />
      )}

      <div className="research-workbench">
        <section className="research-pane research-pane-threads" aria-label={t("appThread.threads.paneTitle")}>
          <div className="research-pane-head">
            <h2>{t("appThread.threads.paneTitle")}</h2>
            <Link className="button button-ghost" href={copy.basePath}>
              {t(copy.newThread)}
            </Link>
          </div>
          <div className="research-pane-body">
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
                    disabled={!current}
                  >
                    {t("appThread.threads.delete")}
                  </button>
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
          </div>
        </section>

        <AppConversationPane threadId={threadId} turns={turns} onTurnCompleted={onTurnCompleted} />
        {children({ threadId, thread: current, artifacts, turns })}
      </div>
    </div>
  );
}
