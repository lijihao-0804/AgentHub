"use client";

import { ANALYTICS_COPY } from "@/components/analytics/analytics-copy";
import AppThreadsPage, { type ThreadWeight } from "@/components/apps/app-threads-page";
import { ANALYSIS_QUERY_RESULT } from "@/lib/api/artifacts";

/**
 * How heavy an analysis is, counted from what the runs actually recorded.
 *
 * One `analysis.query_result` is one `query_sql` call, so the count is the
 * number of questions the thread put to the warehouse — not the number of
 * rows it got back, which says more about the data than about the work.
 */
const WEIGHT: ThreadWeight = {
  summaryKey: "analytics.summary",
  values: (artifacts, turns) => ({
    queries: artifacts.filter((artifact) => artifact.type === ANALYSIS_QUERY_RESULT).length,
    turns,
  }),
};

export default function AnalyticsPage() {
  return <AppThreadsPage copy={ANALYTICS_COPY} weight={WEIGHT} />;
}
