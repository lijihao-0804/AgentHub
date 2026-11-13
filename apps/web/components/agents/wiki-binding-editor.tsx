"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { useFrontendSession } from "@/components/providers/session-provider";
import Select from "@/components/ui/select";
import { ErrorState, InlineError, LoadingState } from "@/components/ui/states";
import { useI18n } from "@/i18n/provider";
import { useWorkspaceData, useWorkspaceMutation } from "@/hooks/use-workspace-data";
import {
  getAgentWikiBinding,
  replaceAgentWikiBinding,
  type AgentVersion,
  type AgentWikiBinding,
} from "@/lib/api/agents";
import { errorHintKey, type AuthInput } from "@/lib/api/client";
import type { KnowledgeBase } from "@/lib/api/knowledge";
import { listCompilations, type WikiCompilation } from "@/lib/api/wiki";
import {
  changeWikiBindingKnowledgeBase,
  credentialRevision,
  defaultWikiBindingDraft,
  evaluateWikiBindingReadback,
  latestFrozenWikiBinding,
  publishedWikiOptions,
  switchWikiBindingMode,
  validateWikiBindingDraft,
  wikiBindingDraftFromBinding,
  type WikiBindingDraftMode,
  type WikiBindingValidationIssue,
} from "@/lib/agents/wiki-binding";

type Copy = {
  title: string;
  intro: string;
  mode: string;
  modeNone: string;
  modePinned: string;
  modeLatest: string;
  knowledgeBase: string;
  selectKnowledgeBase: string;
  noKnowledgeBases: string;
  noKnowledgeBasesHint: string;
  publishedVersion: string;
  selectPublishedVersion: string;
  publishedVersionsHint: string;
  publishedHash: string;
  publishedAt: string;
  hashUnavailable: string;
  currentOptionSuffix: string;
  onlyPublished: string;
  preservedVersion: string;
  latestHint: string;
  noneHint: string;
  pinnedHint: string;
  currentBinding: string;
  currentNone: string;
  currentLatest: string;
  currentPinned: string;
  save: string;
  clear: string;
  saved: string;
  readback: string;
  readonly: string;
  loadBindingError: string;
  loadVersionsError: string;
  kbRequired: string;
  publicationRequired: string;
  frozenTitle: string;
  frozenHint: string;
  frozenVersion: string;
  frozenMode: string;
  frozenKnowledgeBase: string;
  frozenPublication: string;
  frozenHash: string;
  noFrozen: string;
  publishBoundary: string;
};

