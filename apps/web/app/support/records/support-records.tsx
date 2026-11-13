"use client";

import Link from "next/link";
import { useEffect, useState, type FormEvent } from "react";

import { CatalogPageHeader } from "@/components/catalog/catalog-layout";
import { useFrontendSession } from "@/components/providers/session-provider";
import StatusBadge from "@/components/ui/status-badge";
import { EmptyState, ErrorState, InlineError, LoadingState, Panel, SessionRequired } from "@/components/ui/states";
import { useWorkspaceData, useWorkspaceMutation, type WorkspaceQuery } from "@/hooks/use-workspace-data";
import { useI18n } from "@/i18n/provider";
import {
  getSupportTicket, listCustomerTickets, listSupportCustomers, registerSupportCustomer,
  supportTicketHref, TICKET_STATES, updateTicketStatus,
  type SupportCustomer, type TicketDetail, type TicketStatus, type TicketTarget,
} from "@/lib/api/support-records";

const PAGE_SIZE = 20;

function PageControls({ offset, total, onPage }: { offset: number; total: number; onPage: (offset: number) => void }) {
  const { t, formatCount } = useI18n();
  if (!total && !offset) return null;
  return (
    <div className="records-pager">
      <span>{t("support.records.pageRange", {
        first: formatCount(total ? offset + 1 : 0), last: formatCount(Math.min(offset + PAGE_SIZE, total)), total: formatCount(total),
      })}</span>
      {total > PAGE_SIZE || offset > 0 ? (
        <div className="form-actions">
          <button type="button" className="button button-ghost" disabled={!offset} onClick={() => onPage(Math.max(0, offset - PAGE_SIZE))}>{t("support.records.previous")}</button>
          <button type="button" className="button button-ghost" disabled={offset + PAGE_SIZE >= total} onClick={() => onPage(offset + PAGE_SIZE)}>{t("support.records.next")}</button>
        </div>
      ) : null}
    </div>
  );
}

function QueryState({ query }: { query: WorkspaceQuery<unknown> }) {
  if (query.error) return <ErrorState code={query.error.code} message={query.error.message} onRetry={query.reload} />;
  return !query.loaded || query.loading ? <LoadingState /> : null;
}

function TicketBadge({ status }: { status: string }) {
  const { t } = useI18n();
  const known = TICKET_STATES.find((state) => state === status);
  return <StatusBadge status={status} label={known ? t(`support.records.states.${known}`) : status} />;
}

function RegisterCustomer({ onSaved, onCancel }: { onSaved: (customer: SupportCustomer) => void; onCancel: () => void }) {
  const { t } = useI18n();
  const mutation = useWorkspaceMutation("register-support-customer");
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const fields = new FormData(event.currentTarget);
    const customer_ref = String(fields.get("customer_ref") ?? "").trim();
    const name = String(fields.get("name") ?? "").trim();
    const email = String(fields.get("email") ?? "").trim() || null;
    if (!customer_ref || !name || mutation.pending) return;
    await mutation.run((auth) => registerSupportCustomer(auth, { customer_ref, name, email }), onSaved);
  }
  return (
    <form className="eval-form" onSubmit={submit}>
      <div className="form-grid">
        <label>{t("support.records.ref")}<input name="customer_ref" autoComplete="off" required maxLength={128} pattern=".*\S.*" /></label>
        <label>{t("support.records.name")}<input name="name" autoComplete="off" required maxLength={200} pattern=".*\S.*" /></label>
        <label>{t("support.records.emailOptional")}<input name="email" type="email" autoComplete="off" maxLength={320} /></label>
      </div>
      <InlineError error={mutation.error} />
      <div className="form-actions">
        <button className="button button-primary" type="submit" disabled={mutation.pending}>{t("support.records.register")}</button>
        <button className="button button-ghost" type="button" onClick={onCancel} disabled={mutation.pending}>{t("common.cancel")}</button>
      </div>
    </form>
  );
}

