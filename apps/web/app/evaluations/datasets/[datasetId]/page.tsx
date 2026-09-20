import DatasetDetailClient from "@/app/evaluations/datasets/[datasetId]/dataset-detail-client";

export default async function DatasetDetailPage({
  params,
}: {
  params: Promise<{ datasetId: string }>;
}) {
  const { datasetId } = await params;
  return <DatasetDetailClient datasetId={datasetId} />;
}
