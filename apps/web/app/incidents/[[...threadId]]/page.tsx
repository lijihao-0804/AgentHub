"use client";

import { useParams } from "next/navigation";

import AppWorkbench, { type ThreadWeight } from "@/components/apps/app-workbench";
import IncidentPanes from "@/components/incidents/incident-panes";
import { INCIDENT_COPY } from "@/components/incidents/incident-copy";
import { mergeTimelines } from "@/lib/api/artifacts";

const WEIGHT: ThreadWeight = {
  summaryKey: "incidents.summary",
  values: (artifacts, turns) => ({ events: mergeTimelines(artifacts).length, turns }),
};

/**
 * The incident workbench: thread rail, conversation, and the evidence panes
 * — what the agent read, what it proposed, and the one write that stops
 * behind an approval.
 */
export default function IncidentWorkbenchPage() {
  const params = useParams<{ threadId?: string[] }>();
  const threadId = params?.threadId?.[0];
  return (
    <AppWorkbench copy={INCIDENT_COPY} weight={WEIGHT} threadId={threadId}>
      {({ threadId: selected, artifacts }) => (
        <IncidentPanes threadId={selected} artifacts={artifacts} />
      )}
    </AppWorkbench>
  );
}
