"use client";

import Select from "@/components/ui/select";
import { useI18n } from "@/i18n/provider";
import type {
  ContextPreset,
  FrozenContextPolicyProjection,
} from "@/lib/api/agents";
import {
  CONTEXT_PRESETS,
  type ContextPolicyDraft,
  type ContextPolicyField,
  type ContextPolicyValidation,
} from "@/lib/agents/context-policy";

type Copy = {
  title: string;
  intro: string;
  enable: string;
  enabledHint: string;
  clearAction: string;
  clearHint: string;
  dependencyBlocked: string;
  preset: string;
  presetHint: string;
  unsetLimit: string;
  defaultLimit: string;
  customLimit: string;
  customLimitLabel: string;
  boundary: string;
  targetPercent: string;
  softPercent: string;
  hardPercent: string;
  advanced: string;
  advancedHint: string;
  retainedTurns: string;
  maxHistoryGroups: string;
  reservePercent: string;
  reserveTokens: string;
  estimateTitle: string;
  estimateHint: string;
  targetTokens: string;
  softTokens: string;
  hardTokens: string;
  approximateCounter: string;
  frozenTitle: string;
  frozenHint: string;
  draftPreview: string;
  publishedPreview: string;
  version: string;
  workspaceLimit: string;
  actualInput: string;
  modelAvailable: string;
  outputReserve: string;
  thresholdValues: string;
  noProjection: string;
  model: string;
  available: string;
  reasons: string;
  limitations: string;
  unknowns: string;
  summaryPreserved: string;
  errors: Record<string, string>;
};

