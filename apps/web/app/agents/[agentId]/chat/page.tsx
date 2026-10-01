import AgentChatPageClient from "./chat-page-client";

export default async function AgentChatPage({ params }: { params: Promise<{ agentId: string }> }) {
  const { agentId } = await params;
  return <AgentChatPageClient agentId={agentId} />;
}