const COPY: Record<"zh-CN" | "en-US", Copy> = {
  "zh-CN": {
    title: "Wiki 绑定",
    intro:
      "为 Agent 草稿选择 Wiki 知识库。LATEST 只会在 Agent 发布时解析一次；已有版本始终读取发布时冻结的 publication。",
    mode: "绑定模式",
    modeNone: "不绑定",
    modePinned: "固定发布版本",
    modeLatest: "发布时采用最新版本",
    knowledgeBase: "Wiki 知识库",
    selectKnowledgeBase: "选择知识库",
    noKnowledgeBases: "当前 Workspace 没有知识库",
    noKnowledgeBasesHint: "先创建知识库并发布 Wiki，然后才能为 Agent 绑定 Wiki。",
    publishedVersion: "已发布 Wiki 版本",
    selectPublishedVersion: "选择一个已发布版本",
    publishedVersionsHint: "列表只包含 PUBLISHED 状态的编译版本，按服务端返回顺序展示。",
    publishedHash: "发布内容 hash",
    publishedAt: "发布时间",
    hashUnavailable: "hash 不可用",
    currentOptionSuffix: "当前绑定",
    onlyPublished: "未发布的编译任务不会出现在这里，也不能被固定绑定。",
    preservedVersion: "当前绑定不在最近 100 条列表中，已保留该版本，避免误覆盖。",
    latestHint:
      "保存后会绑定该知识库的 LATEST 意图；下次发布 Agent 时才会解析具体 publication。此页面不会自动发布 Agent。",
    noneHint: "保存后会清除草稿 Wiki 绑定；已经发布的 AgentVersion 不受影响。",
    pinnedHint: "保存后固定到所选 publication；下次发布 Agent 时会再次校验其 hash 与来源。",
    currentBinding: "服务端当前草稿绑定",
    currentNone: "未绑定",
    currentLatest: "LATEST，等待发布时解析",
    currentPinned: "PINNED",
    save: "保存 Wiki 绑定",
    clear: "清除 Wiki 绑定",
    saved: "服务端回读成功，草稿 Wiki 绑定已更新。",
    readback: "保存请求已成功，正在从服务端重新读取草稿绑定…",
    readonly: "当前 Workspace 权限不允许修改 Agent；可以查看草稿绑定和冻结版本。",
    loadBindingError: "无法读取 Agent 的 Wiki 绑定。",
    loadVersionsError: "无法读取该知识库的 Wiki 发布版本。",
    kbRequired: "请选择 Wiki 知识库。",
    publicationRequired: "PINNED 模式必须选择一个已发布 Wiki 版本。",
    frozenTitle: "已发布版本中的冻结结果",
    frozenHint:
      "以下信息来自 AgentVersion.resolved_spec，不读取当前 Wiki 最新版本。历史 LATEST 也只展示当时冻结的 publication。",
    frozenVersion: "Agent 版本",
    frozenMode: "发布时模式",
    frozenKnowledgeBase: "冻结知识库",
    frozenPublication: "冻结 publication",
    frozenHash: "冻结 hash",
    noFrozen: "当前版本列表中没有包含 Wiki 绑定的冻结投影。",
    publishBoundary:
      "保存 Wiki 绑定只更新 Agent 草稿，不会自动发布 Agent 或 Wiki。正式使用仍需单独发布 Agent。",
  },
  "en-US": {
    title: "Wiki binding",
    intro:
      "Choose one Wiki knowledge base for the Agent draft. LATEST resolves once when the Agent is published; existing versions always read the publication frozen into them.",
    mode: "Binding mode",
    modeNone: "Not bound",
    modePinned: "Pin a publication",
    modeLatest: "Use the latest at publish time",
    knowledgeBase: "Wiki knowledge base",
    selectKnowledgeBase: "Select a knowledge base",
    noKnowledgeBases: "This Workspace has no knowledge bases",
    noKnowledgeBasesHint:
      "Create a knowledge base and publish a Wiki before binding one to an Agent.",
    publishedVersion: "Published Wiki version",
    selectPublishedVersion: "Select a published version",
    publishedVersionsHint:
      "Only PUBLISHED compilations appear here, in the order returned by the server.",
    publishedHash: "Published content hash",
    publishedAt: "Published at",
    hashUnavailable: "hash unavailable",
    currentOptionSuffix: "current binding",
    onlyPublished:
      "Unpublished compilation jobs are not selectable and cannot be pinned.",
    preservedVersion:
      "The current binding is outside the latest 100 results. It remains selectable so it is not replaced by accident.",
    latestHint:
      "Saving stores a LATEST intent for this knowledge base. The concrete publication is resolved only on the next Agent publication. This page never publishes the Agent automatically.",
    noneHint:
      "Saving clears the draft Wiki binding. Existing AgentVersions keep their frozen publications.",
    pinnedHint:
      "Saving pins the selected publication. The next Agent publication re-validates its hash and sources.",
    currentBinding: "Current server draft binding",
    currentNone: "Not bound",
    currentLatest: "LATEST, resolved at publication",
    currentPinned: "PINNED",
    save: "Save Wiki binding",
    clear: "Clear Wiki binding",
    saved: "Server readback succeeded and the draft Wiki binding was updated.",
    readback:
      "The write succeeded. Reading the draft binding back from the server…",
    readonly:
      "This Workspace role cannot edit the Agent. The draft binding and frozen versions remain visible.",
    loadBindingError: "The Agent Wiki binding could not be loaded.",
    loadVersionsError: "Wiki publications for this knowledge base could not be loaded.",
    kbRequired: "Select a Wiki knowledge base.",
    publicationRequired: "PINNED mode requires a published Wiki version.",
    frozenTitle: "Frozen result in a published version",
    frozenHint:
      "These values come from AgentVersion.resolved_spec, never from the current Wiki latest. Historical LATEST bindings show only the publication frozen at that time.",
    frozenVersion: "Agent version",
    frozenMode: "Published mode",
    frozenKnowledgeBase: "Frozen knowledge base",
    frozenPublication: "Frozen publication",
    frozenHash: "Frozen hash",
    noFrozen:
      "No version in the loaded list contains a frozen Wiki binding projection.",
    publishBoundary:
      "Saving a Wiki binding updates only the Agent draft. It does not publish the Agent or the Wiki; using it still requires a separate Agent publication.",
  },
};

