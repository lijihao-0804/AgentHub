"use client";

import { useState, type FormEvent } from "react";
import { useWorkspaceData, useWorkspaceMutation } from "@/hooks/use-workspace-data";
import { useI18n } from "@/i18n/provider";
import { ErrorState, InlineError } from "@/components/ui/states";
import {
  delegationLevels, getApprovalPreference, saveApprovalPreference,
  type ApprovalPreference, type DelegationLevel,
} from "@/lib/api/approvals";

function PreferenceForm({ preference, onSaved }: {
  preference: ApprovalPreference;
  onSaved: (saved: ApprovalPreference) => void;
}) {
  const { t } = useI18n();
  const mutation = useWorkspaceMutation("personal-approval-preference");
  const [level, setLevel] = useState(preference.effective_level);
  const [limit, setLimit] = useState(String(preference.max_auto_denials));
  const maxLevel = delegationLevels.indexOf(preference.identity_ceiling);
  const limitValue = level === "MANUAL" ? preference.max_auto_denials : Number(limit);
  const validLimit = (level === "MANUAL" || limit.trim() !== "") && Number.isInteger(limitValue) && limitValue >= 0 && limitValue <= 10;
  const changed = level !== preference.level || limitValue !== preference.max_auto_denials;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!validLimit || !changed || mutation.pending || delegationLevels.indexOf(level) > maxLevel) return;
    const saved = await mutation.run((auth) => saveApprovalPreference(auth, { level, max_auto_denials: limitValue }));
    if (saved) onSaved(saved);
  }

  return (
    <form className="eval-form" onSubmit={submit}>
      <p className="state-hint">{t("approvalPreference.ceiling", { level: t(`approvalPreference.${preference.identity_ceiling}`) })}</p>
      <label>
        {t("approvalPreference.level")}
        <select value={level} disabled={mutation.pending || maxLevel <= 0} onChange={(e) => setLevel(e.target.value as DelegationLevel)}>
          {delegationLevels.slice(0, maxLevel + 1).map((value) => <option key={value} value={value}>{t(`approvalPreference.${value}`)}</option>)}
        </select>
      </label>
      {level !== "MANUAL" && <>
        <label>
          {t("approvalPreference.denials")}
          <input type="number" min={0} max={10} step={1} required value={limit} disabled={mutation.pending} onChange={(e) => setLimit(e.target.value)} />
        </label>
        <p className="state-hint">{t("approvalPreference.limitHint")}</p>
      </>}
      <p className="state-hint">{t("approvalPreference.sensitive")}</p>
      <InlineError error={mutation.error} />
      <div className="form-actions">
        <button type="submit" className="button button-primary" disabled={!changed || !validLimit || mutation.pending}>
          {t("approvalPreference.save")}
        </button>
      </div>
    </form>
  );
}

export default function PersonalApprovalPreference() {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [saved, setSaved] = useState<ApprovalPreference | null>(null);
  const query = useWorkspaceData(getApprovalPreference, "personal-approval-preference", { enabled: open });
  const confirmed = saved && query.data && saved.level === query.data.level &&
    saved.max_auto_denials === query.data.max_auto_denials && !query.loading && !query.error;
  return (
    <details className="panel" onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary>{t("approvalPreference.title")}</summary>
      {open && query.loading && <p role="status">{t("common.loading")}</p>}
      {open && query.error && <ErrorState code={query.error.code} message={query.error.message} onRetry={query.reload} />}
      {open && query.data && !query.error && <PreferenceForm key={JSON.stringify(query.data)} preference={query.data}
        onSaved={(next) => { setSaved(next); query.reload(); }} />}
      {open && confirmed && <p role="status">{t("approvalPreference.saved")}</p>}
    </details>
  );
}
