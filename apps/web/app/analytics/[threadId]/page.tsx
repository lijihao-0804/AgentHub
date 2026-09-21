import AnalyticsThreadClient from "@/components/analytics/analytics-thread-client";

export default async function AnalyticsThreadPage({ params }: { params: Promise<{ threadId: string }> }) {
  const { threadId } = await params;
  return <AnalyticsThreadClient threadId={threadId} />;
}
