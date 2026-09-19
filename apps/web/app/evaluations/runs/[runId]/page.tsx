import RunDetailClient from "./run-detail-client";

export default async function EvaluationRunPage({
  params,
}: {
  params: Promise<{ runId: string }>;
}) {
  const { runId } = await params;
  return <RunDetailClient runId={runId} />;
}