function CustomerTickets({ customerId, name, customerRef }: { customerId: string; name: string; customerRef: string }) {
  const { t, formatDateTime } = useI18n();
  const [offset, setOffset] = useState(0);
  const page = useWorkspaceData(
    (auth) => listCustomerTickets(auth, customerId, PAGE_SIZE, offset),
    `customer-tickets:${customerId}:${offset}`,
  );
  return (
    <Panel title={t("support.records.tickets")} actions={<button className="button button-ghost" type="button" disabled={page.loading} onClick={page.reload}>{t("common.refresh")}</button>}>
      <p className="records-customer"><strong>{name}</strong> <code>{customerRef}</code></p>
      <QueryState query={page} />
      {page.loaded && page.data && !page.loading && (
        <>
          {page.data.items.length ? (
            <div className="data-table"><table>
              <thead><tr><th>{t("support.records.subject")}</th><th>{t("support.records.status")}</th><th>{t("common.created")}</th></tr></thead>
              <tbody>{page.data.items.map((ticket) => (
                <tr key={ticket.id}>
                  <td data-label={t("support.records.subject")}>
                    <Link href={supportTicketHref(ticket.id)}>{ticket.subject}</Link>
                    <small className="state-hint">{ticket.ticket_ref}</small>
                  </td>
                  <td data-label={t("support.records.status")}><TicketBadge status={ticket.status} /></td>
                  <td data-label={t("common.created")}>{formatDateTime(ticket.created_at)}</td>
                </tr>
              ))}</tbody>
            </table></div>
          ) : <p className="state-hint">{t("support.records.noTickets")}</p>}
          <PageControls offset={offset} total={page.data.total} onPage={setOffset} />
        </>
      )}
    </Panel>
  );
}

function CustomerDirectory({ canManage }: { canManage: boolean }) {
  const { t } = useI18n();
  const [offset, setOffset] = useState(0);
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [registering, setRegistering] = useState(false);
  const [selected, setSelected] = useState<SupportCustomer | null>(null);
  const [savedRef, setSavedRef] = useState("");
  const page = useWorkspaceData(
    (auth) => listSupportCustomers(auth, query, PAGE_SIZE, offset),
    `support-customers:${query}:${offset}`,
  );

  function applySearch(value: string) {
    setOffset(0);
    setQuery(value.trim());
    setSearch(value);
    setSelected(null);
    setSavedRef("");
  }
  function saved(customer: SupportCustomer) {
    applySearch(customer.customer_ref);
    setSelected(customer);
    setSavedRef(customer.customer_ref);
    setRegistering(false);
    page.reload();
  }
  return (
    <div className="records-columns">
      <Panel title={t("support.records.customers")} actions={
        canManage && !registering ? <button className="button button-primary" type="button" onClick={() => setRegistering(true)}>{t("support.records.register")}</button> : undefined
      }>
        {canManage && registering && <RegisterCustomer onSaved={saved} onCancel={() => setRegistering(false)} />}
        {savedRef && <p className="inline-notice" role="status">{t("support.records.saved", { ref: savedRef })}</p>}
        <form className="records-search" role="search" onSubmit={(event) => { event.preventDefault(); applySearch(search); }}>
          <input name="q" type="search" aria-label={t("support.records.search")} value={search} maxLength={200}
            placeholder={t("support.records.searchHint")} onChange={(event) => setSearch(event.target.value)} />
          <button type="submit" className="button button-ghost">{t("support.records.search")}</button>
          {query && <button type="button" className="button button-ghost" onClick={() => applySearch("")}>{t("support.records.clear")}</button>}
        </form>
        <QueryState query={page} />
        {page.loaded && page.data && !page.loading && (
          <>
            {page.data.items.length ? (
              <ul className="records-customer-list" aria-label={t("support.records.customers")}>
                {page.data.items.map((customer) => (
                  <li key={customer.id}><button type="button" aria-pressed={selected?.id === customer.id}
                    onClick={() => { setSelected(customer); setSavedRef(""); }}>
                    <span><strong>{customer.name}</strong><code>{customer.customer_ref}</code></span>
                    {customer.email && <small>{customer.email}</small>}
                  </button></li>
                ))}
              </ul>
            ) : <EmptyState title={t(query ? "support.records.noMatch" : "support.records.noCustomers")} />}
            <PageControls offset={offset} total={page.data.total} onPage={(next) => { setOffset(next); setSelected(null); }} />
          </>
        )}
      </Panel>
      {selected ? <CustomerTickets key={selected.id} customerId={selected.id} name={selected.name} customerRef={selected.customer_ref} />
        : <Panel title={t("support.records.tickets")}><p className="state-hint">{t("support.records.selectCustomer")}</p></Panel>}
    </div>
  );
}

