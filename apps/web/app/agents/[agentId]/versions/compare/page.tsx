import VersionCompareClient from "@/app/agents/[agentId]/versions/compare/version-compare-client";

export default async function AgentVersionComparePage({
  params,
}: {
  params: Promise<{ agentId: string }>;
}) {
  const { agentId } = await params;
  return <VersionCompareClient agentId={agentId} />;
}
