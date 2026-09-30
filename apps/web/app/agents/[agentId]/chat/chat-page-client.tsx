"use client";

import { useMemo } from "react";

import AppThreadsPage, { type ThreadWeight } from "@/components/apps/app-threads-page";
import type { AppThreadCopy } from "@/components/apps/app-thread-copy";

/**
 * Multi-turn conversation with one agent. The threads are `general` kind and
 * always run the agent's published version; parameter overrides inside a
 * thread turn need a runtime extension and stay out of scope here.
 */
export default function AgentChatPageClient({ agentId }: { agentId: string }) {
  const copy = useMemo<AppThreadCopy>(
    () => ({
      kind: "general",
      basePath: `/agents/${agentId}/chat`,
      eyebrow: "agents.chat.eyebrow",
      title: "agents.chat.title",
      lede: "agents.chat.lede",
      newThread: "agents.chat.newThread",
      createTitle: "agents.chat.createTitle",
      threadTitlePlaceholder: "agents.chat.threadTitlePlaceholder",
      recent: "agents.chat.recent",
      noThreads: "agents.chat.noThreads",
      noThreadsHint: "agents.chat.noThreadsHint",
      sessionContext: "session.context.agents",
    }),
    [agentId],
  );

  const weight = useMemo<ThreadWeight>(
    () => ({ summaryKey: "agents.chat.summary", values: (_artifacts, turns) => ({ turns }) }),
    [],
  );

  return <AppThreadsPage copy={copy} weight={weight} />;
}
