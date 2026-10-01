"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";

import type { AppThreadCopy } from "@/components/apps/app-thread-copy";
import { EmptyState, ErrorState, InlineError, LoadingState, Panel, SessionRequired } from "@/components/ui/states";
import { useFrontendSession } from "@/components/providers/session-provider";
import { useWorkspaceData, useWorkspaceMutation } from "@/hooks/use-workspace-data";
import { errorHintKey, type AuthInput } from "@/lib/api/client";
import { listAgents, type Agent } from "@/lib/api/agents";
import { listThreadArtifacts, type Artifact } from "@/lib/api/artifacts";
import { createThread, listThreads, listThreadTurns, type Thread } from "@/lib/api/threads";
import type { MessageKey } from "@/i18n/messages";
import { useI18n } from "@/i18n/provider";

const THREAD_PAGE_SIZE = 20;

/**
 * A thread row is only useful with its weight on it. What the weight is
 * differs per application — papers for research, recorded events for an
 * incident, queries for an analysis — so the counting is the caller's, and
 * every count comes from the thread's own turns and artifacts rather than
 * from its title.
 */
export type ThreadWeight = {
  summaryKey: MessageKey;
  values: (artifacts: Artifact[], turns: number) => Record<string, number>;
};

type ThreadSummary = { thread: Thread; values: Record<string, number>; degraded: boolean };

