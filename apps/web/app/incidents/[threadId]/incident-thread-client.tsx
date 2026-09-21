"use client";

import AppThreadShell from "@/components/apps/app-thread-shell";
import { INCIDENT_COPY } from "@/components/incidents/incident-copy";
import IncidentPanes from "@/components/incidents/incident-panes";

export default function IncidentThreadClient({ threadId }: { threadId: string }) {
  return (
    <AppThreadShell copy={INCIDENT_COPY} threadId={threadId}>
      {({ artifacts }) => <IncidentPanes threadId={threadId} artifacts={artifacts} />}
    </AppThreadShell>
  );
}
