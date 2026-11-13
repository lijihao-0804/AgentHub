"use client";

import Link from "next/link";
import { useEffect, useRef, useState, type FormEvent } from "react";

import { InlineError, Panel } from "@/components/ui/states";
import { CatalogPageHeader, CatalogStatStrip } from "@/components/catalog/catalog-layout";
import { useFrontendSession } from "@/components/providers/session-provider";
import { ApiError, toApiError } from "@/lib/api/client";
import { createOrganization, createWorkspace } from "@/lib/api/tenancy";
import Select from "@/components/ui/select";
import { useI18n } from "@/i18n/provider";

/**
 * Workspace settings entry point, and the minimal onboarding path for an
 * account that has no organization or workspace yet.
 */
export default function SettingsPage() {
  const { t, locale, formatCount } = useI18n();
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
  const copy = locale === "zh-CN"
    ? {
        eyebrow: "工作区设置",
        description: "确认当前组织与工作区上下文，管理模型和成员，并为新团队完成组织与工作区初始化。",
        organizations: "组织",
        workspaces: "工作区",
        activeContext: "当前上下文",
        none: "未选择",
        loading: "加载中",
        unavailable: "不可用",
      }
    : {
        eyebrow: "Workspace settings",
        description: "Confirm the current organization and workspace, manage models and members, and initialize a new team when needed.",
        organizations: "Organizations",
        workspaces: "Workspaces",
        activeContext: "Active context",
        none: "Not selected",
        loading: "Loading",
        unavailable: "Unavailable",
      };

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
    <div className="page settings-page">
      <CatalogPageHeader
        eyebrow={copy.eyebrow}
        title={t("settings.title")}
        description={copy.description}
        actions={
          <>
            <Link className="button button-ghost" href="/settings/models">{t("settings.models.title")}</Link>
            <Link className="button button-ghost" href="/settings/members">{t("settings.members.title")}</Link>
          </>
        }
      />

      <CatalogStatStrip
        items={[
          { label: copy.organizations, value: tenancyLoading ? copy.loading : tenancyError ? copy.unavailable : formatCount(organizations.length) },
          { label: copy.workspaces, value: tenancyLoading ? copy.loading : tenancyError ? copy.unavailable : formatCount(workspaces.length) },
          { label: copy.activeContext, value: tenancyLoading ? copy.loading : activeWorkspace?.name ?? copy.none, href: activeWorkspace ? "/dashboard" : undefined },
        ]}
      />

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
        <Link className="overview-card" href="/settings/members">
          <span className="overview-card-title">{t("settings.members.title")}</span>
          <span className="overview-card-description">{t("settings.members.lede")}</span>
          <span className="overview-card-cta" aria-hidden="true">{t("evaluation.overview.open")} →</span>
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
