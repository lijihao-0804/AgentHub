"use client";

import { useEffect, useRef } from "react";

import { useI18n } from "@/i18n/provider";
import { useFrontendSession } from "@/components/providers/session-provider";

/**
 * Topbar organization / workspace selector.
 *
 * Switching a workspace goes through `setActiveWorkspace`, which bumps
 * the frontend session generation so every workspace-scoped page drops
 * its data, forms and in-flight requests.
 */
export default function WorkspaceSelector() {
  const { t, errorText } = useI18n();
  const {
    organizations,
    workspaces,
    workspaceId,
    organizationId,
    tenancyLoading,
    tenancyError,
    setActiveWorkspace,
    panelOpen,
    openPanel,
    closePanel,
  } = useFrontendSession();
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!panelOpen) return;
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") closePanel();
    }
    function onPointerDown(event: MouseEvent) {
      if (!containerRef.current?.contains(event.target as Node)) closePanel();
    }
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("mousedown", onPointerDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("mousedown", onPointerDown);
    };
  }, [panelOpen, closePanel]);

  const activeWorkspace = workspaces.find((workspace) => workspace.id === workspaceId) ?? null;
  const activeOrganization = organizations.find((organization) => organization.id === organizationId) ?? null;

  const grouped = organizations.map((organization) => ({
    organization,
    items: workspaces.filter((workspace) => workspace.organization_id === organization.id),
  }));
  const ungrouped = workspaces.filter(
    (workspace) => !organizations.some((organization) => organization.id === workspace.organization_id),
  );

  /*
   * Organisation and workspace are rendered as two spans rather than one
   * joined string so a narrow topbar can drop the organisation and keep the
   * workspace. Truncating the joined string the other way round leaves
   * "Verification O…", which names nothing the reader is standing in.
   */
  const orgPrefix = activeWorkspace && activeOrganization ? activeOrganization.name : "";
  const workspaceLabel = activeWorkspace ? activeWorkspace.name : t("workspace.selectWorkspace");
  const fullLabel = activeWorkspace
    ? `${orgPrefix ? `${orgPrefix} / ` : ""}${activeWorkspace.name}`
    : workspaceLabel;

  return (
    <div className="ws-selector" ref={containerRef}>
      <button
        type="button"
        className="ws-selector-trigger"
        aria-expanded={panelOpen}
        aria-haspopup="menu"
        onClick={() => (panelOpen ? closePanel() : openPanel())}
        title={fullLabel}
      >
        <span className="session-dot" aria-hidden="true" />
        {orgPrefix && <span className="ws-org-prefix">{orgPrefix} /</span>}
        <span className="ws-path">{workspaceLabel}</span>
        <span aria-hidden="true">▾</span>
      </button>
      {panelOpen && (
        <div className="ws-menu" role="menu" aria-label={t("workspace.selectWorkspace")}>
          {tenancyLoading && <p className="ws-menu-empty">{t("common.loading")}</p>}
          {tenancyError && (
            <p className="ws-menu-empty" role="alert">
              {errorText(tenancyError.code, tenancyError.message || t("errors.loadWorkspaces"))}
            </p>
          )}
          {!tenancyLoading && !tenancyError && workspaces.length === 0 && (
            <p className="ws-menu-empty">{t("workspace.noWorkspaces")}</p>
          )}
          {grouped.map(({ organization, items }) =>
            items.length === 0 ? null : (
              <div key={organization.id}>
                <p className="ws-menu-group">{organization.name}</p>
                {items.map((workspace) => (
                  <button
                    type="button"
                    role="menuitem"
                    className="ws-menu-item"
                    aria-current={workspace.id === workspaceId}
                    key={workspace.id}
                    onClick={() => setActiveWorkspace(workspace.id)}
                  >
                    {workspace.name}
                  </button>
                ))}
              </div>
            ),
          )}
          {ungrouped.length > 0 && (
            <div>
              <p className="ws-menu-group">{t("workspace.otherWorkspaces")}</p>
              {ungrouped.map((workspace) => (
                <button
                  type="button"
                  role="menuitem"
                  className="ws-menu-item"
                  aria-current={workspace.id === workspaceId}
                  key={workspace.id}
                  onClick={() => setActiveWorkspace(workspace.id)}
                >
                  {workspace.name}
                </button>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
