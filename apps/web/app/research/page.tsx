"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState, type FormEvent } from "react";

import { EmptyState, ErrorState, InlineError, LoadingState, Panel, SessionRequired } from "@/components/ui/states";
import { useFrontendSession } from "@/components/providers/session-provider";
import { useWorkspaceData, useWorkspaceMutation } from "@/hooks/use-workspace-data";
import { errorHintKey, type AuthInput } from "@/lib/api/client";
import { listAgents, type Agent } from "@/lib/api/agents";
import { artifactPapers, listThreadArtifacts } from "@/lib/api/artifacts";
import { createThread, listThreads, listThreadTurns, type Thread } from "@/lib/api/threads";
import { useI18n } from "@/i18n/provider";

const THREAD_PAGE_SIZE = 20;

/**
 * A thread row is only useful with its weight on it: how much conversation it
 * holds and how many distinct papers came out of it. Both are counted from the
 * thread's own turns and artifacts rather than guessed from the title.
 */
type ThreadSummary = { thread: Thread; turns: number; papers: number };

async function summarize(auth: AuthInput, thread: Thread): Promise<ThreadSummary> {
  const [turns, artifacts] = await Promise.all([
    listThreadTurns(auth, thread.id),
    listThreadArtifacts(auth, thread.id, { limit: 200 }),
  ]);
  const paperIds = new Set<string>();
  for (const artifact of artifacts) {
    for (const paper of artifactPapers(artifact)) paperIds.add(paper.paper_id);
  }
  return { thread, turns: turns.length, papers: paperIds.size };
}

export default function ResearchPage() {
  const { t, formatDateTime, formatNumber } = useI18n();
  const { connected, sessionId, workspaceId } = useFrontendSession();
  const router = useRouter();

  const loadSummaries = useCallback(async (auth: AuthInput) => {
    const threads = await listThreads(auth, { kind: "research", limit: THREAD_PAGE_SIZE });
    return Promise.all(threads.map((thread) => summarize(auth, thread)));
  }, []);
  const loadAgentList = useCallback((auth: AuthInput) => listAgents(auth), []);

  const threads = useWorkspaceData<ThreadSummary[]>(loadSummaries, `research-threads:${workspaceId}`);
  const agents = useWorkspaceData<Agent[]>(loadAgentList, `agents:${workspaceId}`);
  const mutation = useWorkspaceMutation(`research-threads:${workspaceId}`);

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
          <p className="eyebrow">{t("research.eyebrow")}</p>
          <h1>{t("research.title")}</h1>
          <p className="page-lede">{t("research.lede")}</p>
        </header>
        <SessionRequired contextKey="session.context.research" />
      </div>
    );
  }

  const agentList = agents.data ?? [];
  const summaries = threads.data ?? [];

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const created = await mutation.run((auth) =>
      createThread(auth, agentId, { title: title.trim(), kind: "research" }),
    );
    if (created) {
      setShowForm(false);
      setTitle("");
      router.push(`/research/${encodeURIComponent(created.id)}`);
    }
  }

  return (
    <div className="page">
      <header className="page-header">
        <p className="eyebrow">{t("research.eyebrow")}</p>
        <h1>{t("research.title")}</h1>
        <p className="page-lede">{t("research.lede")}</p>
      </header>

      <Panel
        ariaLabel={t("research.recent")}
        title={t("research.recent")}
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
            {t("research.newThread")}
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
            {t("research.needsAgent")} <Link href="/agents">{t("nav.agents")}</Link>
          </p>
        )}

        {showForm && (
          <form className="eval-form" onSubmit={submit} noValidate>
            <p className="eval-form-title">{t("research.createTitle")}</p>
            <div className="form-grid">
              <label>
                {t("research.agent")}
                <select value={agentId} onChange={(event) => setAgentId(event.target.value)}>
                  <option value="">{t("research.selectAgent")}</option>
                  {agentList.map((agent) => (
                    <option value={agent.id} key={agent.id}>
                      {agent.name}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                {t("research.threadTitle")}
                <input
                  value={title}
                  onChange={(event) => setTitle(event.target.value)}
                  placeholder={t("research.threadTitlePlaceholder")}
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
                {t("research.create")}
              </button>
              <button type="button" className="button button-ghost" onClick={() => setShowForm(false)}>
                {t("research.cancel")}
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
          <EmptyState title={t("research.noThreads")} hint={t("research.noThreadsHint")} />
        )}

        {summaries.length > 0 && (
          <ul className="research-thread-cards">
            {summaries.map(({ thread, turns, papers }) => (
              <li key={thread.id}>
                <Link className="research-thread-card" href={`/research/${encodeURIComponent(thread.id)}`}>
                  <span className="research-thread-card-title">{thread.title}</span>
                  <span className="research-thread-card-meta">
                    {t("research.updated", { time: formatDateTime(thread.updated_at) })}
                  </span>
                  <span className="research-thread-card-summary">
                    {t("research.summary", {
                      papers: formatNumber(papers),
                      turns: formatNumber(turns),
                    })}
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
