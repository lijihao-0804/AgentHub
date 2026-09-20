import ExperimentDetailClient from "./experiment-detail-client";

export default async function ExperimentDetailPage({
  params,
}: {
  params: Promise<{ experimentId: string }>;
}) {
  const { experimentId } = await params;
  return <ExperimentDetailClient experimentId={experimentId} />;
}
