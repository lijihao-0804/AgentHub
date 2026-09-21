import IncidentThreadClient from "@/app/incidents/[threadId]/incident-thread-client";

export default async function IncidentThreadPage({
  params,
}: {
  params: Promise<{ threadId: string }>;
}) {
  const { threadId } = await params;
  return <IncidentThreadClient threadId={threadId} />;
}