function TicketChangeForm({ ticket, pending, onSave }: {
  ticket: TicketDetail; pending: boolean; onSave: (target: TicketTarget) => void;
}) {
  const { t } = useI18n();
  const [target, setTarget] = useState<TicketTarget | "">("");
  const active = ticket.status === "OPEN" || ticket.status === "IN_PROGRESS" || ticket.status === "PENDING";
  if (!active) return null;
  return (
    <form className="eval-form" onSubmit={(event) => { event.preventDefault(); if (target && !pending) onSave(target); }}>
      <label>{t("support.records.nextStatus")}
        <select name="status" value={target} disabled={pending} onChange={(event) => setTarget(event.target.value as TicketTarget | "")}>
          <option value="">{t("support.records.selectStatus")}</option>
          {TICKET_STATES.filter((state) => state !== "OPEN" && state !== ticket.status).map((state) =>
            <option key={state} value={state}>{t(`support.records.states.${state}`)}</option>)}
        </select>
      </label>
      {target === "CLOSED" && <p className="state-hint">{t("support.records.closeHint")}</p>}
      <div className="form-actions"><button type="submit" className="button button-primary" disabled={!target || pending}>{t("support.records.saveStatus")}</button></div>
    </form>
  );
}

function TicketView({ ticketId, canManage }: { ticketId: string; canManage: boolean }) {
  const { t, formatDateTime } = useI18n();
  const detail = useWorkspaceData((auth) => getSupportTicket(auth, ticketId), `support-ticket:${ticketId}`);
  const mutation = useWorkspaceMutation(`support-ticket-status:${ticketId}`);
  const [savedVersion, setSavedVersion] = useState<number | null>(null);
  const conflict = mutation.error?.status === 409;
  const reload = detail.reload;
  useEffect(() => { if (mutation.error?.status === 409) reload(); }, [mutation.error, reload]);
  const ticket = detail.data;
  async function save(target: TicketTarget) {
    if (!ticket || !canManage || mutation.pending) return;
    setSavedVersion(null);
    await mutation.run((auth) => updateTicketStatus(auth, ticket.id, ticket.version, target), (updated) => {
      setSavedVersion(updated.version);
      reload();
    });
  }
  return (
    <>
      {conflict ? <p className="inline-notice" role="alert">{t("support.records.changed")}</p> : <InlineError error={mutation.error} />}
      <QueryState query={detail} />
      {ticket && !detail.loading && (
        <>
          <Panel title={ticket.subject} eyebrow={ticket.ticket_ref} actions={
            <button type="button" className="button button-ghost" disabled={mutation.pending} onClick={reload}>{t("common.refresh")}</button>
          }>
            <dl className="artifact-facts">
              <div><dt>{t("support.records.name")}</dt><dd>{ticket.customer_name} · {ticket.customer_ref}</dd></div>
              <div><dt>{t("support.records.status")}</dt><dd><TicketBadge status={ticket.status} /></dd></div>
              <div><dt>{t("common.created")}</dt><dd>{formatDateTime(ticket.created_at)}</dd></div>
            </dl>
            {savedVersion === ticket.version && <p className="inline-notice" role="status">{t("support.records.statusSaved")}</p>}
            {canManage && <TicketChangeForm key={ticket.version} ticket={ticket} pending={mutation.pending} onSave={save} />}
          </Panel>
          <CustomerTickets key={`${ticket.customer_id}:${ticket.version}`} customerId={ticket.customer_id} name={ticket.customer_name} customerRef={ticket.customer_ref} />
        </>
      )}
    </>
  );
}

export default function SupportRecords({ ticketId }: { ticketId?: string }) {
  const { t } = useI18n();
  const { connected, permissions, workspaceId, sessionId } = useFrontendSession();
  const canRead = permissions?.includes("support_records_read") === true;
  const canManage = permissions?.includes("support_records_manage") === true;
  return (
    <div className="page">
      <CatalogPageHeader title={t("support.records.title")} description={t("support.records.description")} actions={
        ticketId && canRead ? <Link className="button button-ghost" href="/support/records">{t("support.records.customerList")}</Link> : undefined
      } />
      {!connected ? <SessionRequired contextKey="session.context.support" />
        : permissions === null ? <LoadingState label={t("support.records.accessPending")} />
        : !canRead ? <EmptyState title={t("support.records.denied")} />
        : ticketId ? <TicketView key={`${workspaceId}:${sessionId}:${ticketId}`} ticketId={ticketId} canManage={canManage} />
        : <CustomerDirectory key={`${workspaceId}:${sessionId}`} canManage={canManage} />}
    </div>
  );
}
