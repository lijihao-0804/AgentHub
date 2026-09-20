import RunDetailClient from "@/app/runs/[runId]/run-detail-client";

export default async function RunDetailPage({
  params,
}: {
  params: Promise<{ runId: string }>;
}) {
  const { runId } = await params;
  return <RunDetailClient runId={runId} />;
}
