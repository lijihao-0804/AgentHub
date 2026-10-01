import AgentChatThreadClient from "./chat-thread-client";

export default async function AgentChatThreadPage({
  params,
}: {
  params: Promise<{ agentId: string; threadId: string }>;
}) {
  const { agentId, threadId } = await params;
  return <AgentChatThreadClient agentId={agentId} threadId={threadId} />;
}
