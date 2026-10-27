"use client";

import { useParams } from "next/navigation";

import AppWorkbench, { type ThreadWeight } from "@/components/apps/app-workbench";
import ActivityPane from "@/components/support/activity-pane";
import ContextPane from "@/components/support/context-pane";
import HandoffPane from "@/components/support/handoff-pane";
import { SUPPORT_COPY } from "@/components/support/support-copy";

const WEIGHT: ThreadWeight = {
  summaryKey: "support.summary",
  values: (_artifacts, turns) => ({ turns }),
};

/**
 * The support workbench. One route, two states: create screen without a
 * thread, conversation with one. The right rail carries who the case is
 * about, what the agent did, and the escalation to a person.
 */
export default function SupportWorkbenchPage() {
  const params = useParams<{ threadId?: string[] }>();
  const threadId = params?.threadId?.[0];
  return (
    <AppWorkbench copy={SUPPORT_COPY} weight={WEIGHT} threadId={threadId}>
      {(context) => (
        <div className="app-pane-stack">
          <ContextPane artifacts={context.artifacts} />
          <ActivityPane turns={context.turns} />
          <HandoffPane threadId={context.threadId} artifacts={context.artifacts} />
        </div>
      )}
    </AppWorkbench>
  );
}
