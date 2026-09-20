import ResearchThreadClient from "@/app/research/[threadId]/research-thread-client";

export default async function ResearchThreadPage({ params }: { params: Promise<{ threadId: string }> }) {
  const { threadId } = await params;
  return <ResearchThreadClient threadId={threadId} />;
}
