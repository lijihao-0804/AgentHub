"use client";

import Link from "next/link";
import { useEffect, useRef, useState, type FormEvent } from "react";

import { InlineError, Panel } from "@/components/ui/states";
import { useFrontendSession } from "@/components/providers/session-provider";
import { ApiError, toApiError } from "@/lib/api/client";
import { createOrganization, createWorkspace } from "@/lib/api/tenancy";
import { useI18n } from "@/i18n/provider";

/**
 * Workspace settings entry point, and the minimal onboarding path for an
 * account that has no organization or workspace yet.
 */
export default function SettingsPage() {
  const { t } = useI18n();
  const {
    accessToken,
    sessionId,
    organizations,
    workspaces,
    organizationId,
    workspaceId,
    tenancyError,
    tenancyLoading,
    reloadTenancy,
    setActiveWorkspace,
  } = useFrontendSession();

  const [orgName, setOrgName] = useState("");
  const [workspaceName, setWorkspaceName] = useState("");
  const [targetOrganizationId, setTargetOrganizationId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const activeSessionRef = useRef(sessionId);
  activeSessionRef.current = sessionId;

  // A session change invalidates every form on this page.
  useEffect(() => {
    setOrgName("");
    setWorkspaceName("");
    setTargetOrganizationId("");
    setBusy(false);
    setError(null);
    setNotice(null);
  }, [sessionId]);

  useEffect(() => {
    if (!targetOrganizationId && organizations.length > 0) {
      setTargetOrganizationId(organizationId || organizations[0].id);
    }
  }, [organizations, organizationId, targetOrganizationId]);

  const activeWorkspace = workspaces.find((workspace) => workspace.id === workspaceId) ?? null;
  const activeOrganization = organizations.find((organization) => organization.id === organizationId) ?? null;

  async function submitOrganization(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!orgName.trim()) return;
    const requestSessionId = sessionId;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const organization = await createOrganization(accessToken, { name: orgName.trim() });
      if (activeSessionRef.current !== requestSessionId) return;
      setOrgName("");
      setTargetOrganizationId(organization.id);
      setNotice(t("settings.organizationCreated"));
      await reloadTenancy();
    } catch (caught) {
      if (activeSessionRef.current === requestSessionId) setError(toApiError(caught, ""));
    } finally {
      if (activeSessionRef.current === requestSessionId) setBusy(false);
    }
  }

  async function submitWorkspace(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!workspaceName.trim() || !targetOrganizationId) return;
    const requestSessionId = sessionId;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const workspace = await createWorkspace(accessToken, {
        organization_id: targetOrganizationId,
        name: workspaceName.trim(),
      });
      if (activeSessionRef.current !== requestSessionId) return;
      setWorkspaceName("");
      setNotice(t("settings.workspaceCreated"));
      await reloadTenancy();
      if (activeSessionRef.current !== requestSessionId) return;
      setActiveWorkspace(workspace.id);
    } catch (caught) {
      if (activeSessionRef.current === requestSessionId) setError(toApiError(caught, ""));
    } finally {
      if (activeSessionRef.current === requestSessionId) setBusy(false);
    }
  }

  return (
    <div className="page">
      <header className="page-header">
        <p className="eyebrow">{t("settings.eyebrow")}</p>
        <h1>{t("settings.title")}</h1>
        <p className="page-lede">{t("settings.lede")}</p>
      </header>

      <Panel ariaLabel={t("settings.context")} title={t("settings.context")}>
        {tenancyLoading && <p className="state-hint">{t("common.loading")}</p>}
        <InlineError error={tenancyError} fallback={t("errors.loadWorkspaces")} />
        <dl className="key-values">
          <div className="key-value-row">
            <dt>{t("settings.organization")}</dt>
            <dd>{activeOrganization?.name ?? t("common.none")}</dd>
          </div>
          <div className="key-value-row">
            <dt>{t("settings.workspace")}</dt>
            <dd>{activeWorkspace?.name ?? t("common.none")}</dd>
          </div>
          <div className="key-value-row">
            <dt>{t("settings.workspaceId")}</dt>
            <dd>{workspaceId ? <code>{workspaceId}</code> : t("common.none")}</dd>
          </div>
        </dl>
      </Panel>

      <section className="overview-grid" aria-label={t("settings.areas")}>
        <Link className="overview-card" href="/settings/models">
          <span className="overview-card-title">{t("settings.models.title")}</span>
          <span className="overview-card-description">{t("settings.models.description")}</span>
          <span className="overview-card-cta" aria-hidden="true">
            {t("home.cardCta")} →
          </span>
        </Link>
      </section>

      <Panel ariaLabel={t("settings.tenancy")} title={t("settings.tenancy")} eyebrow={t("settings.eyebrow")}>
        {notice && <p className="inline-notice">{notice}</p>}
        <InlineError error={error} fallback={t("errors.requestFailed")} />

        <form className="eval-form" onSubmit={submitOrganization} noValidate>
          <p className="eval-form-title">{t("settings.createOrganization")}</p>
          <label>
            {t("settings.organizationName")}
            <input value={orgName} onChange={(event) => setOrgName(event.target.value)} maxLength={200} />
          </label>
          <div className="form-actions">
            <button type="submit" className="button button-primary" disabled={busy || !orgName.trim()}>
              {t("settings.createOrganization")}
            </button>
          </div>
        </form>

        <form className="eval-form" onSubmit={submitWorkspace} noValidate>
          <p className="eval-form-title">{t("settings.createWorkspace")}</p>
          <label>
            {t("settings.organization")}
            <select
              value={targetOrganizationId}
              onChange={(event) => setTargetOrganizationId(event.target.value)}
            >
              <option value="">{t("settings.selectOrganization")}</option>
              {organizations.map((organization) => (
                <option value={organization.id} key={organization.id}>
                  {organization.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            {t("settings.workspaceName")}
            <input
              value={workspaceName}
              onChange={(event) => setWorkspaceName(event.target.value)}
              maxLength={200}
            />
          </label>
          <div className="form-actions">
            <button
              type="submit"
              className="button button-primary"
              disabled={busy || !workspaceName.trim() || !targetOrganizationId}
            >
              {t("settings.createWorkspace")}
            </button>
          </div>
        </form>
      </Panel>
    </div>
  );
}
