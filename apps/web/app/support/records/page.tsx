import SupportRecords from "./support-records";

export default async function SupportRecordsPage({
  searchParams,
}: {
  searchParams: Promise<{ ticket?: string | string[] }>;
}) {
  const { ticket } = await searchParams;
  return <SupportRecords ticketId={typeof ticket === "string" ? ticket : undefined} />;
}
