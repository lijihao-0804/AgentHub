"use client";

import AppThreadShell from "@/components/apps/app-thread-shell";
import ActivityPane from "@/components/support/activity-pane";
import ContextPane from "@/components/support/context-pane";
import HandoffPane from "@/components/support/handoff-pane";
import { SUPPORT_COPY } from "@/components/support/support-copy";

/**
 * A support case. The shell supplies the thread rail and the conversation;
 * the case adds who it is about, what the agent actually did, and the
 * escalation that hands the work to a person without losing any of it.
 */
export default function SupportThreadClient({ threadId }: { threadId: string }) {
  return (
    <AppThreadShell copy={SUPPORT_COPY} threadId={threadId}>
      {(context) => (
        <div className="app-pane-stack">
          <ContextPane artifacts={context.artifacts} />
          <ActivityPane turns={context.turns} />
          <HandoffPane threadId={context.threadId} artifacts={context.artifacts} />
        </div>
      )}
    </AppThreadShell>
  );
}
