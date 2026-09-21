import SupportThreadClient from "@/components/support/support-thread-client";

export default async function SupportThreadPage({ params }: { params: Promise<{ threadId: string }> }) {
  const { threadId } = await params;
  return <SupportThreadClient threadId={threadId} />;
}
