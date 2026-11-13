import { apiRequest, type AuthInput } from "@/lib/api/client";

export type SupportCustomer = {
  id: string;
  customer_ref: string;
  name: string;
  email: string | null;
  created_at: string;
};

export const TICKET_STATES = ["OPEN", "IN_PROGRESS", "PENDING", "CLOSED"] as const;
export type TicketStatus = (typeof TICKET_STATES)[number];
export type TicketTarget = Exclude<TicketStatus, "OPEN">;

export type SupportTicket = {
  id: string;
  ticket_ref: string;
  customer_id: string;
  subject: string;
  status: string;
  created_at: string;
  version: number;
};

export type TicketDetail = SupportTicket & { customer_ref: string; customer_name: string };
type Page<T> = { items: T[]; total: number };

function supportBase(auth: AuthInput) {
  return `/api/v1/workspaces/${encodeURIComponent(auth.workspaceId)}/support`;
}

export function listSupportCustomers(auth: AuthInput, query: string, limit: number, offset: number) {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (query) params.set("q", query);
  return apiRequest<Page<SupportCustomer>>(`${supportBase(auth)}/customers?${params}`, auth.accessToken);
}

export function registerSupportCustomer(auth: AuthInput, body: { customer_ref: string; name: string; email: string | null }) {
  return apiRequest<SupportCustomer>(`${supportBase(auth)}/customers`, auth.accessToken, { method: "POST", body });
}

export function listCustomerTickets(auth: AuthInput, customerId: string, limit: number, offset: number) {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  return apiRequest<Page<SupportTicket>>(
    `${supportBase(auth)}/customers/${encodeURIComponent(customerId)}/tickets?${params}`, auth.accessToken,
  );
}

export function getSupportTicket(auth: AuthInput, ticketId: string) {
  return apiRequest<TicketDetail>(`${supportBase(auth)}/tickets/${encodeURIComponent(ticketId)}`, auth.accessToken);
}

export function updateTicketStatus(auth: AuthInput, ticketId: string, expectedVersion: number, status: TicketTarget) {
  return apiRequest<TicketDetail>(`${supportBase(auth)}/tickets/${encodeURIComponent(ticketId)}`, auth.accessToken, {
    method: "PATCH", body: { expected_version: expectedVersion, status },
  });
}

export function supportTicketHref(ticketId: string) {
  return `/support/records?ticket=${encodeURIComponent(ticketId)}`;
}
