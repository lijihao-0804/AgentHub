import ToolDetailClient from "@/app/tools/[toolId]/tool-detail-client";

export default async function ToolDetailPage({ params }: { params: Promise<{ toolId: string }> }) {
  const { toolId } = await params;
  return <ToolDetailClient toolId={toolId} />;
}
