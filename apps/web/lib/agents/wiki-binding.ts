import type {
  AgentVersion,
  AgentWikiBinding,
  AgentWikiBindingInput,
  KnowledgeBindingMode,
} from "@/lib/api/agents";
import type { WikiCompilation } from "@/lib/api/wiki";

export const WIKI_BINDING_SCHEMA_VERSION = 1 as const;

export type WikiBindingDraftMode = "NONE" | KnowledgeBindingMode;

export type WikiBindingDraft = {
  mode: WikiBindingDraftMode;
  knowledge_base_id: string;
  wiki_publication_id: string;
};

export type WikiBindingValidationIssue = {
  field: "knowledge_base_id" | "wiki_publication_id";
  code: "KNOWLEDGE_BASE_REQUIRED" | "PUBLICATION_REQUIRED";
};

export type WikiBindingValidation = {
  valid: boolean;
  request: AgentWikiBindingInput | null;
  issues: WikiBindingValidationIssue[];
};

export type WikiPublicationOption = {
  id: string;
  contentHash: string;
  publishedAt: string | null;
  preservedFromCurrentBinding: boolean;
};

export type FrozenWikiBinding = {
  schema_version: typeof WIKI_BINDING_SCHEMA_VERSION;
  binding_mode: KnowledgeBindingMode;
  knowledge_base_id: string;
  wiki_publication_id: string;
  publication_hash: string;
};

export type FrozenWikiBindingVersion = {
  version: AgentVersion;
  binding: FrozenWikiBinding;
};

export type WikiBindingReadbackDecision =
  | { status: "IDLE" }
  | { status: "WAITING"; sawLoading: boolean }
  | { status: "ERROR"; error: { code: string } }
  | { status: "SUCCESS"; binding: AgentWikiBinding | null };

/**
 * A one-way revision marker lets resource keys distinguish access-token
 * generations without copying the credential into state, URLs or request
 * labels. Authorization still uses the real token supplied by the session.
 */
export function credentialRevision(accessToken: string): string {
  let hash = 0x811c9dc5;
  for (let index = 0; index < accessToken.length; index += 1) {
    hash ^= accessToken.charCodeAt(index);
    hash = Math.imul(hash, 0x01000193);
  }
  return `${(hash >>> 0).toString(36)}-${accessToken.length}`;
}

export function defaultWikiBindingDraft(): WikiBindingDraft {
  return {
    mode: "NONE",
    knowledge_base_id: "",
    wiki_publication_id: "",
  };
}

export function wikiBindingDraftFromBinding(
  binding: AgentWikiBinding | null,
): WikiBindingDraft {
  if (!binding) return defaultWikiBindingDraft();
  return {
    mode: binding.binding_mode,
    knowledge_base_id: binding.knowledge_base_id,
    wiki_publication_id: binding.wiki_publication_id ?? "",
  };
}

export function switchWikiBindingMode(
  draft: WikiBindingDraft,
  mode: WikiBindingDraftMode,
): WikiBindingDraft {
  return {
    ...draft,
    mode,
    wiki_publication_id: mode === "PINNED" ? draft.wiki_publication_id : "",
  };
}

export function changeWikiBindingKnowledgeBase(
  draft: WikiBindingDraft,
  knowledgeBaseId: string,
): WikiBindingDraft {
  return {
    ...draft,
    knowledge_base_id: knowledgeBaseId,
    wiki_publication_id: "",
  };
}

/**
 * NONE is represented by a null request. Every other mode names exactly one
 * knowledge base; PINNED also names a published compilation.
 */
export function validateWikiBindingDraft(
  draft: WikiBindingDraft,
): WikiBindingValidation {
  if (draft.mode === "NONE") {
    return { valid: true, request: null, issues: [] };
  }

  const issues: WikiBindingValidationIssue[] = [];
  const knowledgeBaseId = draft.knowledge_base_id.trim();
  const publicationId = draft.wiki_publication_id.trim();
  if (!knowledgeBaseId) {
    issues.push({
      field: "knowledge_base_id",
      code: "KNOWLEDGE_BASE_REQUIRED",
    });
  }
  if (draft.mode === "PINNED" && !publicationId) {
    issues.push({
      field: "wiki_publication_id",
      code: "PUBLICATION_REQUIRED",
    });
  }
  if (issues.length > 0) {
    return { valid: false, request: null, issues };
  }

  if (draft.mode === "LATEST") {
    return {
      valid: true,
      request: {
        knowledge_base_id: knowledgeBaseId,
        binding_mode: "LATEST",
        wiki_publication_id: null,
      },
      issues: [],
    };
  }
  return {
    valid: true,
    request: {
      knowledge_base_id: knowledgeBaseId,
      binding_mode: "PINNED",
      wiki_publication_id: publicationId,
    },
    issues: [],
  };
}

