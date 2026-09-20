import AgentVersionDetailClient from "./agent-version-client";

export default async function AgentVersionPage({
  params,
}: {
  params: Promise<{ agentId: string; versionId: string }>;
}) {
  const { agentId, versionId } = await params;
  return <AgentVersionDetailClient agentId={agentId} versionId={versionId} />;
}
