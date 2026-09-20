"use client";

import AppThreadsPage, { type ThreadWeight } from "@/components/apps/app-threads-page";
import { SUPPORT_COPY } from "@/components/support/support-copy";

/**
 * How far a case has got, counted in turns.
 *
 * A case has no recorded artifact per lookup — the customer, the orders and
 * the policy are read through the agent and stay in the run — so the honest
 * number on the card is the conversation, not an artifact count that would
 * read as zero on every case that was answered without escalating.
 */
const WEIGHT: ThreadWeight = {
  summaryKey: "support.summary",
  values: (_artifacts, turns) => ({ turns }),
};

export default function SupportPage() {
  return <AppThreadsPage copy={SUPPORT_COPY} weight={WEIGHT} />;
}