/**
 * The compilations endpoint already returns newest first and caps the list at
 * 100. Keep that order and retain an older currently-bound publication when it
 * falls outside the first page, rather than silently replacing the selection.
 */
export function publishedWikiOptions(
  compilations: WikiCompilation[],
  current: {
    knowledgeBaseId: string;
    binding: AgentWikiBinding | null;
  },
): WikiPublicationOption[] {
  const options: WikiPublicationOption[] = [];
  const seen = new Set<string>();
  for (const compilation of compilations) {
    if (
      compilation.status !== "PUBLISHED" ||
      typeof compilation.content_hash !== "string" ||
      !compilation.content_hash ||
      seen.has(compilation.id)
    ) {
      continue;
    }
    seen.add(compilation.id);
    options.push({
      id: compilation.id,
      contentHash: compilation.content_hash,
      publishedAt: compilation.published_at,
      preservedFromCurrentBinding: false,
    });
  }

  const binding = current.binding;
  if (
    binding?.binding_mode === "PINNED" &&
    binding.knowledge_base_id === current.knowledgeBaseId &&
    binding.wiki_publication_id &&
    !seen.has(binding.wiki_publication_id)
  ) {
    options.push({
      id: binding.wiki_publication_id,
      contentHash: binding.publication_hash ?? "",
      publishedAt: null,
      preservedFromCurrentBinding: true,
    });
  }
  return options;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function nonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim() !== "";
}

export function extractFrozenWikiBinding(
  resolvedSpec: Record<string, unknown>,
): FrozenWikiBinding | null {
  const projection = resolvedSpec.wiki_binding;
  if (!isRecord(projection)) return null;
  const schemaVersion = projection.schema_version;
  const bindingMode = projection.binding_mode;
  const knowledgeBaseId = projection.knowledge_base_id;
  const wikiPublicationId = projection.wiki_publication_id;
  const publicationHash = projection.publication_hash;
  if (
    schemaVersion !== WIKI_BINDING_SCHEMA_VERSION ||
    (bindingMode !== "PINNED" && bindingMode !== "LATEST") ||
    !nonEmptyString(knowledgeBaseId) ||
    !nonEmptyString(wikiPublicationId) ||
    !nonEmptyString(publicationHash)
  ) {
    return null;
  }
  return {
    schema_version: WIKI_BINDING_SCHEMA_VERSION,
    binding_mode: bindingMode,
    knowledge_base_id: knowledgeBaseId,
    wiki_publication_id: wikiPublicationId,
    publication_hash: publicationHash,
  };
}

export function latestFrozenWikiBinding(
  versions: AgentVersion[],
): FrozenWikiBindingVersion | null {
  let latest: FrozenWikiBindingVersion | null = null;
  for (const version of versions) {
    const binding = extractFrozenWikiBinding(version.resolved_spec);
    if (!binding) continue;
    if (!latest || version.version_number > latest.version.version_number) {
      latest = { version, binding };
    }
  }
  return latest;
}

/**
 * A successful PUT may return null when it clears the binding, so completion
 * is judged from the follow-up GET state instead of truthiness of the write
 * result.
 */
export function evaluateWikiBindingReadback({
  pending,
  sawLoading,
  loading,
  loaded,
  error,
  data,
}: {
  pending: boolean;
  sawLoading: boolean;
  loading: boolean;
  loaded: boolean;
  error: { code: string } | null;
  data: AgentWikiBinding | null;
}): WikiBindingReadbackDecision {
  if (!pending) return { status: "IDLE" };
  if (loading) return { status: "WAITING", sawLoading: true };
  if (!sawLoading) return { status: "WAITING", sawLoading: false };
  if (error) return { status: "ERROR", error };
  if (loaded) return { status: "SUCCESS", binding: data };
  return { status: "WAITING", sawLoading: true };
}