const COPY: Record<"zh-CN" | "en-US", Copy> = {
  "zh-CN": {
    title: "自适应上下文策略",
    intro:
      "该策略为上下文工作区设置上限。保存后先写入草稿；发布时服务端会按主模型与全部备用模型取最小可用输入，并冻结到版本中。",
    enable: "启用自适应上下文策略",
    enabledHint: "预设只决定工作区上限的默认边界；最终预算仍受模型链影响。",
    clearAction: "从草稿中清除",
    clearHint: "保存后将清除草稿中的 context_policy；已发布版本保持不变。",
    dependencyBlocked:
      "当前 runtime_config 还包含历史策略或摘要策略，后端要求它们依赖 context_policy。本页会原样保留这些字段，因此无法单独清除上下文策略。",
    preset: "上下文预设",
    presetHint: "切换预设会保留自定义输入，并在超出新边界时显示错误，不会替你重置。",
    unsetLimit: "未设置（按预设解析）",
    defaultLimit: "跟随预设默认值",
    customLimit: "自定义上限",
    customLimitLabel: "自定义工作区输入上限（token）",
    boundary: "该预设允许的最大值",
    targetPercent: "目标比例（%）",
    softPercent: "软触发比例（%）",
    hardPercent: "硬触发比例（%）",
    advanced: "高级历史与安全余量",
    advancedHint: "这些值进入服务端规范化结果，并在发布时冻结。",
    retainedTurns: "保留最近轮数",
    maxHistoryGroups: "最大历史组数",
    reservePercent: "安全余量比例（%）",
    reserveTokens: "安全余量 token",
    estimateTitle: "草稿估算边界",
    estimateHint:
      "客户端使用 approx_language_v1 估算阈值；它不保证与服务端 token 计数完全一致，且不是实际可用输入。",
    targetTokens: "目标阈值",
    softTokens: "软阈值",
    hardTokens: "硬阈值",
    approximateCounter: "估算计数器",
    frozenTitle: "服务端模型链投影",
    frozenHint:
      "实际可用输入取主模型与备用模型的最小值，再与工作区上限取较小者。每个冻结版本保留发布当时的数值。",
    draftPreview: "当前草稿预检投影",
    publishedPreview: "最近包含该策略的已发布版本",
    version: "版本",
    workspaceLimit: "工作区上限",
    actualInput: "实际可用输入",
    modelAvailable: "模型链最小可用输入",
    outputReserve: "输出预留",
    thresholdValues: "冻结阈值",
    noProjection: "保存并预检或发布后，可在这里查看服务端计算的实际模型链边界。",
    model: "模型",
    available: "可用输入",
    reasons: "限制来源",
    limitations: "模型限制",
    unknowns: "未知项",
    summaryPreserved:
      "现有摘要策略会作为 runtime_config 的其他字段原样保留；本页不提供通过摘要绕过上下文上限的开关。",
    errors: {
      INVALID_CUSTOM_LIMIT: "自定义上限必须是正整数，且不能超过当前预设允许的 token 数。",
      INVALID_TARGET_PERCENT: "目标比例必须是 5 到 90 的整数。",
      INVALID_SOFT_PERCENT: "软触发比例必须是 10 到 95 的整数。",
      INVALID_HARD_PERCENT: "硬触发比例必须是 15 到 100 的整数。",
      INVALID_THRESHOLD_ORDER: "比例必须满足：目标 < 软触发 < 硬触发。",
      INVALID_RETAINED_TURNS: "保留最近轮数必须是 0 到 50 的整数。",
      INVALID_HISTORY_GROUPS: "最大历史组数必须是 16 到 4096 的整数。",
      INVALID_RESERVE_PERCENT: "安全余量比例必须是 0 到 30 的整数。",
      INVALID_RESERVE_TOKENS: "安全余量 token 必须是 0 到 65536 的整数。",
    },
  },
  "en-US": {
    title: "Adaptive context policy",
    intro:
      "This policy bounds the context workspace. Saves first update the draft; publication resolves the minimum usable input across the primary and fallback models and freezes the result into the version.",
    enable: "Enable adaptive context policy",
    enabledHint:
      "The preset supplies the default workspace boundary. The model chain can reduce the final budget.",
    clearAction: "Clear from draft",
    clearHint:
      "Saving removes context_policy from the draft. Existing published versions remain unchanged.",
    dependencyBlocked:
      "This runtime_config contains history or summary settings that the backend requires context_policy for. This page preserves those fields, so it cannot clear the policy independently.",
    preset: "Context preset",
    presetHint:
      "Switching presets keeps custom input and reports when it exceeds the new boundary.",
    unsetLimit: "Unset (resolve from preset)",
    defaultLimit: "Follow preset default",
    customLimit: "Custom limit",
    customLimitLabel: "Custom workspace input limit (tokens)",
    boundary: "Maximum allowed by this preset",
    targetPercent: "Target percent",
    softPercent: "Soft trigger percent",
    hardPercent: "Hard trigger percent",
    advanced: "History and safety reserve",
    advancedHint: "These values are normalized by the server and frozen on publication.",
    retainedTurns: "Retained recent turns",
    maxHistoryGroups: "Maximum history groups",
    reservePercent: "Safety reserve percent",
    reserveTokens: "Safety reserve tokens",
    estimateTitle: "Draft estimate boundary",
    estimateHint:
      "The UI estimates thresholds with approx_language_v1. It is not the actual usable input and need not match the provider tokenizer exactly.",
    targetTokens: "Target threshold",
    softTokens: "Soft threshold",
    hardTokens: "Hard threshold",
    approximateCounter: "Approximation counter",
    frozenTitle: "Server model-chain projection",
    frozenHint:
      "Actual usable input is the minimum across primary and fallback models, reduced further by the workspace limit. Each published version keeps its publication-time values.",
    draftPreview: "Current draft preflight projection",
    publishedPreview: "Latest published version containing this policy",
    version: "Version",
    workspaceLimit: "Workspace limit",
    actualInput: "Actual usable input",
    modelAvailable: "Minimum available across chain",
    outputReserve: "Output reserve",
    thresholdValues: "Frozen thresholds",
    noProjection:
      "Run a server preflight or publish this draft to see the actual model-chain boundary resolved by the server.",
    model: "Model",
    available: "Available input",
    reasons: "Limit source",
    limitations: "Model limitations",
    unknowns: "Unknowns",
    summaryPreserved:
      "An existing summary policy is preserved as another runtime_config field. This page provides no toggle that would let summaries bypass the context limit.",
    errors: {
      INVALID_CUSTOM_LIMIT:
        "The custom limit must be a positive integer and cannot exceed the current preset.",
      INVALID_TARGET_PERCENT: "Target percent must be an integer from 5 to 90.",
      INVALID_SOFT_PERCENT: "Soft trigger percent must be an integer from 10 to 95.",
      INVALID_HARD_PERCENT: "Hard trigger percent must be an integer from 15 to 100.",
      INVALID_THRESHOLD_ORDER: "Percentages must satisfy target < soft trigger < hard trigger.",
      INVALID_RETAINED_TURNS: "Retained recent turns must be an integer from 0 to 50.",
      INVALID_HISTORY_GROUPS: "Maximum history groups must be an integer from 16 to 4096.",
      INVALID_RESERVE_PERCENT: "Safety reserve percent must be an integer from 0 to 30.",
      INVALID_RESERVE_TOKENS: "Safety reserve tokens must be an integer from 0 to 65536.",
    },
  },
};

