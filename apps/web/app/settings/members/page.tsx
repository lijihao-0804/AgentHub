"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { EmptyState, ErrorState, InlineError, LoadingState, Panel, SessionRequired } from "@/components/ui/states";
import { InlineConfirm } from "@/components/evaluation/hash-value";
import { useFrontendSession } from "@/components/providers/session-provider";
import { useI18n } from "@/i18n/provider";
import { ApiError, errorHintKey, toApiError } from "@/lib/api/client";
import {
  listOrganizationMembers,
  listWorkspaceMembers,
  removeOrganizationMember,
  removeWorkspaceMember,
  type Member,
} from "@/lib/api/tenancy";

export default function SettingsMembersPage() {
  const { t } = useI18n();
  const { connected, sessionId, workspaceId, organizationId, accessToken, userId, permissions } =
    useFrontendSession();
  const canManageOrg = permissions === null || permissions.includes("organization_management");
  const canManageWorkspace = permissions === null || permissions.includes("workspace_administration");

  const [orgMembers, setOrgMembers] = useState<Member[]>([]);
  const [wsMembers, setWsMembers] = useState<Member[]>([]);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [rowError, setRowError] = useState<{ scope: string; userId: string; error: ApiError } | null>(null);
  const [confirming, setConfirming] = useState<{ scope: string; userId: string } | null>(null);
  const [removing, setRemoving] = useState(false);
  const activeSessionRef = useRef(sessionId);
  activeSessionRef.current = sessionId;
  const generationRef = useRef(0);

  useEffect(() => {
    setOrgMembers([]);
    setWsMembers([]);
    setError(null);
    setLoaded(false);
    setConfirming(null);
    setRowError(null);
    setRemoving(false);
    generationRef.current += 1;
  }, [sessionId]);

  const refresh = useCallback(async () => {
    if (!connected) return;
    const requestSessionId = sessionId;
    const generation = ++generationRef.current;
    setLoading(true);
    setError(null);
    try {
      const [org, ws] = await Promise.all([
        organizationId && canManageOrg
          ? listOrganizationMembers(organizationId, accessToken).catch((caught) => {
              // Permissions may still be loading. An organization-only 403
              // must not hide the workspace roster the caller can read.
              if (toApiError(caught, "").status === 403) return [];
              throw caught;
            })
          : Promise.resolve([]),
        listWorkspaceMembers(workspaceId, accessToken),
      ]);
      if (activeSessionRef.current !== requestSessionId || generationRef.current !== generation) return;
      setOrgMembers(org);
      setWsMembers(ws);
      setLoaded(true);
    } catch (caught) {
      if (activeSessionRef.current === requestSessionId && generationRef.current === generation) setError(toApiError(caught, ""));
    } finally {
      if (activeSessionRef.current === requestSessionId && generationRef.current === generation) setLoading(false);
    }
  }, [connected, organizationId, workspaceId, accessToken, sessionId, canManageOrg]);

  useEffect(() => {
    if (connected) void refresh();
  }, [connected, refresh]);

  async function removeMember(scope: "org" | "workspace", targetUserId: string) {
    if (removing || targetUserId === userId) return;
    const requestSessionId = sessionId;
    setRemoving(true);
    setRowError(null);
    try {
      if (scope === "org") {
        await removeOrganizationMember(organizationId ?? "", targetUserId, accessToken);
      } else {
        await removeWorkspaceMember(workspaceId, targetUserId, accessToken);
      }
      if (activeSessionRef.current !== requestSessionId) return;
      setConfirming(null);
      await refresh();
    } catch (caught) {
      if (activeSessionRef.current === requestSessionId) setRowError({ scope, userId: targetUserId, error: toApiError(caught, "") });
    } finally {
      if (activeSessionRef.current === requestSessionId) setRemoving(false);
    }
  }

  function renderRows(scope: "org" | "workspace", members: Member[], canManage: boolean) {
    return members.map((member) => {
      const isSelf = member.user_id === userId;
      const rowFailed = rowError?.scope === scope && rowError?.userId === member.user_id;
      const confirmingRow = confirming?.scope === scope && confirming?.userId === member.user_id;
      return (
        <tr key={`${scope}-${member.user_id}`}>
          <td data-label={t("settings.members.email")}>
            <code>{member.email}</code>
          </td>
          <td data-label={t("settings.members.role")}>{member.role}</td>
          <td data-label={t("settings.models.actions")}>
            {canManage && !isSelf ? (
              confirmingRow ? (
                <InlineConfirm
                  title={t("settings.members.removeTitle")}
                  text={t("settings.members.removeText", { email: member.email })}
                  confirmLabel={t("settings.members.remove")}
                  pendingLabel={t("common.loading")}
                  cancelLabel={t("common.cancel")}
                  pending={removing}
                  onConfirm={() => void removeMember(scope, member.user_id)}
                  onCancel={() => setConfirming(null)}
                />
              ) : (
                <button
                  type="button"
                  className="button button-ghost"
                  onClick={() => setConfirming({ scope, userId: member.user_id })}
                >
                  {t("settings.members.remove")}
                </button>
              )
            ) : (
              <span className="state-hint">{isSelf ? t("settings.members.you") : "—"}</span>
            )}
            {rowFailed && <InlineError error={rowError!.error} fallback={t("errors.requestFailed")} />}
          </td>
        </tr>
      );
    });
  }

  if (!connected) {
    return (
      <div className="page">
        <header className="page-header">
          <p className="eyebrow">{t("settings.eyebrow")}</p>
          <h1>{t("settings.members.title")}</h1>
        </header>
        <SessionRequired contextKey="session.context.approvals" />
      </div>
    );
  }

  const hint = error ? errorHintKey(error) : null;

  return (
    <div className="page">
      <header className="page-header">
        <p className="eyebrow">{t("settings.eyebrow")}</p>
        <h1>{t("settings.members.title")}</h1>
        <p className="page-lede">{t("settings.members.lede")}</p>
      </header>

      {error && (
        <ErrorState
          code={error.code}
          message={error.message || t("errors.requestFailed")}
          hint={hint ? t(hint) : undefined}
          onRetry={() => void refresh()}
        />
      )}
      {loading && !error && !loaded && <LoadingState />}

      {loaded && !error && (
        <>
          <Panel ariaLabel={t("settings.members.workspaceTitle")} title={t("settings.members.workspaceTitle")}>
            {wsMembers.length === 0 ? (
              <EmptyState title={t("settings.members.empty")} />
            ) : (
              <div className="data-table">
                <table>
                  <thead>
                    <tr>
                      <th scope="col">{t("settings.members.email")}</th>
                      <th scope="col">{t("settings.members.role")}</th>
                      <th scope="col">{t("settings.models.actions")}</th>
                    </tr>
                  </thead>
                  <tbody>{renderRows("workspace", wsMembers, canManageWorkspace)}</tbody>
                </table>
              </div>
            )}
          </Panel>

          {canManageOrg && <Panel ariaLabel={t("settings.members.orgTitle")} title={t("settings.members.orgTitle")}>
            {orgMembers.length === 0 ? (
              <EmptyState title={t("settings.members.empty")} />
            ) : (
              <div className="data-table">
                <table>
                  <thead>
                    <tr>
                      <th scope="col">{t("settings.members.email")}</th>
                      <th scope="col">{t("settings.members.role")}</th>
                      <th scope="col">{t("settings.models.actions")}</th>
                    </tr>
                  </thead>
                  <tbody>{renderRows("org", orgMembers, canManageOrg)}</tbody>
                </table>
              </div>
            )}
          </Panel>}
        </>
      )}
    </div>
  );
}
