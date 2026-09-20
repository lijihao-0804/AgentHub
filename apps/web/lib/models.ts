import { apiRequest, type AuthInput } from "./api-client";
import { listItems } from "./tenancy";

/**
 * Model settings client: provider credentials and model profiles.
 *
 * Secrets are write-only by contract — no response on this surface ever
 * carries `secret`, `secret_ciphertext` or `secret_version`, so the UI
 * never renders or masks one.
 */

function workspaceBase(workspaceId: string): string {
  return `/api/v1/workspaces/${encodeURIComponent(workspaceId)}`;
}

// ---------- Provider credentials ----------

export type ProviderCredential = {
  id: string;
  workspace_id: string;
  provider: string;
  name: string;
  base_url: string | null;
  enabled: boolean;
  created_at: string;
};

export type ProviderCredentialCreateInput = {
  provider: string;
  name: string;
  secret: string;
  base_url?: string | null;
};

export type ProviderCredentialPatchInput = {
  name?: string;
  base_url?: string | null;
  enabled?: boolean;
};

export async function listProviderCredentials(input: AuthInput): Promise<ProviderCredential[]> {
  const payload = await apiRequest<unknown>(
    `${workspaceBase(input.workspaceId)}/provider-credentials`,
    input.accessToken,
  );
  return listItems<ProviderCredential>(payload);
}

export function getProviderCredential(input: AuthInput, credentialId: string): Promise<ProviderCredential> {
  return apiRequest<ProviderCredential>(
    `${workspaceBase(input.workspaceId)}/provider-credentials/${encodeURIComponent(credentialId)}`,
    input.accessToken,
  );
}

export function createProviderCredential(
  input: AuthInput,
  body: ProviderCredentialCreateInput,
): Promise<ProviderCredential> {
  return apiRequest<ProviderCredential>(
    `${workspaceBase(input.workspaceId)}/provider-credentials`,
    input.accessToken,
    { method: "POST", body },
  );
}

export function patchProviderCredential(
  input: AuthInput,
  credentialId: string,
  body: ProviderCredentialPatchInput,
): Promise<ProviderCredential> {
  return apiRequest<ProviderCredential>(
    `${workspaceBase(input.workspaceId)}/provider-credentials/${encodeURIComponent(credentialId)}`,
    input.accessToken,
    { method: "PATCH", body },
  );
}

export function rotateProviderCredentialSecret(
  input: AuthInput,
  credentialId: string,
  secret: string,
): Promise<ProviderCredential> {
  return apiRequest<ProviderCredential>(
    `${workspaceBase(input.workspaceId)}/provider-credentials/${encodeURIComponent(credentialId)}/rotate-secret`,
    input.accessToken,
    { method: "POST", body: { secret } },
  );
}

// ---------- Model profiles ----------

/** Server-owned capability projection; the UI presents it read-only. */
export type ModelCapabilities = {
  tool_calling?: boolean;
  streaming?: boolean;
  structured_output?: boolean;
  vision?: boolean;
  max_context_tokens?: number;
  [key: string]: unknown;
};

export type ModelProfile = {
  id: string;
  workspace_id: string;
  provider_credential_id: string;
  model: string;
  temperature: number | string;
  max_tokens: number;
  timeout_seconds: number | string;
  fallback_profile_id: string | null;
  capabilities: ModelCapabilities;
  enabled: boolean;
  created_at?: string | null;
};

export type ModelProfileCreateInput = {
  provider_credential_id: string;
  model: string;
  temperature: number;
  max_tokens: number;
  timeout_seconds: number;
  fallback_profile_id?: string | null;
  capabilities?: ModelCapabilities;
  enabled?: boolean;
};

export type ModelProfilePatchInput = Partial<ModelProfileCreateInput>;

export async function listModelProfiles(input: AuthInput): Promise<ModelProfile[]> {
  const payload = await apiRequest<unknown>(
    `${workspaceBase(input.workspaceId)}/model-profiles`,
    input.accessToken,
  );
  return listItems<ModelProfile>(payload);
}

export function getModelProfile(input: AuthInput, profileId: string): Promise<ModelProfile> {
  return apiRequest<ModelProfile>(
    `${workspaceBase(input.workspaceId)}/model-profiles/${encodeURIComponent(profileId)}`,
    input.accessToken,
  );
}

export function createModelProfile(input: AuthInput, body: ModelProfileCreateInput): Promise<ModelProfile> {
  return apiRequest<ModelProfile>(`${workspaceBase(input.workspaceId)}/model-profiles`, input.accessToken, {
    method: "POST",
    body,
  });
}

export function patchModelProfile(
  input: AuthInput,
  profileId: string,
  body: ModelProfilePatchInput,
): Promise<ModelProfile> {
  return apiRequest<ModelProfile>(
    `${workspaceBase(input.workspaceId)}/model-profiles/${encodeURIComponent(profileId)}`,
    input.accessToken,
    { method: "PATCH", body },
  );
}

/** Readable label for a profile in selectors — never a bare UUID. */
export function modelProfileLabel(profile: ModelProfile | undefined | null): string {
  if (!profile) return "";
  return profile.model;
}