function modeFromValue(value: string): WikiBindingDraftMode {
  if (value === "PINNED" || value === "LATEST") return value;
  return "NONE";
}

function shortHash(value: string): string {
  return value.length > 16 ? `${value.slice(0, 16)}…` : value;
}

function shortIdentifier(value: string): string {
  return value.length > 20 ? `${value.slice(0, 12)}…${value.slice(-6)}` : value;
}

function issueCopy(copy: Copy, issue: WikiBindingValidationIssue): string {
  return issue.code === "KNOWLEDGE_BASE_REQUIRED"
    ? copy.kbRequired
    : copy.publicationRequired;
}

export default function WikiBindingEditor({
  agentId,
  bases,
  versions,
  canEdit,
  onChanged,
}: {
  agentId: string;
  bases: KnowledgeBase[];
  versions: AgentVersion[];
  canEdit: boolean;
  onChanged?: () => void;
}) {
  const { t, locale, formatDateTime } = useI18n();
  const copy = locale === "zh-CN" ? COPY["zh-CN"] : COPY["en-US"];
  const { workspaceId, sessionId, accessToken } = useFrontendSession();
  const permissionKey = canEdit ? "edit" : "read";
  const tokenKey = credentialRevision(accessToken);
  const scopeKey =
    `${workspaceId}:${agentId}:${sessionId}:${tokenKey}:${permissionKey}`;

  const loadBinding = useCallback(
    (auth: AuthInput) => getAgentWikiBinding(auth, agentId),
    [agentId],
  );
  const binding = useWorkspaceData<AgentWikiBinding | null>(
    loadBinding,
    `agent-wiki-binding:${scopeKey}`,
  );
  const mutation = useWorkspaceMutation(`agent-wiki-binding:${scopeKey}`);

  const [draft, setDraft] = useState(() =>
    binding.loaded
      ? wikiBindingDraftFromBinding(binding.data)
      : defaultWikiBindingDraft(),
  );
  const [attempted, setAttempted] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [readbackPending, setReadbackPending] = useState(false);
  const readbackSawLoadingRef = useRef(false);

  const loadCompilations = useCallback(
    (auth: AuthInput) => {
      if (!draft.knowledge_base_id) return Promise.resolve<WikiCompilation[]>([]);
      return listCompilations(auth, draft.knowledge_base_id);
    },
    [draft.knowledge_base_id],
  );
  const compilations = useWorkspaceData<WikiCompilation[]>(
    loadCompilations,
    `agent-wiki-compilations:${scopeKey}:${draft.knowledge_base_id || "NONE"}`,
    {
      enabled:
        draft.mode === "PINNED" && draft.knowledge_base_id !== "",
    },
  );

  // Workspace, Agent, access-token generation and edit permission all replace
  // the local draft and invalidate any older query or mutation result.
  useEffect(() => {
    setDraft(defaultWikiBindingDraft());
    setAttempted(false);
    setNotice(null);
    setReadbackPending(false);
    readbackSawLoadingRef.current = false;
  }, [scopeKey]);

  useEffect(() => {
    if (!binding.loaded || readbackPending) return;
    setDraft(wikiBindingDraftFromBinding(binding.data));
    setAttempted(false);
  }, [binding.data, binding.loaded, readbackPending]);

  useEffect(() => {
    if (!readbackPending) return;
    if (binding.loading) {
      readbackSawLoadingRef.current = true;
      return;
    }
    const decision = evaluateWikiBindingReadback({
      pending: readbackPending,
      sawLoading: readbackSawLoadingRef.current,
      loading: binding.loading,
      loaded: binding.loaded,
      error: binding.error,
      data: binding.data,
    });
    if (decision.status === "SUCCESS") {
      setDraft(wikiBindingDraftFromBinding(decision.binding));
      setAttempted(false);
      setNotice(copy.saved);
      setReadbackPending(false);
      readbackSawLoadingRef.current = false;
    } else if (decision.status === "ERROR") {
      setNotice(null);
      setReadbackPending(false);
      readbackSawLoadingRef.current = false;
    }
  }, [
    binding.data,
    binding.error,
    binding.loaded,
    binding.loading,
    copy.saved,
    readbackPending,
  ]);

  const currentBinding = binding.loaded ? binding.data : null;
  const validation = validateWikiBindingDraft(draft);
  const publicationOptions = publishedWikiOptions(compilations.data ?? [], {
    knowledgeBaseId: draft.knowledge_base_id,
    binding: currentBinding,
  });
  const selectedPublication = publicationOptions.find(
    (option) => option.id === draft.wiki_publication_id,
  );
  const frozen = latestFrozenWikiBinding(versions);
  const readOnly = !canEdit;
  const controlsDisabled =
    readOnly || mutation.pending || readbackPending || binding.loading;
  const showValidation = attempted || draft.mode !== "NONE";

  function updateDraft(next: ReturnType<typeof defaultWikiBindingDraft>) {
    setDraft(next);
    setNotice(null);
  }

  async function saveBinding() {
    setAttempted(true);
    setNotice(null);
    if (
      readOnly ||
      !binding.loaded ||
      binding.loading ||
      mutation.pending ||
      readbackPending ||
      !validation.valid
    ) {
      return;
    }

    await mutation.run(
      (auth) => replaceAgentWikiBinding(auth, agentId, validation.request),
      () => {
        readbackSawLoadingRef.current = false;
        setReadbackPending(true);
        binding.reload();
        onChanged?.();
      },
    );
  }

  const modeOptions: Array<{ value: WikiBindingDraftMode; label: string }> = [
    { value: "NONE", label: copy.modeNone },
    { value: "PINNED", label: copy.modePinned },
    { value: "LATEST", label: copy.modeLatest },
  ];
  const knowledgeBaseOptions = bases.map((base) => ({
    value: base.id,
    label: base.name,
  }));
  const publicationSelectOptions = publicationOptions.map((option) => ({
    value: option.id,
    label: `${shortIdentifier(option.id)} · ${
      option.contentHash ? option.contentHash.slice(0, 12) : copy.hashUnavailable
    }${
      option.preservedFromCurrentBinding ? ` · ${copy.currentOptionSuffix}` : ""
    }`,
  }));

  return (
    <section className="binding-editor" aria-label={copy.title}>
      <h3>{copy.title}</h3>
      <p className="state-hint">{copy.intro}</p>
      {readOnly && (
        <p className="state-hint" role="note">
          {copy.readonly}
        </p>
      )}

      {binding.error && (
        <ErrorState
          code={binding.error.code}
          message={binding.error.message || copy.loadBindingError}
          hint={
            errorHintKey(binding.error)
              ? t(errorHintKey(binding.error)!)
              : undefined
          }
          onRetry={binding.reload}
        />
      )}
      {binding.loading && !binding.error && <LoadingState />}

      <div className="form-grid">
        <label>
          {copy.mode}
          <Select
            value={draft.mode}
            disabled={controlsDisabled || !binding.loaded}
            onChange={(value) =>
              updateDraft(switchWikiBindingMode(draft, modeFromValue(value)))
            }
            options={modeOptions}
          />
        </label>
        {draft.mode !== "NONE" && (
          <label>
            {copy.knowledgeBase}
            <Select
              value={draft.knowledge_base_id}
              disabled={controlsDisabled || bases.length === 0}
              onChange={(value) =>
                updateDraft(changeWikiBindingKnowledgeBase(draft, value))
              }
              options={knowledgeBaseOptions}
              placeholder={copy.selectKnowledgeBase}
            />
          </label>
        )}
      </div>

      {draft.mode === "NONE" && <p className="state-hint">{copy.noneHint}</p>}
      {draft.mode === "LATEST" && <p className="state-hint">{copy.latestHint}</p>}
      {draft.mode === "PINNED" && (
        <>
          <p className="state-hint">{copy.pinnedHint}</p>
          <p className="state-hint">{copy.onlyPublished}</p>
          {bases.length === 0 && (
            <p className="inline-error" role="alert">
              {copy.noKnowledgeBases}. {copy.noKnowledgeBasesHint}
            </p>
          )}
          {compilations.error && (
            <ErrorState
              code={compilations.error.code}
              message={compilations.error.message || copy.loadVersionsError}
              hint={
                errorHintKey(compilations.error)
                  ? t(errorHintKey(compilations.error)!)
                  : undefined
              }
              onRetry={compilations.reload}
            />
          )}
          {compilations.loading && !compilations.error && <LoadingState />}
          {draft.knowledge_base_id && !compilations.loading && (
            <label>
              {copy.publishedVersion}
              <Select
                value={draft.wiki_publication_id}
                disabled={controlsDisabled || publicationSelectOptions.length === 0}
                onChange={(value) =>
                  updateDraft({ ...draft, wiki_publication_id: value })
                }
                options={publicationSelectOptions}
                placeholder={copy.selectPublishedVersion}
              />
              <span className="state-hint">{copy.publishedVersionsHint}</span>
            </label>
          )}
          {selectedPublication?.preservedFromCurrentBinding && (
            <p className="inline-notice">{copy.preservedVersion}</p>
          )}
          {selectedPublication && (
            <dl className="key-values">
              <div className="key-value-row">
                <dt>{copy.publishedVersion}</dt>
                <dd>
                  <code>{selectedPublication.id}</code>
                </dd>
              </div>
              <div className="key-value-row">
                <dt>{copy.publishedHash}</dt>
                <dd>
                  <code className="hash-value">
                    {selectedPublication.contentHash || "—"}
                  </code>
                </dd>
              </div>
              <div className="key-value-row">
                <dt>{copy.publishedAt}</dt>
                <dd>
                  {selectedPublication.publishedAt
                    ? formatDateTime(selectedPublication.publishedAt)
                    : "—"}
                </dd>
              </div>
            </dl>
          )}
        </>
      )}

      {showValidation && validation.issues.length > 0 && (
        <div className="inline-error" role="alert">
          {validation.issues.map((issue) => (
            <p key={`${issue.field}:${issue.code}`}>{issueCopy(copy, issue)}</p>
          ))}
        </div>
      )}

      {currentBinding && (
        <div>
          <h4>{copy.currentBinding}</h4>
          <dl className="key-values">
            <div className="key-value-row">
              <dt>{copy.mode}</dt>
              <dd>
                {currentBinding.binding_mode === "LATEST"
                  ? copy.currentLatest
                  : copy.currentPinned}
              </dd>
            </div>
            <div className="key-value-row">
              <dt>{copy.knowledgeBase}</dt>
              <dd>
                <code>{currentBinding.knowledge_base_id}</code>
              </dd>
            </div>
            {currentBinding.wiki_publication_id && (
              <>
                <div className="key-value-row">
                  <dt>{copy.frozenPublication}</dt>
                  <dd>
                    <code>{currentBinding.wiki_publication_id}</code>
                  </dd>
                </div>
                <div className="key-value-row">
                  <dt>{copy.frozenHash}</dt>
                  <dd>
                    <code className="hash-value">
                      {currentBinding.publication_hash ?? "—"}
                    </code>
                  </dd>
                </div>
              </>
            )}
          </dl>
        </div>
      )}
      {binding.loaded && binding.data === null && (
        <p className="state-hint">
          {copy.currentBinding}: {copy.currentNone}
        </p>
      )}

      <InlineError error={mutation.error} fallback={t("errors.requestFailed")} />
      {readbackPending && <p className="state-hint" role="status">{copy.readback}</p>}
      {notice && <p className="inline-notice">{notice}</p>}
      <p className="state-hint">{copy.publishBoundary}</p>
      <div className="form-actions">
        <button
          type="button"
          className="button button-primary"
          onClick={() => void saveBinding()}
          disabled={
            readOnly ||
            !binding.loaded ||
            binding.loading ||
            binding.error !== null ||
            mutation.pending ||
            readbackPending
          }
        >
          {draft.mode === "NONE" ? copy.clear : copy.save}
        </button>
      </div>

      <h4>{copy.frozenTitle}</h4>
      <p className="state-hint">{copy.frozenHint}</p>
      {frozen ? (
        <dl className="key-values">
          <div className="key-value-row">
            <dt>{copy.frozenVersion}</dt>
            <dd>v{frozen.version.version_number}</dd>
          </div>
          <div className="key-value-row">
            <dt>{copy.frozenMode}</dt>
            <dd>{frozen.binding.binding_mode}</dd>
          </div>
          <div className="key-value-row">
            <dt>{copy.frozenKnowledgeBase}</dt>
            <dd>
              <code>{frozen.binding.knowledge_base_id}</code>
            </dd>
          </div>
          <div className="key-value-row">
            <dt>{copy.frozenPublication}</dt>
            <dd>
              <code>{frozen.binding.wiki_publication_id}</code>
            </dd>
          </div>
          <div className="key-value-row">
            <dt>{copy.frozenHash}</dt>
            <dd>
              <code className="hash-value">
                {shortHash(frozen.binding.publication_hash)}
              </code>
            </dd>
          </div>
        </dl>
      ) : (
        <p className="state-hint">{copy.noFrozen}</p>
      )}
    </section>
  );
}