export default function AppThreadsPage({ copy, weight }: { copy: AppThreadCopy; weight: ThreadWeight }) {
  const { t, formatDateTime, formatNumber } = useI18n();
  const { connected, sessionId, workspaceId, accessToken } = useFrontendSession();
  const router = useRouter();

  const compute = weight.values;
  /** Per-thread summary cache: unchanged threads (same updated_at) are not
   * re-fetched, so revisiting a list costs one request instead of 2N. */
  const summaryCacheRef = useRef(new Map<string, ThreadSummary>());
  useEffect(() => {
    summaryCacheRef.current.clear();
  }, [sessionId]);

  const summarize = useCallback(
    async (auth: AuthInput, thread: Thread): Promise<ThreadSummary> => {
      const cacheKey = `${thread.id}:${thread.updated_at}`;
      const cached = summaryCacheRef.current.get(cacheKey);
      if (cached) return cached;
      const [turnsResult, artifactsResult] = await Promise.allSettled([
        listThreadTurns(auth, thread.id),
        listThreadArtifacts(auth, thread.id, { limit: 200 }),
      ]);
      // One failing sub-request degrades that row's counts; it must not take
      // the whole list down with it.
      const turns = turnsResult.status === "fulfilled" ? turnsResult.value : [];
      const artifacts = artifactsResult.status === "fulfilled" ? artifactsResult.value : [];
      const summary: ThreadSummary = {
        thread,
        values: compute(artifacts, turns.length),
        degraded: turnsResult.status === "rejected" || artifactsResult.status === "rejected",
      };
      summaryCacheRef.current.set(cacheKey, summary);
      return summary;
    },
    [compute],
  );

  const loadSummaries = useCallback(
    async (auth: AuthInput, offset = 0): Promise<ThreadSummary[]> => {
      const threads = await listThreads(auth, { kind: copy.kind, limit: THREAD_PAGE_SIZE, offset });
      return Promise.all(threads.map((thread) => summarize(auth, thread)));
    },
    [copy.kind, summarize],
  );
  const loadAgentList = useCallback((auth: AuthInput) => listAgents(auth), []);

  const scope = `${copy.basePath}:${workspaceId}`;
  const threads = useWorkspaceData<ThreadSummary[]>(loadSummaries, `app-threads:${scope}`);
  const agents = useWorkspaceData<Agent[]>(loadAgentList, `agents:${workspaceId}`);
  const mutation = useWorkspaceMutation(`app-threads:${scope}`);

  // Offset paging beyond the first 20 threads: the backend returns no total,
  // so a short page means the end has been reached.
  const [extraPages, setExtraPages] = useState<ThreadSummary[][]>([]);
  const [loadingMore, setLoadingMore] = useState(false);
  const [hasMore, setHasMore] = useState(true);
  useEffect(() => {
    setExtraPages([]);
    setHasMore(true);
  }, [threads.data]);

  const loadMore = useCallback(async () => {
    if (!connected || !accessToken || loadingMore) return;
    setLoadingMore(true);
    try {
      const offset = (threads.data?.length ?? 0) + extraPages.flat().length;
      const more = await loadSummaries({ workspaceId, accessToken }, offset);
      setExtraPages((current) => [...current, more]);
      if (more.length < THREAD_PAGE_SIZE) setHasMore(false);
    } catch {
      // A failed page keeps the visible list untouched; the button stays.
    } finally {
      setLoadingMore(false);
    }
  }, [connected, accessToken, loadingMore, threads.data, extraPages, loadSummaries, workspaceId]);

  const [showForm, setShowForm] = useState(false);
  const [agentId, setAgentId] = useState("");
  const [title, setTitle] = useState("");

  // A session or workspace change invalidates the half-filled form.
  useEffect(() => {
    setShowForm(false);
    setAgentId("");
    setTitle("");
  }, [sessionId]);

  if (!connected) {
    return (
      <div className="page">
        <header className="page-header">
          <p className="eyebrow">{t(copy.eyebrow)}</p>
          <h1>{t(copy.title)}</h1>
          <p className="page-lede">{t(copy.lede)}</p>
        </header>
        <SessionRequired contextKey={copy.sessionContext} />
      </div>
    );
  }

  const agentList = agents.data ?? [];
  // First page plus whatever the user paged in; dedupe by id in case a new
  // thread shifted a row across a page boundary between fetches.
  const seen = new Set<string>();
  const summaries: ThreadSummary[] = [];
  for (const summary of [...(threads.data ?? []), ...extraPages.flat()]) {
    if (seen.has(summary.thread.id)) continue;
    seen.add(summary.thread.id);
    summaries.push(summary);
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const created = await mutation.run((auth) =>
      createThread(auth, agentId, { title: title.trim(), kind: copy.kind }),
    );
    if (created) {
      setShowForm(false);
      setTitle("");
      router.push(`${copy.basePath}/${encodeURIComponent(created.id)}`);
    }
  }

  return (
    <div className="page">
      <header className="page-header">
        <p className="eyebrow">{t(copy.eyebrow)}</p>
        <h1>{t(copy.title)}</h1>
        <p className="page-lede">{t(copy.lede)}</p>
      </header>

      <Panel
        ariaLabel={t(copy.recent)}
        title={t(copy.recent)}
        actions={
          <button
            type="button"
            className="button button-primary"
            onClick={() => {
              setShowForm((open) => !open);
              setTitle("");
            }}
            disabled={agentList.length === 0}
          >
            {t(copy.newThread)}
          </button>
        }
      >
        {agents.error && (
          <ErrorState
            code={agents.error.code}
            message={agents.error.message || t("errors.loadAgents")}
            hint={errorHintKey(agents.error) ? t(errorHintKey(agents.error)!) : undefined}
            onRetry={agents.reload}
          />
        )}
        {agents.loaded && agentList.length === 0 && (
          <p className="state-hint">
            {t("appThread.needsAgent")} <Link href="/agents">{t("nav.agents")}</Link>
          </p>
        )}

        {showForm && (
          <form className="eval-form" onSubmit={submit} noValidate>
            <p className="eval-form-title">{t(copy.createTitle)}</p>
            <div className="form-grid">
              <label>
                {t("appThread.agent")}
                <select value={agentId} onChange={(event) => setAgentId(event.target.value)}>
                  <option value="">{t("appThread.selectAgent")}</option>
                  {agentList.map((agent) => (
                    <option value={agent.id} key={agent.id}>
                      {agent.name}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                {t("appThread.threadTitle")}
                <input
                  value={title}
                  onChange={(event) => setTitle(event.target.value)}
                  placeholder={t(copy.threadTitlePlaceholder)}
                  maxLength={200}
                />
              </label>
            </div>
            <InlineError error={mutation.error} fallback={t("errors.requestFailed")} />
            <div className="form-actions">
              <button
                type="submit"
                className="button button-primary"
                disabled={mutation.pending || !agentId || !title.trim()}
              >
                {t("appThread.create")}
              </button>
              <button type="button" className="button button-ghost" onClick={() => setShowForm(false)}>
                {t("appThread.cancel")}
              </button>
            </div>
          </form>
        )}

        {threads.error && (
          <ErrorState
            code={threads.error.code}
            message={threads.error.message || t("errors.loadThreads")}
            hint={errorHintKey(threads.error) ? t(errorHintKey(threads.error)!) : undefined}
            onRetry={threads.reload}
          />
        )}
        {threads.loading && !threads.error && <LoadingState />}
        {threads.loaded && !threads.error && summaries.length === 0 && (
          <EmptyState title={t(copy.noThreads)} hint={t(copy.noThreadsHint)} />
        )}

        {summaries.length > 0 && (
          <ul className="research-thread-cards">
            {summaries.map(({ thread, values, degraded }) => (
              <li key={thread.id}>
                <Link
                  className="research-thread-card"
                  href={`${copy.basePath}/${encodeURIComponent(thread.id)}`}
                >
                  <span className="research-thread-card-title">{thread.title}</span>
                  <span className="research-thread-card-meta">
                    {t("appThread.updated", { time: formatDateTime(thread.updated_at) })}
                  </span>
                  <span className="research-thread-card-summary">
                    {degraded
                      ? t("appThread.summaryDegraded")
                      : t(
                          weight.summaryKey,
                          Object.fromEntries(
                            Object.entries(values).map(([name, count]) => [name, formatNumber(count)]),
                          ),
                        )}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        )}
        {summaries.length > 0 && hasMore && (
          <div className="form-actions">
            <button
              type="button"
              className="button button-ghost"
              onClick={() => void loadMore()}
              disabled={loadingMore}
            >
              {loadingMore ? t("common.loading") : t("appThread.loadMore")}
            </button>
          </div>
        )}
      </Panel>
    </div>
  );
}
