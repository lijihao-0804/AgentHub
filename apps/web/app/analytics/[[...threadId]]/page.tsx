"use client";

import { useParams } from "next/navigation";

import AppWorkbench, { type ThreadWeight } from "@/components/apps/app-workbench";
import FindingPane from "@/components/analytics/finding-pane";
import QueriesPane from "@/components/analytics/queries-pane";
import { ANALYTICS_COPY } from "@/components/analytics/analytics-copy";
import { ANALYSIS_QUERY_RESULT } from "@/lib/api/artifacts";

const WEIGHT: ThreadWeight = {
  summaryKey: "analytics.summary",
  values: (artifacts, turns) => ({
    queries: artifacts.filter((artifact) => artifact.type === ANALYSIS_QUERY_RESULT).length,
    turns,
  }),
};

/**
 * The analysis workbench: thread rail, conversation, and the two panes that
 * make an analysis one — what was queried and what a person concluded.
 */
export default function AnalyticsWorkbenchPage() {
  const params = useParams<{ threadId?: string[] }>();
  const threadId = params?.threadId?.[0];
  return (
    <AppWorkbench copy={ANALYTICS_COPY} weight={WEIGHT} threadId={threadId}>
      {(context) => (
        <div className="app-pane-stack">
          <QueriesPane threadId={context.threadId} artifacts={context.artifacts} />
          <FindingPane threadId={context.threadId} artifacts={context.artifacts} />
        </div>
      )}
    </AppWorkbench>
  );
}
