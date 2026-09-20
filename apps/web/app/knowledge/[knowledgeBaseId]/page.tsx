import KnowledgeBaseDetailClient from "./knowledge-detail-client";

export default async function KnowledgeBaseDetailPage({
  params,
}: {
  params: Promise<{ knowledgeBaseId: string }>;
}) {
  const { knowledgeBaseId } = await params;
  return <KnowledgeBaseDetailClient knowledgeBaseId={knowledgeBaseId} />;
}
