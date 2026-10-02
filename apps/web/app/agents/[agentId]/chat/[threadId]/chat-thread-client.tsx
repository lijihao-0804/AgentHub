"use client";

import Link from "next/link";
import { useMemo } from "react";

import AppThreadShell from "@/components/apps/app-thread-shell";
import type { AppThreadCopy } from "@/components/apps/app-thread-copy";
import { Panel } from "@/components/ui/states";
import { useI18n } from "@/i18n/provider";

export default function AgentChatThreadClient({
  agentId,
  threadId,
}: {
  agentId: string;
  threadId: string;
}) {
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
    <AppThreadShell copy={copy} threadId={threadId}>
      {(context) => (
        <Panel ariaLabel={t("agents.chat.sideTitle")} title={t("agents.chat.sideTitle")} eyebrow={t("agents.chat.eyebrow")}>
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
    </AppThreadShell>
  );
}
