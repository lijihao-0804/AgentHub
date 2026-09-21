"use client";

import { ANALYTICS_COPY } from "@/components/analytics/analytics-copy";
import FindingPane from "@/components/analytics/finding-pane";
import QueriesPane from "@/components/analytics/queries-pane";
import AppThreadShell from "@/components/apps/app-thread-shell";

/**
 * The analysis workbench. Threads and the conversation come from the shell,
 * which is the same shell every application runs on; the third column is the
 * two panes that make an analysis one — what was actually queried, and what a
 * person concluded from it.
 */
export default function AnalyticsThreadClient({ threadId }: { threadId: string }) {
  return (
    <AppThreadShell copy={ANALYTICS_COPY} threadId={threadId}>
      {(context) => (
        <div className="app-pane-stack">
          <QueriesPane threadId={context.threadId} artifacts={context.artifacts} />
          <FindingPane threadId={context.threadId} artifacts={context.artifacts} />
        </div>
      )}
    </AppThreadShell>
  );
}
