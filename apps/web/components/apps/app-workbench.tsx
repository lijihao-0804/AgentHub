"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";

import type { AppThreadCopy } from "@/components/apps/app-thread-copy";
import AppConversationPane from "@/components/apps/app-conversation-pane";
import Icon, { type IconName } from "@/components/ui/icon";
import { InlineConfirm } from "@/components/evaluation/hash-value";
import { EmptyState, ErrorState, InlineError, LoadingState, SessionRequired } from "@/components/ui/states";
import { useFrontendSession } from "@/components/providers/session-provider";
import { useWorkspaceData, useWorkspaceMutation, type WorkspaceQuery } from "@/hooks/use-workspace-data";
import { ApiError, errorHintKey, type AuthInput } from "@/lib/api/client";
import { listThreadArtifacts, type Artifact } from "@/lib/api/artifacts";
import {
  createThread,
  deleteThread,
  getThread,
  listThreads,
  listThreadTurns,
  patchThread,
  type Thread,
  type ThreadTurn,
} from "@/lib/api/threads";
import { listAgents, type Agent } from "@/lib/api/agents";
import type { MessageKey } from "@/i18n/messages";
import { useI18n } from "@/i18n/provider";

export type ThreadWeight = {
  summaryKey: MessageKey;
  values: (artifacts: Artifact[], turns: number) => Record<string, number>;
};

/** What an application's own panes get from the workbench. */
export type AppThreadContext = {
  threadId: string;
  thread: Thread | null;
  artifacts: WorkspaceQuery<Artifact[]>;
  turns: WorkspaceQuery<ThreadTurn[]>;
};

type ThreadSummary = { thread: Thread; values: Record<string, number> | null };

const THREAD_PAGE_SIZE = 20;

/**
 * The application workbench: conversation list, chat, and the application's
 * own panes on one full-height surface — a thread is one click away at all
 * times instead of living on a page of its own. `threadId` selects the
 * conversation; without it the center shows the create screen.
 *
 * The list, the rail state and the scroll survive switching threads because
 * the workbench itself never remounts — only the conversation pane does.
 */
