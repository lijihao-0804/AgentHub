"use client";

import AppThreadsPage, { type ThreadWeight } from "@/components/apps/app-threads-page";
import { INCIDENT_COPY } from "@/components/incidents/incident-copy";
import { mergeTimelines } from "@/lib/api/artifacts";

/**
 * How heavy an incident is, counted from what the runs actually recorded.
 *
 * Events, not artifacts: one `get_recent_deployments` call records five
 * deployments in a single artifact, and an investigation that read five
 * deployments is not the same size as one that read one. `mergeTimelines`
 * is also what the thread page shows, so the number on the card is the
 * number of rows behind it.
 */
const WEIGHT: ThreadWeight = {
  summaryKey: "incidents.summary",
  values: (artifacts, turns) => ({ events: mergeTimelines(artifacts).length, turns }),
};

export default function IncidentsPage() {
  return <AppThreadsPage copy={INCIDENT_COPY} weight={WEIGHT} />;
}
