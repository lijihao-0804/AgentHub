"use client";

import Link from "next/link";
import { useParams } from "next/navigation";

import AppWorkbench from "@/components/apps/app-workbench";
import type { AppThreadCopy } from "@/components/apps/app-thread-copy";
import { Panel } from "@/components/ui/states";
import { useI18n } from "@/i18n/provider";
import { useMemo } from "react";

/**
 * Multi-turn conversation with one agent, on the same workbench the four
 * applications use. Threads are `general` kind and always run the agent's
 * published version; the side rail links back to the agent's own pages.
 */
export default function AgentChatWorkbenchPage() {
  const params = useParams<{ agentId: string; threadId?: string[] }>();
  const agentId = params?.agentId ?? "";
  const threadId = params?.threadId?.[0];
  const { t } = useI18n();

  const copy = useMemo<AppThreadCopy>(
    () => ({
      kind: "general",
      agentId,
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

  return (
    <AppWorkbench copy={copy} threadId={threadId}>
      {() => (
        <Panel ariaLabel={t("agents.chat.sideTitle")} title={t("agents.chat.sideTitle")}>
          <p className="state-hint">{t("agents.chat.sideHint")}</p>
          <div className="form-actions">
            <Link className="button button-ghost" href={`/agents/${encodeURIComponent(agentId)}`}>
              {t("agents.chat.openAgent")}
            </Link>
            <Link className="button button-ghost" href={`/agents/${encodeURIComponent(agentId)}/playground`}>
              {t("agents.chat.openPlayground")}
            </Link>
          </div>
        </Panel>
      )}
    </AppWorkbench>
  );
}