export default function AppWorkbench({
  copy,
  weight = { summaryKey: "appThread.turnsCount", values: (_artifacts, turns) => ({ turns }) },
  threadId,
  children,
}: {
  copy: AppThreadCopy;
  weight?: ThreadWeight;
  threadId?: string;
  children: (context: AppThreadContext) => ReactNode;
}) {
  const { t, locale, formatDateTime, formatRelativeTime, formatCount } = useI18n();
  const { connected, sessionId, workspaceId, accessToken, permissions, queueTaskDraft, takeTaskDraft } = useFrontendSession();
  const cannotDelete = permissions === null || !permissions.includes("agent_run");
  const cannotCreate = cannotDelete;
  const router = useRouter();

  const listScope = `${copy.basePath}:${workspaceId}`;
  const scope = `${copy.basePath}:${workspaceId}:${threadId ?? "none"}`;
  const countOnly = copy.kind === "support" || copy.kind === "research";

  const summarize = useCallback(
    async (auth: AuthInput, thread: Thread): Promise<ThreadSummary> => {
      const cacheKey = `${thread.id}:${thread.updated_at}`;
      const cached = summaryCacheRef.current.get(cacheKey);
      if (cached) return cached;
      const [turnsResult, artifactsResult] = await Promise.allSettled([
        listThreadTurns(auth, thread.id),
        listThreadArtifacts(auth, thread.id, { limit: 200 }),
      ]);
      const turnItems = turnsResult.status === "fulfilled" ? turnsResult.value : [];
      const artifactItems = artifactsResult.status === "fulfilled" ? artifactsResult.value : [];
      const summary: ThreadSummary = {
        thread,
        values: weight.values(artifactItems, turnItems.length),
      };
      summaryCacheRef.current.set(cacheKey, summary);
      return summary;
    },
    [weight],
  );
  const loadThreadPage = useCallback(
    async (auth: AuthInput, offset = 0): Promise<ThreadSummary[]> => {
      const page = await listThreads(auth, {
        kind: copy.kind, agentId: copy.agentId, limit: THREAD_PAGE_SIZE, offset,
        ...(countOnly ? { includeTurnCount: true } : {}),
      });
      if (countOnly) return page.map((thread) => ({
        thread,
        values: typeof thread.turn_count === "number" ? weight.values([], thread.turn_count) : null,
      }));
      return await Promise.all(page.map((thread) => summarize(auth, thread)));
    },
    [copy.kind, copy.agentId, countOnly, summarize, weight],
  );
  const loadAgents = useCallback((auth: AuthInput) => listAgents(auth, { publishedOnly: true }), []);
  const threads = useWorkspaceData<ThreadSummary[]>(loadThreadPage, `app-threads:${listScope}`);
  const agents = useWorkspaceData<Agent[]>(loadAgents, `app-threads-agents:${listScope}`, { enabled: !threadId });
  const mutation = useWorkspaceMutation(`app-threads:${listScope}`);

  const loadThread = useCallback(
    async (auth: AuthInput) => {
      const result = await getThread(auth, threadId!);
      if (result.kind !== copy.kind || (copy.agentId && result.agent_id !== copy.agentId)) {
        throw new ApiError("THREAD_NOT_FOUND", "The thread was not found for this agent.", 404);
      }
      return result;
    },
    [threadId, copy.agentId, copy.kind],
  );
  const loadTurns = useCallback(
    (auth: AuthInput) => listThreadTurns(auth, threadId!),
    [threadId],
  );
  const loadArtifacts = useCallback(
    (auth: AuthInput) => listThreadArtifacts(auth, threadId!, { limit: 200 }),
    [threadId],
  );
  const thread = useWorkspaceData<Thread>(loadThread, `app-thread:${scope}`, { enabled: Boolean(threadId) });
  const turns = useWorkspaceData<ThreadTurn[]>(loadTurns, `app-turns:${scope}`, { enabled: Boolean(threadId) });
  const artifacts = useWorkspaceData<Artifact[]>(loadArtifacts, `app-artifacts:${scope}`, {
    enabled: Boolean(threadId),
  });

  const threadMutation = useWorkspaceMutation(`app-thread:${scope}`);

  const [railOpen, setRailOpen] = useState(true);
  const [sideOpen, setSideOpen] = useState(false);
  const [showCreate, setShowCreate] = useState(false);
  const [agentId, setAgentId] = useState("");
  const [title, setTitle] = useState("");
  const initialTask = useRef("");
  const [renaming, setRenaming] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [threadSearch, setThreadSearch] = useState("");
  const isChinese = locale === "zh-CN";
  const appIcon: IconName = copy.kind === "research" ? "research" : copy.kind === "incident" ? "incidents" : copy.kind === "analysis" ? "analytics" : copy.kind === "support" ? "support" : "agents";

  useEffect(() => {
    setShowCreate(false);
    setAgentId(copy.agentId ?? "");
    setTitle("");
    setRenaming(false);
    setTitleDraft("");
    setConfirmDelete(false);
  }, [sessionId, threadId, copy.agentId]);

  useEffect(() => { setThreadSearch(""); }, [sessionId, listScope]);

  useEffect(() => {
    if (!railOpen) return;
    const dismiss = (event: KeyboardEvent) => {
      if (event.key === "Escape" && window.matchMedia("(max-width: 860px)").matches) setRailOpen(false);
    };
    document.addEventListener("keydown", dismiss);
    return () => document.removeEventListener("keydown", dismiss);
  }, [railOpen]);

  // Older bookmarks used a query string. New tasks travel in the current session.
  useEffect(() => {
    if (typeof window === "undefined" || threadId) return;
    const seed = takeTaskDraft(copy.basePath) || new URLSearchParams(window.location.search).get("draft");
    if (seed) initialTask.current = seed.slice(0, 32000);
    if (initialTask.current) setTitle(initialTask.current.split(/\r?\n/, 1)[0].slice(0, 200));
  }, [copy.basePath, takeTaskDraft, threadId]);

  // On phones the thread list is an overlay drawer; start it closed so the
  // conversation is visible first.
  useEffect(() => {
    if (typeof window !== "undefined" && window.innerWidth <= 860) setRailOpen(false);
  }, []);

  // ---- Rail summaries (per-thread turn counts, cached by updated_at) ----
  const summaryCacheRef = useRef(new Map<string, ThreadSummary>());
  useEffect(() => {
    summaryCacheRef.current.clear();
  }, [sessionId]);

  const [extraPages, setExtraPages] = useState<ThreadSummary[][]>([]);
  const [loadingMore, setLoadingMore] = useState(false);
  const [hasMore, setHasMore] = useState(true);
  const activeListRef = useRef({ sessionId, scope: listScope, generation: 0 });
  useEffect(() => {
    activeListRef.current = { sessionId, scope: listScope, generation: activeListRef.current.generation + 1 };
    setExtraPages([]);
    setHasMore((threads.data?.length ?? 0) >= THREAD_PAGE_SIZE);
    setLoadingMore(false);
  }, [threads.data, sessionId, listScope]);

  const loadMore = useCallback(async () => {
    if (!connected || !accessToken || loadingMore) return;
    const generation = ++activeListRef.current.generation;
    const isCurrent = () =>
      activeListRef.current.sessionId === sessionId && activeListRef.current.scope === listScope && activeListRef.current.generation === generation;
    setLoadingMore(true);
    try {
      const offset = (threads.data?.length ?? 0) + extraPages.flat().length;
      const more = await loadThreadPage({ workspaceId, accessToken }, offset);
      if (!isCurrent()) return;
      setExtraPages((current) => [...current, more]);
      if (more.length < THREAD_PAGE_SIZE) setHasMore(false);
    } catch {
      // A failed page keeps the visible list untouched; the button stays.
    } finally {
      if (isCurrent()) setLoadingMore(false);
    }
  }, [connected, accessToken, loadingMore, threads.data, extraPages, loadThreadPage, workspaceId, sessionId, listScope]);

  const threadList = useMemo(() => {
    const seen = new Set<string>();
    const items: ThreadSummary[] = [];
    for (const summary of [...(threads.data ?? []), ...extraPages.flat()]) {
      if (seen.has(summary.thread.id)) continue;
      seen.add(summary.thread.id);
      items.push(summary);
    }
    return items;
  }, [threads.data, extraPages]);

  const visibleThreads = useMemo(() => {
    const query = threadSearch.trim().toLocaleLowerCase(locale);
    return query ? threadList.filter(({ thread: item }) => item.title.toLocaleLowerCase(locale).includes(query)) : threadList;
  }, [threadList, threadSearch, locale]);

  const current = thread.data ?? null;
  const agentList = (agents.data ?? []).filter((agent) => !copy.agentId || agent.id === copy.agentId);
  const selectedAgent = agentList.find((agent) => agent.id === (copy.agentId ?? agentId));
  const templateKey = copy.kind === "analysis" ? "data_analyst" : copy.kind;

  useEffect(() => {
    if (threadId && thread.loaded && !current) setSideOpen(false);
  }, [threadId, thread.loaded, current]);

  async function submitCreate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (cannotCreate || !title.trim() || !selectedAgent) return;
    const created = await mutation.run((auth) =>
      createThread(auth, selectedAgent.id, { title: title.trim(), kind: copy.kind }),
    );
    if (created) {
      queueTaskDraft(created.id, initialTask.current);
      initialTask.current = "";
      setShowCreate(false);
      setTitle("");
      summaryCacheRef.current.clear();
      threads.reload();
      router.push(`${copy.basePath}/${encodeURIComponent(created.id)}`);
    }
  }

  async function rename() {
    const nextTitle = titleDraft.trim();
    if (cannotDelete || !nextTitle || !threadId) return;
    const result = await threadMutation.run((auth) => patchThread(auth, threadId, { title: nextTitle }));
    if (result) {
      setRenaming(false);
      thread.reload();
      threads.reload();
      summaryCacheRef.current.clear();
    }
  }

  async function removeThread() {
    if (cannotDelete || !threadId) return;
    const result = await threadMutation.run(async (auth) => {
      await deleteThread(auth, threadId);
      return true;
    });
    if (result) {
      setConfirmDelete(false);
      summaryCacheRef.current.clear();
      threads.reload();
      router.push(copy.basePath);
    }
  }

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

  const showCreateForm = showCreate || !threadId;

  return (
    <div className="thread-page">
      <header className="thread-topbar">
        <button
          type="button"
          className="button button-ghost"
          aria-pressed={railOpen}
          aria-expanded={railOpen}
          aria-controls="conversation-history"
          onClick={() => setRailOpen((open) => !open)}
        >
          {t("appThread.railToggleList")}
        </button>
        <span className="thread-app-icon"><Icon name={appIcon} size={20} /></span>
        <div className="thread-heading">
          {threadId && <p className="thread-app-name">{t(copy.title)}</p>}
          <h1 className="thread-title">{current?.title ?? t(copy.title)}</h1>
        </div>
        <div className="thread-topbar-actions">
          {copy.kind === "support" && permissions?.includes("support_records_read") && (
            <Link className="button button-ghost" href="/support/records">{t("support.records.title")}</Link>
          )}
          {threadId && (
            <button
              type="button"
              className="button button-ghost thread-side-toggle"
              aria-pressed={sideOpen}
              onClick={() => setSideOpen((open) => !open)}
            >
              {t("appThread.railToggle")}
            </button>
          )}
          {threadId && (
            <button type="button" className="button button-ghost" onClick={turns.reload} disabled={turns.loading}>
              {t("common.refresh")}
            </button>
          )}
        </div>
      </header>

      <div
        className="thread-frame"
        data-rail={railOpen ? "open" : "closed"}
        data-side={threadId && sideOpen ? "open" : "closed"}
      >
        {railOpen && (
          <nav className="thread-rail" id="conversation-history" aria-label={t("appThread.threads.paneTitle")}>
            <button
              type="button"
              className="button button-primary thread-new"
              disabled={cannotCreate}
              title={cannotCreate ? t("appThread.createPermissionHint") : undefined}
              onClick={() => threadId ? router.push(copy.basePath) : setShowCreate(true)}
            >
              <span aria-hidden="true">+</span>
              {t(copy.newThread)}
            </button>
            <label className="thread-search">
              <span className="sr-only">{isChinese ? "筛选已加载对话" : "Filter loaded conversations"}</span>
              <Icon name="search" size={15} />
              <input type="search" name="conversation-search" autoComplete="off" value={threadSearch} onChange={(event) => setThreadSearch(event.target.value)} placeholder={isChinese ? "筛选已加载对话…" : "Filter loaded conversations…"} />
            </label>
            <div className="thread-list-heading"><span>{t(copy.recent)}</span><span>{formatCount(visibleThreads.length)}</span></div>
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
              {visibleThreads.map(({ thread: item, values }) => {
                const active = item.id === threadId;
                return (
                  <li key={item.id}>
                    <Link
                      className={`thread-list-item${active ? " thread-list-item-active" : ""}`}
                      href={`${copy.basePath}/${encodeURIComponent(item.id)}`}
                      aria-current={active ? "page" : undefined}
                      onClick={() => {
                        if (typeof window !== "undefined" && window.innerWidth <= 860) setRailOpen(false);
                      }}
                    >
                      <span className="thread-list-title">{item.title}</span>
                      <span className="thread-list-meta" title={formatDateTime(item.updated_at)}>{formatRelativeTime(item.updated_at)}</span>
                      {values && weight.summaryKey && (
                        <span className="thread-list-meta">
                          {t(weight.summaryKey, Object.fromEntries(Object.entries(values).map(([key, value]) => [key, formatCount(value)])))}
                        </span>
                      )}
                    </Link>
                  </li>
                );
              })}
            </ul>
            {threadSearch.trim() && visibleThreads.length === 0 && <p className="state-hint" role="status">{isChinese ? "已加载的对话中没有匹配结果。" : "No matches in loaded conversations."}</p>}
            {hasMore && threadList.length > 0 && (
              <button type="button" className="button button-ghost" onClick={() => void loadMore()} disabled={loadingMore}>
                {loadingMore ? t("common.loading") : t("appThread.loadMore")}
              </button>
            )}
            {threads.loaded && !threads.error && threadList.length === 0 && (
              <p className="state-hint">{t(copy.noThreadsHint)}</p>
            )}

            {threadId && (
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
                        disabled={cannotDelete || threadMutation.pending || !titleDraft.trim()}
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
                      disabled={!current || cannotDelete}
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
            )}
          </nav>
        )}

        {threadId && current ? (
          <AppConversationPane key={`${sessionId}:${threadId}`} threadId={threadId} turns={turns} onTurnCompleted={onTurnCompleted} />
        ) : (
          <div className="thread-chat">
            {threadId && !thread.error && <LoadingState />}
            {thread.error && (
              <ErrorState
                code={thread.error.code}
                message={thread.error.message || t("errors.loadThread")}
                hint={errorHintKey(thread.error) ? t(errorHintKey(thread.error)!) : undefined}
                onRetry={thread.reload}
              />
            )}
            {!threadId && (
              showCreateForm ? (
                <form className="thread-welcome eval-form" onSubmit={submitCreate} noValidate>
                  <div className="thread-welcome-intro">
                    <span className="thread-welcome-icon"><Icon name={appIcon} size={29} /></span>
                    <h2>{t(copy.title)}</h2>
                    <p>{t(copy.lede)}</p>
                  </div>
                  <p className="eval-form-title">{t(copy.createTitle)}</p>
                  {!copy.agentId && (
                    <label>
                      {t("appThread.publishedAgent")}
                      <select name="agent" value={agentId} disabled={!agents.loaded || !agentList.length || cannotCreate} onChange={(event) => setAgentId(event.target.value)}>
                        <option value="">{t("appThread.selectAgent")}</option>
                        {agentList.map((agent) => (
                          <option value={agent.id} key={agent.id}>
                            {agent.name}
                          </option>
                        ))}
                      </select>
                    </label>
                  )}
                  {agents.error && <ErrorState code={agents.error.code} message={agents.error.message} onRetry={agents.reload} />}
                  {agents.loading && !agents.loaded && <LoadingState />}
                  {agents.loaded && !agentList.length && <p className="state-hint">{t("appThread.noPublishedAgent")}</p>}
                  {permissions?.includes("agent_create") && (
                    <Link href={copy.agentId ? `/agents/${encodeURIComponent(copy.agentId)}` : `/agents?template=${encodeURIComponent(templateKey)}`}>
                      {t(copy.agentId ? "agents.chat.openAgent" : "appThread.configureTemplate")}
                    </Link>
                  )}
                  <label>
                    {t("appThread.threadTitle")}
                    <input
                      name="conversation-title"
                      autoComplete="off"
                      value={title}
                      onChange={(event) => setTitle(event.target.value)}
                      placeholder={t(copy.threadTitlePlaceholder)}
                      maxLength={200}
                    />
                  </label>
                  <InlineError error={mutation.error} fallback={t("errors.requestFailed")} />
                  {cannotCreate && <p className="state-hint" role="note">{t("appThread.createPermissionHint")}</p>}
                  <div className="form-actions">
                    <button
                      type="submit"
                      className="button button-primary"
                      disabled={cannotCreate || mutation.pending || !title.trim() || !selectedAgent}
                    >
                      {mutation.pending ? t("common.loading") : t(copy.createTitle)}
                    </button>
                    {threadId && (
                      <button type="button" className="button button-ghost" onClick={() => setShowCreate(false)}>
                        {t("appThread.cancel")}
                      </button>
                    )}
                  </div>
                </form>
              ) : (
                <div className="thread-welcome thread-welcome-empty">
                  <p className="thread-welcome-title">{t(copy.createTitle)}</p>
                  <p className="state-hint">{t(copy.noThreadsHint)}</p>
                  <button type="button" className="button button-primary" onClick={() => setShowCreate(true)}>
                    {t(copy.newThread)}
                  </button>
                </div>
              )
            )}
          </div>
        )}

        {railOpen && <button className="thread-rail-scrim" type="button" aria-label={isChinese ? "关闭对话列表" : "Close conversation list"} onClick={() => setRailOpen(false)} />}

        {threadId && current && sideOpen && (
          <aside className="thread-side" aria-label={t("appThread.railToggle")}>
            {children({ threadId, thread: current, artifacts, turns })}
          </aside>
        )}
      </div>
    </div>
  );
}
