"use client";

import { useParams } from "next/navigation";

import AppWorkbench, { type ThreadWeight } from "@/components/apps/app-workbench";
import ArtifactsPane from "@/components/research/artifacts-pane";
import { RESEARCH_COPY } from "@/components/research/research-copy";

const WEIGHT: ThreadWeight = {
  summaryKey: "research.summary",
  values: (_artifacts, turns) => ({ turns }),
};

/**
 * The research workbench. One route serves both states: no thread selected
 * shows the create screen, `.../[threadId]` opens the conversation — the
 * workbench itself never remounts, so the rail and its state persist.
 */
export default function ResearchWorkbenchPage() {
  const params = useParams<{ threadId?: string[] }>();
  const threadId = params?.threadId?.[0];
  return (
    <AppWorkbench copy={RESEARCH_COPY} weight={WEIGHT} threadId={threadId}>
      {(context) => <ArtifactsPane threadId={context.threadId} artifacts={context.artifacts} />}
    </AppWorkbench>
  );
}
