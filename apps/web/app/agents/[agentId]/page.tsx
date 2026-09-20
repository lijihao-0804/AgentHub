import AgentDetailClient from "./agent-detail-client";

export default async function AgentDetailPage({ params }: { params: Promise<{ agentId: string }> }) {
  const { agentId } = await params;
  return <AgentDetailClient agentId={agentId} />;
}
