import AgentPlaygroundClient from "./playground-client";

export default async function AgentPlaygroundPage({
  params,
}: {
  params: Promise<{ agentId: string }>;
}) {
  const { agentId } = await params;
  return <AgentPlaygroundClient agentId={agentId} />;
}
