import VersionDetailClient from "./version-detail-client";

export default async function DatasetVersionPage({
  params,
}: {
  params: Promise<{ datasetId: string; versionId: string }>;
}) {
  const { datasetId, versionId } = await params;
  return <VersionDetailClient datasetId={datasetId} versionId={versionId} />;
}
