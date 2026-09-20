"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";

import { EmptyState, ErrorState, LoadingState, Panel, SessionRequired } from "../../../components/states";
import StatusBadge from "../../../components/status-badge";
import { useFrontendSession } from "../../../components/session-provider";
import { useWorkspaceData, useWorkspaceMutation } from "../../../components/use-workspace-data";
import { errorHintKey, type AuthInput } from "../../../lib/api-client";
import {
  createModelProfile,
  createProviderCredential,
  listModelProfiles,
  listProviderCredentials,
  patchModelProfile,
  patchProviderCredential,
  rotateProviderCredentialSecret,
  type ModelProfile,
  type ProviderCredential,
} from "../../../lib/models";
import { useI18n } from "../../../i18n/provider";

/** Backend defaults for a new profile; semantics stay server-owned. */
const DEFAULT_PROFILE_FORM = {
  provider_credential_id: "",
  model: "",
  temperature: "0",
  max_tokens: "2048",
  timeout_seconds: "60",
  fallback_profile_id: "",
};

const DEFAULT_CREDENTIAL_FORM = { provider: "", name: "", secret: "", base_url: "" };

function numberValue(value: number | string): number {
  return typeof value === "number" ? value : Number(value);
}

export default function ModelSettingsPage() {
  const { t, formatDateTime } = useI18n();
  const { connected, sessionId, workspaceId } = useFrontendSession();

  const loadCredentials = useCallback((auth: AuthInput) => listProviderCredentials(auth), []);
  const loadProfiles = useCallback((auth: AuthInput) => listModelProfiles(auth), []);

  const credentials = useWorkspaceData<ProviderCredential[]>(loadCredentials, `credentials:${workspaceId}`);
  const profiles = useWorkspaceData<ModelProfile[]>(loadProfiles, `profiles:${workspaceId}`);

  const credentialMutation = useWorkspaceMutation(`credentials:${workspaceId}`);
  const profileMutation = useWorkspaceMutation(`profiles:${workspaceId}`);

  const [credentialForm, setCredentialForm] = useState(DEFAULT_CREDENTIAL_FORM);
  const [showCredentialForm, setShowCredentialForm] = useState(false);
  const [editingCredentialId, setEditingCredentialId] = useState<string | null>(null);
  const [editCredentialForm, setEditCredentialForm] = useState({ name: "", base_url: "" });
  const [rotatingCredentialId, setRotatingCredentialId] = useState<string | null>(null);
  const [rotateSecret, setRotateSecret] = useState("");
  const [notice, setNotice] = useState<string | null>(null);

  const [profileForm, setProfileForm] = useState(DEFAULT_PROFILE_FORM);
  const [showProfileForm, setShowProfileForm] = useState(false);
  const [editingProfileId, setEditingProfileId] = useState<string | null>(null);

  // Every form, selection and confirmation resets when the session
  // generation changes (workspace switch, sign-in, sign-out).
  useEffect(() => {
    setCredentialForm(DEFAULT_CREDENTIAL_FORM);
    setShowCredentialForm(false);
    setEditingCredentialId(null);
    setEditCredentialForm({ name: "", base_url: "" });
    setRotatingCredentialId(null);
    setRotateSecret("");
    setNotice(null);
    setProfileForm(DEFAULT_PROFILE_FORM);
    setShowProfileForm(false);
    setEditingProfileId(null);
  }, [sessionId]);

  if (!connected) {
    return (
      <div className="page">
        <header className="page-header">
          <p className="eyebrow">{t("settings.eyebrow")}</p>
          <h1>{t("settings.models.title")}</h1>
        </header>
        <SessionRequired contextKey="session.context.models" />
      </div>
    );
  }

  const credentialList = credentials.data ?? [];
  const profileList = profiles.data ?? [];

  function credentialName(credentialId: string): string {
    const credential = credentialList.find((item) => item.id === credentialId);
    return credential ? `${credential.name} (${credential.provider})` : t("common.unknown");
  }

  function profileName(profileId: string | null): string {
    if (!profileId) return t("common.none");
    const profile = profileList.find((item) => item.id === profileId);
    return profile ? profile.model : t("common.unknown");
  }

  async function submitCredential(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setNotice(null);
    const result = await credentialMutation.run((auth) =>
      createProviderCredential(auth, {
        provider: credentialForm.provider.trim(),
        name: credentialForm.name.trim(),
        secret: credentialForm.secret,
        base_url: credentialForm.base_url.trim() || null,
      }),
    );
    if (result) {
      // The secret leaves the form the moment it is accepted.
      setCredentialForm(DEFAULT_CREDENTIAL_FORM);
      setShowCredentialForm(false);
      setNotice(t("settings.models.credentialCreated"));
      credentials.reload();
    }
  }

  async function submitCredentialEdit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!editingCredentialId) return;
    setNotice(null);
    const credentialId = editingCredentialId;
    const result = await credentialMutation.run((auth) =>
      patchProviderCredential(auth, credentialId, {
        name: editCredentialForm.name.trim(),
        base_url: editCredentialForm.base_url.trim() || null,
      }),
    );
    if (result) {
      setEditingCredentialId(null);
      setNotice(t("settings.models.credentialUpdated"));
      credentials.reload();
    }
  }

  async function toggleCredential(credential: ProviderCredential) {
    setNotice(null);
    const result = await credentialMutation.run((auth) =>
      patchProviderCredential(auth, credential.id, { enabled: !credential.enabled }),
    );
    if (result) credentials.reload();
  }

  async function submitRotate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!rotatingCredentialId || !rotateSecret) return;
    setNotice(null);
    const credentialId = rotatingCredentialId;
    const secret = rotateSecret;
    const result = await credentialMutation.run((auth) =>
      rotateProviderCredentialSecret(auth, credentialId, secret),
    );
    // Clear the secret input whatever the outcome — it is never retained.
    setRotateSecret("");
    if (result) {
      setRotatingCredentialId(null);
      setNotice(t("settings.models.secretRotated"));
      credentials.reload();
    }
  }

  async function submitProfile(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setNotice(null);
    const body = {
      provider_credential_id: profileForm.provider_credential_id,
      model: profileForm.model.trim(),
      temperature: Number(profileForm.temperature),
      max_tokens: Number(profileForm.max_tokens),
      timeout_seconds: Number(profileForm.timeout_seconds),
      fallback_profile_id: profileForm.fallback_profile_id || null,
    };
    const profileId = editingProfileId;
    const result = await profileMutation.run((auth) =>
      profileId ? patchModelProfile(auth, profileId, body) : createModelProfile(auth, body),
    );
    if (result) {
      setProfileForm(DEFAULT_PROFILE_FORM);
      setShowProfileForm(false);
      setEditingProfileId(null);
      setNotice(profileId ? t("settings.models.profileUpdated") : t("settings.models.profileCreated"));
      profiles.reload();
    }
  }

  async function toggleProfile(profile: ModelProfile) {
    setNotice(null);
    const result = await profileMutation.run((auth) =>
      patchModelProfile(auth, profile.id, { enabled: !profile.enabled }),
    );
    if (result) profiles.reload();
  }

  function startProfileEdit(profile: ModelProfile) {
    setEditingProfileId(profile.id);
    setShowProfileForm(true);
    setProfileForm({
      provider_credential_id: profile.provider_credential_id,
      model: profile.model,
      temperature: String(numberValue(profile.temperature)),
      max_tokens: String(profile.max_tokens),
      timeout_seconds: String(numberValue(profile.timeout_seconds)),
      fallback_profile_id: profile.fallback_profile_id ?? "",
    });
  }

  return (
    <div className="page">
      <header className="page-header">
        <p className="eyebrow">{t("settings.eyebrow")}</p>
        <h1>{t("settings.models.title")}</h1>
        <p className="page-lede">{t("settings.models.lede")}</p>
      </header>

      {notice && <p className="inline-notice">{notice}</p>}

      {/* ---------- Provider credentials ---------- */}
      <Panel
        ariaLabel={t("settings.models.providers")}
        title={t("settings.models.providers")}
        eyebrow={t("settings.models.providersEyebrow")}
        actions={
          <button
            type="button"
            className="button button-primary"
            onClick={() => {
              setShowCredentialForm((value) => !value);
              setCredentialForm(DEFAULT_CREDENTIAL_FORM);
            }}
          >
            {t("settings.models.addProvider")}
          </button>
        }
      >
        <p className="state-hint">{t("settings.models.secretNote")}</p>

        {showCredentialForm && (
          <form className="eval-form" onSubmit={submitCredential} noValidate>
            <p className="eval-form-title">{t("settings.models.addProvider")}</p>
            <div className="form-grid">
              <label>
                {t("settings.models.provider")}
                <input
                  value={credentialForm.provider}
                  onChange={(event) =>
                    setCredentialForm((form) => ({ ...form, provider: event.target.value }))
                  }
                  placeholder={t("settings.models.providerPlaceholder")}
                  spellCheck={false}
                />
              </label>
              <label>
                {t("settings.models.name")}
                <input
                  value={credentialForm.name}
                  onChange={(event) => setCredentialForm((form) => ({ ...form, name: event.target.value }))}
                />
              </label>
              <label>
                {t("settings.models.baseUrl")}
                <input
                  value={credentialForm.base_url}
                  onChange={(event) =>
                    setCredentialForm((form) => ({ ...form, base_url: event.target.value }))
                  }
                  placeholder={t("settings.models.baseUrlPlaceholder")}
                  spellCheck={false}
                />
              </label>
              <label>
                {t("settings.models.apiKey")}
                <input
                  type="password"
                  autoComplete="off"
                  value={credentialForm.secret}
                  onChange={(event) => setCredentialForm((form) => ({ ...form, secret: event.target.value }))}
                />
              </label>
            </div>
            {credentialMutation.error && (
              <p className="session-error" role="alert">
                <code>{credentialMutation.error.code}</code>{" "}
                {credentialMutation.error.message || t("errors.requestFailed")}
              </p>
            )}
            <div className="form-actions">
              <button
                type="submit"
                className="button button-primary"
                disabled={
                  credentialMutation.pending ||
                  !credentialForm.provider.trim() ||
                  !credentialForm.name.trim() ||
                  !credentialForm.secret
                }
              >
                {t("settings.models.addProvider")}
              </button>
              <button
                type="button"
                className="button button-ghost"
                onClick={() => setShowCredentialForm(false)}
              >
                {t("session.cancel")}
              </button>
            </div>
          </form>
        )}

        {credentials.error && (
          <ErrorState
            code={credentials.error.code}
            message={credentials.error.message || t("errors.loadProviders")}
            hint={errorHintKey(credentials.error) ? t(errorHintKey(credentials.error)!) : undefined}
            onRetry={credentials.reload}
          />
        )}
        {credentials.loading && !credentials.error && <LoadingState />}
        {credentials.loaded && !credentials.error && credentialList.length === 0 && (
          <EmptyState title={t("settings.models.noProviders")} hint={t("settings.models.noProvidersHint")} />
        )}

        {credentialList.length > 0 && (
          <div className="data-table">
            <table>
              <thead>
                <tr>
                  <th scope="col">{t("settings.models.name")}</th>
                  <th scope="col">{t("settings.models.provider")}</th>
                  <th scope="col">{t("settings.models.baseUrl")}</th>
                  <th scope="col">{t("settings.models.secretState")}</th>
                  <th scope="col">{t("settings.models.status")}</th>
                  <th scope="col">{t("settings.models.created")}</th>
                  <th scope="col">{t("settings.models.actions")}</th>
                </tr>
              </thead>
              <tbody>
                {credentialList.map((credential) => (
                  <tr key={credential.id}>
                    <td data-label={t("settings.models.name")}>{credential.name}</td>
                    <td data-label={t("settings.models.provider")}>
                      <code>{credential.provider}</code>
                    </td>
                    <td data-label={t("settings.models.baseUrl")}>
                      {credential.base_url ? <code>{credential.base_url}</code> : t("common.none")}
                    </td>
                    <td data-label={t("settings.models.secretState")}>
                      {t("settings.models.secretConfigured")}
                    </td>
                    <td data-label={t("settings.models.status")}>
                      <StatusBadge
                        status={credential.enabled ? "ENABLED" : "DISABLED"}
                        tone={credential.enabled ? "success" : "neutral"}
                        label={credential.enabled ? t("common.enabled") : t("common.disabled")}
                      />
                    </td>
                    <td data-label={t("settings.models.created")}>
                      {credential.created_at ? formatDateTime(credential.created_at) : t("common.none")}
                    </td>
                    <td data-label={t("settings.models.actions")}>
                      <div className="approval-actions">
                        <button
                          type="button"
                          className="button button-ghost"
                          onClick={() => {
                            setEditingCredentialId(credential.id);
                            setEditCredentialForm({
                              name: credential.name,
                              base_url: credential.base_url ?? "",
                            });
                          }}
                        >
                          {t("common.edit")}
                        </button>
                        <button
                          type="button"
                          className="button button-ghost"
                          onClick={() => void toggleCredential(credential)}
                          disabled={credentialMutation.pending}
                        >
                          {credential.enabled ? t("common.disable") : t("common.enable")}
                        </button>
                        <button
                          type="button"
                          className="button button-ghost"
                          onClick={() => {
                            setRotatingCredentialId(credential.id);
                            setRotateSecret("");
                          }}
                        >
                          {t("settings.models.rotate")}
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {editingCredentialId && (
          <form className="eval-form" onSubmit={submitCredentialEdit} noValidate>
            <p className="eval-form-title">
              {t("settings.models.editProvider")} · {credentialName(editingCredentialId)}
            </p>
            <div className="form-grid">
              <label>
                {t("settings.models.name")}
                <input
                  value={editCredentialForm.name}
                  onChange={(event) =>
                    setEditCredentialForm((form) => ({ ...form, name: event.target.value }))
                  }
                />
              </label>
              <label>
                {t("settings.models.baseUrl")}
                <input
                  value={editCredentialForm.base_url}
                  onChange={(event) =>
                    setEditCredentialForm((form) => ({ ...form, base_url: event.target.value }))
                  }
                  spellCheck={false}
                />
              </label>
            </div>
            <div className="form-actions">
              <button type="submit" className="button button-primary" disabled={credentialMutation.pending}>
                {t("common.save")}
              </button>
              <button
                type="button"
                className="button button-ghost"
                onClick={() => setEditingCredentialId(null)}
              >
                {t("session.cancel")}
              </button>
            </div>
          </form>
        )}

        {rotatingCredentialId && (
          <form className="eval-form" onSubmit={submitRotate} noValidate>
            <p className="eval-form-title">
              {t("settings.models.rotate")} · {credentialName(rotatingCredentialId)}
            </p>
            <label>
              {t("settings.models.newApiKey")}
              <input
                type="password"
                autoComplete="off"
                value={rotateSecret}
                onChange={(event) => setRotateSecret(event.target.value)}
              />
            </label>
            <div className="form-actions">
              <button
                type="submit"
                className="button button-primary"
                disabled={credentialMutation.pending || !rotateSecret}
              >
                {t("settings.models.rotate")}
              </button>
              <button
                type="button"
                className="button button-ghost"
                onClick={() => {
                  setRotatingCredentialId(null);
                  setRotateSecret("");
                }}
              >
                {t("session.cancel")}
              </button>
            </div>
          </form>
        )}
      </Panel>

      {/* ---------- Model profiles ---------- */}
      <Panel
        ariaLabel={t("settings.models.profiles")}
        title={t("settings.models.profiles")}
        eyebrow={t("settings.models.profilesEyebrow")}
        actions={
          <button
            type="button"
            className="button button-primary"
            onClick={() => {
              setShowProfileForm((value) => !value);
              setEditingProfileId(null);
              setProfileForm(DEFAULT_PROFILE_FORM);
            }}
            disabled={credentialList.length === 0}
          >
            {t("settings.models.addProfile")}
          </button>
        }
      >
        {credentialList.length === 0 && !credentials.loading && (
          <p className="state-hint">{t("settings.models.profileNeedsProvider")}</p>
        )}

        {showProfileForm && (
          <form className="eval-form" onSubmit={submitProfile} noValidate>
            <p className="eval-form-title">
              {editingProfileId ? t("settings.models.editProfile") : t("settings.models.addProfile")}
            </p>
            <div className="form-grid">
              <label>
                {t("settings.models.providerCredential")}
                <select
                  value={profileForm.provider_credential_id}
                  onChange={(event) =>
                    setProfileForm((form) => ({ ...form, provider_credential_id: event.target.value }))
                  }
                >
                  <option value="">{t("settings.models.selectCredential")}</option>
                  {credentialList.map((credential) => (
                    <option value={credential.id} key={credential.id}>
                      {credential.name} ({credential.provider})
                    </option>
                  ))}
                </select>
              </label>
              <label>
                {t("settings.models.model")}
                <input
                  value={profileForm.model}
                  onChange={(event) => setProfileForm((form) => ({ ...form, model: event.target.value }))}
                  spellCheck={false}
                />
              </label>
              <label>
                {t("settings.models.temperature")}
                <input
                  type="number"
                  step="0.1"
                  min="0"
                  value={profileForm.temperature}
                  onChange={(event) =>
                    setProfileForm((form) => ({ ...form, temperature: event.target.value }))
                  }
                />
              </label>
              <label>
                {t("settings.models.maxTokens")}
                <input
                  type="number"
                  min="1"
                  value={profileForm.max_tokens}
                  onChange={(event) => setProfileForm((form) => ({ ...form, max_tokens: event.target.value }))}
                />
              </label>
              <label>
                {t("settings.models.timeout")}
                <input
                  type="number"
                  min="1"
                  step="0.5"
                  value={profileForm.timeout_seconds}
                  onChange={(event) =>
                    setProfileForm((form) => ({ ...form, timeout_seconds: event.target.value }))
                  }
                />
              </label>
              <label>
                {t("settings.models.fallback")}
                <select
                  value={profileForm.fallback_profile_id}
                  onChange={(event) =>
                    setProfileForm((form) => ({ ...form, fallback_profile_id: event.target.value }))
                  }
                >
                  <option value="">{t("settings.models.noFallback")}</option>
                  {profileList
                    .filter((profile) => profile.id !== editingProfileId)
                    .map((profile) => (
                      <option value={profile.id} key={profile.id}>
                        {profile.model}
                      </option>
                    ))}
                </select>
              </label>
            </div>
            {profileMutation.error && (
              <p className="session-error" role="alert">
                <code>{profileMutation.error.code}</code>{" "}
                {profileMutation.error.message || t("errors.requestFailed")}
              </p>
            )}
            <div className="form-actions">
              <button
                type="submit"
                className="button button-primary"
                disabled={
                  profileMutation.pending ||
                  !profileForm.provider_credential_id ||
                  !profileForm.model.trim() ||
                  !profileForm.max_tokens
                }
              >
                {editingProfileId ? t("common.save") : t("settings.models.addProfile")}
              </button>
              <button
                type="button"
                className="button button-ghost"
                onClick={() => {
                  setShowProfileForm(false);
                  setEditingProfileId(null);
                }}
              >
                {t("session.cancel")}
              </button>
            </div>
          </form>
        )}

        {profiles.error && (
          <ErrorState
            code={profiles.error.code}
            message={profiles.error.message || t("errors.loadProfiles")}
            hint={errorHintKey(profiles.error) ? t(errorHintKey(profiles.error)!) : undefined}
            onRetry={profiles.reload}
          />
        )}
        {profiles.loading && !profiles.error && <LoadingState />}
        {profiles.loaded && !profiles.error && profileList.length === 0 && (
          <EmptyState title={t("settings.models.noProfiles")} hint={t("settings.models.noProfilesHint")} />
        )}

        {profileList.length > 0 && (
          <div className="data-table">
            <table>
              <thead>
                <tr>
                  <th scope="col">{t("settings.models.model")}</th>
                  <th scope="col">{t("settings.models.providerCredential")}</th>
                  <th scope="col">{t("settings.models.temperature")}</th>
                  <th scope="col">{t("settings.models.maxTokens")}</th>
                  <th scope="col">{t("settings.models.timeout")}</th>
                  <th scope="col">{t("settings.models.fallback")}</th>
                  <th scope="col">{t("settings.models.capabilities")}</th>
                  <th scope="col">{t("settings.models.status")}</th>
                  <th scope="col">{t("settings.models.actions")}</th>
                </tr>
              </thead>
              <tbody>
                {profileList.map((profile) => (
                  <tr key={profile.id}>
                    <td data-label={t("settings.models.model")}>
                      <code>{profile.model}</code>
                    </td>
                    <td data-label={t("settings.models.providerCredential")}>
                      {credentialName(profile.provider_credential_id)}
                    </td>
                    <td data-label={t("settings.models.temperature")}>{numberValue(profile.temperature)}</td>
                    <td data-label={t("settings.models.maxTokens")}>{profile.max_tokens}</td>
                    <td data-label={t("settings.models.timeout")}>
                      {numberValue(profile.timeout_seconds)} s
                    </td>
                    <td data-label={t("settings.models.fallback")}>
                      {profileName(profile.fallback_profile_id)}
                    </td>
                    <td data-label={t("settings.models.capabilities")}>
                      <CapabilityList capabilities={profile.capabilities} />
                    </td>
                    <td data-label={t("settings.models.status")}>
                      <StatusBadge
                        status={profile.enabled ? "ENABLED" : "DISABLED"}
                        tone={profile.enabled ? "success" : "neutral"}
                        label={profile.enabled ? t("common.enabled") : t("common.disabled")}
                      />
                    </td>
                    <td data-label={t("settings.models.actions")}>
                      <div className="approval-actions">
                        <button
                          type="button"
                          className="button button-ghost"
                          onClick={() => startProfileEdit(profile)}
                        >
                          {t("common.edit")}
                        </button>
                        <button
                          type="button"
                          className="button button-ghost"
                          onClick={() => void toggleProfile(profile)}
                          disabled={profileMutation.pending}
                        >
                          {profile.enabled ? t("common.disable") : t("common.enable")}
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}

/** Read-only projection of the server-owned capability map. */
function CapabilityList({ capabilities }: { capabilities: Record<string, unknown> | null | undefined }) {
  const { t } = useI18n();
  const entries = Object.entries(capabilities ?? {});
  if (entries.length === 0) return <span className="muted">{t("common.none")}</span>;
  return (
    <span className="approval-chips">
      {entries.map(([key, value]) => (
        <code key={key}>
          {key}={String(value)}
        </code>
      ))}
    </span>
  );
}
