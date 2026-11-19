"use client";

import { useFrontendSession } from "@/components/providers/session-provider";
import { useI18n } from "@/i18n/provider";
import type { MessageKey } from "@/i18n/messages";

export default function WorkspaceAccessNotice({ denied }: { denied: MessageKey }) {
  const { permissions, permissionsFailed, retryPermissions } = useFrontendSession();
  const { t } = useI18n();
  if (permissions !== null) return <p className="state-hint" role="note">{t(denied)}</p>;
  return (
    <div className="state-hint" role="status">
      {t(permissionsFailed ? "appThread.accessUnavailable" : "appThread.accessChecking")}
      {permissionsFailed && (
        <button className="button button-ghost" type="button" onClick={retryPermissions}>{t("common.retry")}</button>
      )}
    </div>
  );
}
