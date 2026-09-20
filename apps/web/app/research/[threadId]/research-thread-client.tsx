"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import ArtifactsPane from "@/app/research/[threadId]/artifacts-pane";
import ConversationPane from "@/app/research/[threadId]/conversation-pane";
import Breadcrumbs from "@/components/layout/breadcrumbs";
import { InlineConfirm } from "@/components/evaluation/hash-value";
import { ErrorState, InlineError, LoadingState, SessionRequired } from "@/components/ui/states";
import { useFrontendSession } from "@/components/providers/session-provider";
import { useWorkspaceData, useWorkspaceMutation } from "@/hooks/use-workspace-data";
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

/**
 * The research workbench: threads, the conversation, and the artifacts the
 * conversation produced, side by side. Nothing here replaces the Playground,
 * which stays the single-shot debug view for one agent version.
 */
export default function ResearchThreadClient({ threadId }: { threadId: string }) {
  const { t, formatDateTime } = useI18n();
  const { connected, sessionId, workspaceId } = useFrontendSession();
  const router = useRouter();

  const scope = `${workspaceId}:${threadId}`;

  const loadThread = useCallback((auth: AuthInput) => getThread(auth, threadId), [threadId]);
  const loadThreadList = useCallback((auth: AuthInput) => listThreads(auth, { limit: 50 }), []);
  const loadTurns = useCallback((auth: AuthInput) => listThreadTurns(auth, threadId), [threadId]);
  const loadArtifacts = useCallback(
    (auth: AuthInput) => listThreadArtifacts(auth, threadId, { limit: 200 }),
    [threadId],
  );

  const thread = useWorkspaceData<Thread>(loadThread, `research-thread:${scope}`);
  const threads = useWorkspaceData<Thread[]>(loadThreadList, `research-threads:${workspaceId}`);
  const turns = useWorkspaceData<ThreadTurn[]>(loadTurns, `research-turns:${scope}`);
  const artifacts = useWorkspaceData<Artifact[]>(loadArtifacts, `research-artifacts:${scope}`);

  const threadMutation = useWorkspaceMutation(`research-thread:${scope}`);

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
          <p className="eyebrow">{t("research.eyebrow")}</p>
          <h1>{t("research.title")}</h1>
        </header>
        <SessionRequired contextKey="session.context.research" />
      </div>
    );
  }

  const current = thread.data;
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
      router.push("/research");
    }
  }

  return (
    <div className="page">
      <Breadcrumbs
        items={[
          { label: t("research.title"), href: "/research" },
          { label: current?.title ?? t("common.loading") },
        ]}
      />
      <header className="page-header">
        <p className="eyebrow">{t("research.eyebrow")}</p>
        <h1>{current?.title ?? t("research.title")}</h1>
        {current && (
          <p className="page-lede">{t("research.updated", { time: formatDateTime(current.updated_at) })}</p>
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
        <section className="research-pane research-pane-threads" aria-label={t("research.threads.paneTitle")}>
          <div className="research-pane-head">
            <h2>{t("research.threads.paneTitle")}</h2>
            <Link className="button button-ghost" href="/research">
              {t("research.newThread")}
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
                      href={`/research/${encodeURIComponent(item.id)}`}
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
                    {t("research.threads.renameLabel")}
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
                      {t("research.threads.saveTitle")}
                    </button>
                    <button
                      type="button"
                      className="button button-ghost"
                      onClick={() => setRenaming(false)}
                      disabled={threadMutation.pending}
                    >
                      {t("research.cancel")}
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
                    {t("research.threads.rename")}
                  </button>
                  <button
                    type="button"
                    className="button button-ghost"
                    onClick={() => setConfirmDelete(true)}
                    disabled={!current}
                  >
                    {t("research.threads.delete")}
                  </button>
                </div>
              )}

              {confirmDelete && (
                <InlineConfirm
                  title={t("research.threads.delete")}
                  text={t("research.threads.confirmDelete")}
                  confirmLabel={t("research.threads.confirm")}
                  pendingLabel={t("common.loading")}
                  cancelLabel={t("research.threads.keep")}
                  pending={threadMutation.pending}
                  onConfirm={() => void removeThread()}
                  onCancel={() => setConfirmDelete(false)}
                />
              )}

              <InlineError error={threadMutation.error} fallback={t("errors.requestFailed")} />
            </div>
          </div>
        </section>

        <ConversationPane threadId={threadId} turns={turns} onTurnCompleted={onTurnCompleted} />
        <ArtifactsPane threadId={threadId} artifacts={artifacts} />
      </div>
    </div>
  );
}
