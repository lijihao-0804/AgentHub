"use client";

import Link from "next/link";
import { useEffect, useRef, useState, type FormEvent } from "react";

import { InlineError, Panel } from "@/components/ui/states";
import { CatalogPageHeader } from "@/components/catalog/catalog-layout";
import { useFrontendSession } from "@/components/providers/session-provider";
import AppearanceSettings from "@/components/layout/appearance-settings";
import { ApiError, toApiError } from "@/lib/api/client";
import { canCreateWorkspaceIn, createOrganization, createWorkspace } from "@/lib/api/tenancy";
import Select from "@/components/ui/select";
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

  const targetOrganization = organizations.find((org) => org.id === targetOrganizationId);
  const canCreateWorkspace = canCreateWorkspaceIn(targetOrganization);

  useEffect(() => {
    if (!targetOrganizationId && organizations.length > 0) {
      const eligible = organizations.filter(canCreateWorkspaceIn);
      const initial = eligible.find((org) => org.id === organizationId) ?? eligible[0] ?? organizations[0];
      setTargetOrganizationId(initial.id);
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
    if (!canCreateWorkspace || !workspaceName.trim()) return;
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
    <div className="page settings-page">
      <CatalogPageHeader
        title={t("settings.title")}
        actions={
          <>
            <Link className="button button-ghost" href="/settings/models">{t("settings.models.title")}</Link>
            <Link className="button button-ghost" href="/settings/members">{t("settings.members.title")}</Link>
          </>
        }
      />

      <AppearanceSettings />

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
            <dd>{activeWorkspace ? <Link href="/dashboard">{activeWorkspace.name}</Link> : t("common.none")}</dd>
          </div>
          <div className="key-value-row">
            <dt>{t("settings.workspaceId")}</dt>
            <dd>{workspaceId ? <code>{workspaceId}</code> : t("common.none")}</dd>
          </div>
        </dl>
      </Panel>

      <Panel ariaLabel={t("settings.tenancy")} title={t("settings.tenancy")}>
        {notice && <p className="inline-notice" role="status">{notice}</p>}
        <InlineError error={error} fallback={t("errors.requestFailed")} />

        <form id="create-organization" className="eval-form" aria-label={t("settings.createOrganization")} onSubmit={submitOrganization} noValidate>
          <label>
            {t("settings.organizationName")}
            <input name="organization" autoComplete="organization" value={orgName} onChange={(event) => setOrgName(event.target.value)} maxLength={200} />
          </label>
          <div className="form-actions">
            <button type="submit" className="button button-primary" disabled={busy || !orgName.trim()}>
              {t("settings.createOrganization")}
            </button>
          </div>
        </form>

        <form id="create-workspace" className="eval-form" aria-label={t("settings.createWorkspace")} onSubmit={submitWorkspace} noValidate>
          <label>
            {t("settings.organization")}
            <Select
              value={targetOrganizationId}
              onChange={setTargetOrganizationId}
              placeholder={t("settings.selectOrganization")}
              options={organizations.map((organization) => ({ value: organization.id, label: organization.name }))}
            />
          </label>
          <label>
            {t("settings.workspaceName")}
            <input
              name="workspace"
              value={workspaceName}
              onChange={(event) => setWorkspaceName(event.target.value)}
              maxLength={200}
            />
          </label>
          {targetOrganization && !canCreateWorkspace && (
            <p className="state-hint">{t("workspace.createPermission")}</p>
          )}
          <div className="form-actions">
            <button
              type="submit"
              className="button button-primary"
              disabled={busy || !workspaceName.trim() || !canCreateWorkspace}
            >
              {t("settings.createWorkspace")}
            </button>
          </div>
        </form>
      </Panel>
    </div>
  );
}
