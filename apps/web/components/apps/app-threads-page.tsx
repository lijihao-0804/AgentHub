"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState, type FormEvent } from "react";

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

type ThreadSummary = { thread: Thread; values: Record<string, number> };

export default function AppThreadsPage({ copy, weight }: { copy: AppThreadCopy; weight: ThreadWeight }) {
  const { t, formatDateTime, formatNumber } = useI18n();
  const { connected, sessionId, workspaceId } = useFrontendSession();
  const router = useRouter();

  const compute = weight.values;
  const loadSummaries = useCallback(
    async (auth: AuthInput): Promise<ThreadSummary[]> => {
      const threads = await listThreads(auth, { kind: copy.kind, limit: THREAD_PAGE_SIZE });
      return Promise.all(
        threads.map(async (thread) => {
          const [turns, artifacts] = await Promise.all([
            listThreadTurns(auth, thread.id),
            listThreadArtifacts(auth, thread.id, { limit: 200 }),
          ]);
          return { thread, values: compute(artifacts, turns.length) };
        }),
      );
    },
    [compute, copy.kind],
  );
  const loadAgentList = useCallback((auth: AuthInput) => listAgents(auth), []);

  const scope = `${copy.basePath}:${workspaceId}`;
  const threads = useWorkspaceData<ThreadSummary[]>(loadSummaries, `app-threads:${scope}`);
  const agents = useWorkspaceData<Agent[]>(loadAgentList, `agents:${workspaceId}`);
  const mutation = useWorkspaceMutation(`app-threads:${scope}`);

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
  const summaries = threads.data ?? [];

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
            {summaries.map(({ thread, values }) => (
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
                    {t(
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
      </Panel>
    </div>
  );
}
