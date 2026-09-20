import VersionCompareClient from "./version-compare-client";

export default async function AgentVersionComparePage({
  params,
}: {
  params: Promise<{ agentId: string }>;
}) {
  const { agentId } = await params;
  return <VersionCompareClient agentId={agentId} />;
}
