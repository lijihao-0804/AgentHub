"use client";

import AppThreadShell from "@/components/apps/app-thread-shell";
import ArtifactsPane from "@/app/research/[threadId]/artifacts-pane";
import { RESEARCH_COPY } from "@/components/research/research-copy";

/**
 * The research workbench: the shell supplies the thread rail and the
 * conversation; research adds the papers and citations the runs produced.
 * Nothing here replaces the Playground, which stays the single-shot debug
 * view for one agent version.
 */
export default function ResearchThreadClient({ threadId }: { threadId: string }) {
  return (
    <AppThreadShell copy={RESEARCH_COPY} threadId={threadId}>
      {(context) => <ArtifactsPane threadId={context.threadId} artifacts={context.artifacts} />}
    </AppThreadShell>
  );
}
