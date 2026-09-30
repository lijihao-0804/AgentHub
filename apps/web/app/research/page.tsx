"use client";

import AppThreadsPage, { type ThreadWeight } from "@/components/apps/app-threads-page";
import { RESEARCH_COPY } from "@/components/research/research-copy";

/**
 * The card count is the conversation; the papers a thread produced live in
 * its third pane, where the filters are.
 */
const WEIGHT: ThreadWeight = {
  summaryKey: "research.summary",
  values: (_artifacts, turns) => ({ turns }),
};

export default function ResearchPage() {
  return <AppThreadsPage copy={RESEARCH_COPY} weight={WEIGHT} />;
}