function presetFromValue(value: string): ContextPreset {
  const match = CONTEXT_PRESETS.find((preset) => preset.value === value);
  return match?.value ?? "GENERAL_64K";
}

function sourceFromValue(value: string): ContextPolicyDraft["workspace_input_limit_source"] {
  if (value === "CUSTOM") return "CUSTOM";
  if (value === "DEFAULT") return "DEFAULT";
  return "UNSET";
}

function fieldCopy(copy: Copy, field: ContextPolicyField): string {
  switch (field) {
    case "workspace_input_limit":
      return copy.customLimitLabel;
    case "soft_trigger_percent":
      return copy.softPercent;
    case "hard_trigger_percent":
      return copy.hardPercent;
    case "target_percent":
      return copy.targetPercent;
    case "retained_recent_turns":
      return copy.retainedTurns;
    case "max_history_groups":
      return copy.maxHistoryGroups;
    case "safety_reserve_percent":
      return copy.reservePercent;
    case "safety_reserve_tokens":
      return copy.reserveTokens;
  }
}

function FrozenPolicyDetails({
  policy,
  label,
  versionNumber,
  copy,
  formatNumber,
}: {
  policy: FrozenContextPolicyProjection;
  label: string;
  versionNumber: number | null;
  copy: Copy;
  formatNumber: (value: number) => string;
}) {
  return (
    <div>
      <h4>
        {label}
        {versionNumber === null ? "" : ` · ${copy.version} ${versionNumber}`}
      </h4>
      <dl className="key-values">
        <div className="key-value-row">
          <dt>{copy.workspaceLimit}</dt>
          <dd>
            <code>{formatNumber(policy.workspace_input_limit)}</code> ·{" "}
            {policy.workspace_input_limit_source}
          </dd>
        </div>
        <div className="key-value-row">
          <dt>{copy.actualInput}</dt>
          <dd>
            <strong>{formatNumber(policy.input_limit_tokens)}</strong> token
          </dd>
        </div>
        <div className="key-value-row">
          <dt>{copy.modelAvailable}</dt>
          <dd>{formatNumber(policy.model_available_input_tokens)} token</dd>
        </div>
        <div className="key-value-row">
          <dt>{copy.outputReserve}</dt>
          <dd>{formatNumber(policy.reserved_output_tokens)} token</dd>
        </div>
        <div className="key-value-row">
          <dt>{copy.thresholdValues}</dt>
          <dd>
            {copy.targetTokens} {formatNumber(policy.thresholds.target)} · {copy.softTokens}{" "}
            {formatNumber(policy.thresholds.soft)} · {copy.hardTokens}{" "}
            {formatNumber(policy.thresholds.hard)}
          </dd>
        </div>
      </dl>
      <div className="data-table" style={{ marginTop: "0.75rem" }}>
        <table>
          <thead>
            <tr>
              <th scope="col">{copy.model}</th>
              <th scope="col">{copy.available}</th>
              <th scope="col">{copy.reasons}</th>
            </tr>
          </thead>
          <tbody>
            {policy.models.map((model) => (
              <tr key={`${model.profile_id}:${model.credential_ref}`}>
                <td>
                  <code>{`${model.provider}/${model.model}`}</code>
                  <span className="state-hint">{model.profile_id}</span>
                </td>
                <td className="numeric-cell">
                  {formatNumber(model.budget.available_input_tokens)}
                </td>
                <td>{model.reasons.length > 0 ? model.reasons.join(", ") : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {policy.limitations.length > 0 && (
        <p className="state-hint">
          {copy.limitations}:{" "}
          {policy.limitations
            .map((limitation) =>
              typeof limitation.code === "string" ? limitation.code : JSON.stringify(limitation),
            )
            .join(", ")}
        </p>
      )}
      {policy.unknowns.length > 0 && (
        <p className="state-hint">
          {copy.unknowns}: {policy.unknowns.join(", ")}
        </p>
      )}
    </div>
  );
}

export default function ContextPolicyEditor({
  draft,
  validation,
  disabled,
  hasExistingPolicy,
  hasDependentPolicies,
  runtimeHasSummaryPolicy,
  publishedPolicy,
  publishedVersionNumber,
  preflightPolicy,
  onChange,
}: {
  draft: ContextPolicyDraft;
  validation: ContextPolicyValidation;
  disabled: boolean;
  hasExistingPolicy: boolean;
  hasDependentPolicies: boolean;
  runtimeHasSummaryPolicy: boolean;
  publishedPolicy: FrozenContextPolicyProjection | null;
  publishedVersionNumber: number | null;
  preflightPolicy: FrozenContextPolicyProjection | null;
  onChange: (next: ContextPolicyDraft) => void;
}) {
  const { locale, formatNumber } = useI18n();
  const copy = locale === "zh-CN" ? COPY["zh-CN"] : COPY["en-US"];
  const clearBlocked =
    hasExistingPolicy && hasDependentPolicies && !draft.enabled;

  function update(patch: Partial<ContextPolicyDraft>) {
    onChange({ ...draft, ...patch });
  }

  return (
    <section aria-label={copy.title}>
      <h3>{copy.title}</h3>
      <p className="state-hint">{copy.intro}</p>
      <label className="checkbox-label">
        <input
          type="checkbox"
          checked={draft.enabled}
          disabled={disabled}
          onChange={(event) => update({ enabled: event.target.checked })}
        />
        {copy.enable}
      </label>

      {hasExistingPolicy && draft.enabled && (
        <div className="form-actions">
          <button
            type="button"
            className="button button-ghost"
            disabled={disabled}
            onClick={() => update({ enabled: false })}
          >
            {copy.clearAction}
          </button>
        </div>
      )}
      {!draft.enabled && hasExistingPolicy && (
        <p className="state-hint">{copy.clearHint}</p>
      )}
      {clearBlocked && (
        <p className="inline-error" role="alert">
          {copy.dependencyBlocked}
        </p>
      )}

      {draft.enabled && (
        <>
          <p className="state-hint">{copy.enabledHint}</p>
          <div className="form-grid">
            <label>
              {copy.preset}
              <Select
                value={draft.preset}
                disabled={disabled}
                onChange={(value) => update({ preset: presetFromValue(value) })}
                options={CONTEXT_PRESETS.map((preset) => ({
                  value: preset.value,
                  label: `${preset.label} · ${preset.value}`,
                }))}
              />
              <span className="state-hint">{copy.presetHint}</span>
            </label>
            <label>
              {copy.workspaceLimit}
              <Select
                value={draft.workspace_input_limit_source}
                disabled={disabled}
                onChange={(value) =>
                  update({ workspace_input_limit_source: sourceFromValue(value) })
                }
                options={[
                  { value: "UNSET", label: copy.unsetLimit },
                  { value: "DEFAULT", label: copy.defaultLimit },
                  { value: "CUSTOM", label: copy.customLimit },
                ]}
              />
              <span className="state-hint">
                {copy.boundary}: {formatNumber(validation.presetLimit)} token
              </span>
            </label>
            {draft.workspace_input_limit_source === "CUSTOM" && (
              <label>
                {copy.customLimitLabel}
                <input
                  type="number"
                  min="1"
                  max={validation.presetLimit}
                  step="1"
                  value={draft.workspace_input_limit}
                  disabled={disabled}
                  onChange={(event) =>
                    update({ workspace_input_limit: event.target.value })
                  }
                />
              </label>
            )}
          </div>

          <h4>{copy.thresholdValues}</h4>
          <div className="form-grid">
            <label>
              {copy.targetPercent}
              <input
                type="number"
                min="5"
                max="90"
                step="1"
                value={draft.target_percent}
                disabled={disabled}
                onChange={(event) => update({ target_percent: event.target.value })}
              />
            </label>
            <label>
              {copy.softPercent}
              <input
                type="number"
                min="10"
                max="95"
                step="1"
                value={draft.soft_trigger_percent}
                disabled={disabled}
                onChange={(event) =>
                  update({ soft_trigger_percent: event.target.value })
                }
              />
            </label>
            <label>
              {copy.hardPercent}
              <input
                type="number"
                min="15"
                max="100"
                step="1"
                value={draft.hard_trigger_percent}
                disabled={disabled}
                onChange={(event) =>
                  update({ hard_trigger_percent: event.target.value })
                }
              />
            </label>
          </div>
          <details className="technical-details">
            <summary>{copy.advanced}</summary>
            <p className="state-hint">{copy.advancedHint}</p>
            <div className="form-grid">
              <label>
                {copy.retainedTurns}
                <input
                  type="number"
                  min="0"
                  max="50"
                  step="1"
                  value={draft.retained_recent_turns}
                  disabled={disabled}
                  onChange={(event) =>
                    update({ retained_recent_turns: event.target.value })
                  }
                />
              </label>
              <label>
                {copy.maxHistoryGroups}
                <input
                  type="number"
                  min="16"
                  max="4096"
                  step="1"
                  value={draft.max_history_groups}
                  disabled={disabled}
                  onChange={(event) =>
                    update({ max_history_groups: event.target.value })
                  }
                />
              </label>
              <label>
                {copy.reservePercent}
                <input
                  type="number"
                  min="0"
                  max="30"
                  step="1"
                  value={draft.safety_reserve_percent}
                  disabled={disabled}
                  onChange={(event) =>
                    update({ safety_reserve_percent: event.target.value })
                  }
                />
              </label>
              <label>
                {copy.reserveTokens}
                <input
                  type="number"
                  min="0"
                  max="65536"
                  step="1"
                  value={draft.safety_reserve_tokens}
                  disabled={disabled}
                  onChange={(event) =>
                    update({ safety_reserve_tokens: event.target.value })
                  }
                />
              </label>
            </div>
          </details>

          {validation.issues.length > 0 && (
            <div className="inline-error" role="alert">
              {validation.issues.map((issue) => (
                <p key={`${issue.field}:${issue.code}`}>
                  {copy.errors[issue.code] ??
                    (issue.field === "thresholds"
                      ? issue.code
                      : `${fieldCopy(copy, issue.field)}: ${issue.code}`)}
                </p>
              ))}
            </div>
          )}

          <h4>{copy.estimateTitle}</h4>
          <p className="state-hint">{copy.estimateHint}</p>
          {validation.estimatedLimit !== null && validation.estimatedThresholds !== null ? (
            <div className="metric-card">
              <p className="metric-label">
                {copy.workspaceLimit}: {formatNumber(validation.estimatedLimit)} token
              </p>
              <p className="metric-hint">
                {copy.targetTokens} {formatNumber(validation.estimatedThresholds.target)} ·{" "}
                {copy.softTokens} {formatNumber(validation.estimatedThresholds.soft)} ·{" "}
                {copy.hardTokens} {formatNumber(validation.estimatedThresholds.hard)}
              </p>
              <p className="metric-hint">
                {copy.approximateCounter}: approx_language_v1
              </p>
            </div>
          ) : (
            <p className="state-hint">—</p>
          )}
        </>
      )}

      {runtimeHasSummaryPolicy && <p className="state-hint">{copy.summaryPreserved}</p>}

      <h4>{copy.frozenTitle}</h4>
      <p className="state-hint">{copy.frozenHint}</p>
      {preflightPolicy && (
        <FrozenPolicyDetails
          policy={preflightPolicy}
          label={copy.draftPreview}
          versionNumber={null}
          copy={copy}
          formatNumber={formatNumber}
        />
      )}
      {publishedPolicy && (
        <FrozenPolicyDetails
          policy={publishedPolicy}
          label={copy.publishedPreview}
          versionNumber={publishedVersionNumber}
          copy={copy}
          formatNumber={formatNumber}
        />
      )}
      {!preflightPolicy && !publishedPolicy && (
        <p className="state-hint">{copy.noProjection}</p>
      )}
    </section>
  );
}
