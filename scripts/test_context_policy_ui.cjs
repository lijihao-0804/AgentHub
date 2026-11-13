// Focused context-policy editor regression. Run from any working directory.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");

const root = path.resolve(__dirname, "..");
const webRoot = path.join(root, "apps", "web");
const helperPath = path.join(webRoot, "lib", "agents", "context-policy.ts");
const agentsApiPath = path.join(webRoot, "lib", "api", "agents.ts");
const componentPath = path.join(webRoot, "components", "agents", "context-policy-editor.tsx");
const pagePath = path.join(webRoot, "app", "agents", "[agentId]", "agent-detail-client.tsx");
const requireWeb = Module.createRequire(path.join(webRoot, "package.json"));
const ts = requireWeb("typescript");
const React = requireWeb("react");

function loadTs(relative, mocks = {}) {
  const filename = path.join(webRoot, relative);
  const output = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
    compilerOptions: {
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.CommonJS,
      jsx: ts.JsxEmit.ReactJSX,
      esModuleInterop: true,
    },
    fileName: filename,
  }).outputText;
  const module = { exports: {} };
  new Function("require", "module", "exports", "__filename", "__dirname", output)(
    (specifier) => {
      if (specifier in mocks) return mocks[specifier];
      if (specifier.startsWith("@/")) {
        throw new Error(`Unexpected unmocked app import: ${specifier}`);
      }
      return requireWeb(specifier);
    },
    module,
    module.exports,
    filename,
    path.dirname(filename),
  );
  return module.exports;
}

function runHelperRegression(helper) {
  assert.deepEqual(
    helper.CONTEXT_PRESETS.map((preset) => [preset.value, preset.tokens]),
    [
      ["MINI_16K", 16384],
      ["STANDARD_32K", 32768],
      ["GENERAL_64K", 65536],
      ["LONG_DOCUMENT_128K", 131072],
      ["EXTENDED_256K", 262144],
    ],
    "presets must match the backend contract",
  );

  const defaultDraft = { ...helper.defaultContextPolicyDraft(), enabled: true };
  const defaultPolicy = helper.validateContextPolicyDraft(defaultDraft);
  assert.equal(defaultPolicy.valid, true);
  assert.equal(defaultPolicy.policy.workspace_input_limit_source, "UNSET");
  assert.equal("workspace_input_limit" in defaultPolicy.policy, false);
  assert.deepEqual(defaultPolicy.estimatedThresholds, {
    target: 36044,
    soft: 45876,
    hard: 55706,
  });

  const customDraft = {
    ...defaultDraft,
    preset: "LONG_DOCUMENT_128K",
    workspace_input_limit_source: "CUSTOM",
    workspace_input_limit: "100000",
  };
  const customPolicy = helper.validateContextPolicyDraft(customDraft);
  assert.equal(customPolicy.valid, true);
  assert.equal(customPolicy.policy.workspace_input_limit, 100000);

  const invalidAfterPresetSwitch = { ...customDraft, preset: "MINI_16K" };
  const invalidPolicy = helper.validateContextPolicyDraft(invalidAfterPresetSwitch);
  assert.equal(invalidPolicy.valid, false);
  assert.ok(invalidPolicy.issues.some((issue) => issue.code === "INVALID_CUSTOM_LIMIT"));
  assert.equal(invalidAfterPresetSwitch.workspace_input_limit, "100000");
  assert.equal(invalidAfterPresetSwitch.preset, "MINI_16K");

  const loaded = helper.contextPolicyDraftFromPolicy({
    policy_version: "adaptive_context_v1",
    preset: "STANDARD_32K",
    workspace_input_limit: 30000,
    workspace_input_limit_source: "CUSTOM",
    soft_trigger_percent: 72,
    hard_trigger_percent: 88,
    target_percent: 60,
    retained_recent_turns: 4,
    max_history_groups: 128,
    safety_reserve_percent: 6,
    safety_reserve_tokens: 2048,
    counter_id: "approx_language_v1",
  });
  assert.equal(loaded.enabled, true);
  assert.equal(loaded.preset, "STANDARD_32K");
  assert.equal(loaded.workspace_input_limit_source, "CUSTOM");
  assert.equal(loaded.workspace_input_limit, "30000");
  assert.equal(loaded.target_percent, "60");

  const summaryPolicy = {
    enabled: true,
    policy_version: "thread_summary_policy_v1",
    prompt_version: "summary_candidate_zh_v1",
    validator_version: "summary_mechanical_v1",
    tokenizer_id: "approx_language_v1",
    tokenizer_version: 1,
    soft_trigger_percent: 70,
    hard_trigger_percent: 85,
    target_percent: 55,
  };
  const currentRuntime = {
    max_steps: 8,
    max_tool_calls: 12,
    context_budget: {
      reserved_output_tokens: 2000,
      max_retrieval_tokens: 5000,
      max_tool_result_tokens: 4000,
    },
    context_budget_sources: {
      reserved_output_tokens: "DEFAULT",
      max_retrieval_tokens: "CUSTOM",
      max_tool_result_tokens: "UNSET",
    },
    memory: {
      thread_history_search: true,
      long_term_memory: true,
      max_memory_tokens: 1500,
      max_memory_tokens_source: "CUSTOM",
    },
    context_policy: {
      policy_version: "adaptive_context_v1",
      preset: "GENERAL_64K",
      workspace_input_limit_source: "DEFAULT",
      soft_trigger_percent: 70,
      hard_trigger_percent: 85,
      target_percent: 55,
      counter_id: "approx_language_v1",
    },
    history_policy: "deterministic_history_v1",
    summary_policy: summaryPolicy,
    future_runtime_field: { keep: true },
  };
  const merged = helper.mergeRuntimeConfigDraft({
    currentRuntime,
    runtimeDraft: {
      max_steps: "8",
      max_tool_calls: "12",
      max_identical_calls: "",
      max_parallel_reads: "",
      reserved_output_tokens: "2000",
      max_retrieval_tokens: "5000",
      max_tool_result_tokens: "4000",
    },
    memoryDraft: {
      thread_history_search: true,
      long_term_memory: true,
    },
    contextDraft: customDraft,
  });
  assert.deepEqual(merged.future_runtime_field, { keep: true });
  assert.deepEqual(merged.summary_policy, summaryPolicy);
  assert.equal(merged.history_policy, "deterministic_history_v1");
  assert.deepEqual(merged.memory, currentRuntime.memory);
  assert.deepEqual(merged.context_budget_sources, currentRuntime.context_budget_sources);
  assert.equal(merged.context_policy.workspace_input_limit, 100000);
  assert.equal(helper.hasContextPolicyDependents(merged), true);

  const experimentRuntime = {
    ...currentRuntime,
    memory: {
      ...currentRuntime.memory,
      selection_policy: "lexical_overlap_v1",
      future_memory_field: { keep: true },
    },
  };
  const experimentMerged = helper.mergeRuntimeConfigDraft({
    currentRuntime: experimentRuntime,
    runtimeDraft: {
      max_steps: "8",
      max_tool_calls: "12",
      max_identical_calls: "",
      max_parallel_reads: "",
      reserved_output_tokens: "2000",
      max_retrieval_tokens: "5000",
      max_tool_result_tokens: "4000",
    },
    memoryDraft: {
      thread_history_search: true,
      long_term_memory: true,
      selection_policy: "lexical_overlap_v1",
    },
    contextDraft: customDraft,
  });
  assert.equal(experimentMerged.memory.selection_policy, "lexical_overlap_v1");
  assert.equal(experimentMerged.memory.max_memory_tokens, 1500);
  assert.equal(experimentMerged.memory.max_memory_tokens_source, "CUSTOM");
  assert.deepEqual(experimentMerged.memory.future_memory_field, { keep: true });

  const legacyMemoryMerged = helper.mergeRuntimeConfigDraft({
    currentRuntime: experimentRuntime,
    runtimeDraft: {
      max_steps: "8",
      max_tool_calls: "12",
      max_identical_calls: "",
      max_parallel_reads: "",
      reserved_output_tokens: "2000",
      max_retrieval_tokens: "5000",
      max_tool_result_tokens: "4000",
    },
    memoryDraft: {
      thread_history_search: true,
      long_term_memory: true,
      selection_policy: "",
    },
    contextDraft: customDraft,
  });
  assert.equal("selection_policy" in legacyMemoryMerged.memory, false);
  assert.equal(legacyMemoryMerged.memory.max_memory_tokens, 1500);
  assert.equal(legacyMemoryMerged.memory.max_memory_tokens_source, "CUSTOM");

  const invalidMemoryDraft = {
    thread_history_search: true,
    long_term_memory: false,
    selection_policy: "lexical_overlap_v1",
  };
  assert.deepEqual(helper.validateMemoryDraft(invalidMemoryDraft), {
    valid: false,
    issue: "LONG_TERM_MEMORY_REQUIRED",
  });
  assert.throws(
    () =>
      helper.mergeRuntimeConfigDraft({
        currentRuntime: experimentRuntime,
        runtimeDraft: {
          max_steps: "",
          max_tool_calls: "",
          max_identical_calls: "",
          max_parallel_reads: "",
          reserved_output_tokens: "",
          max_retrieval_tokens: "",
          max_tool_result_tokens: "",
        },
        memoryDraft: invalidMemoryDraft,
        contextDraft: customDraft,
      }),
    RangeError,
    "lexical_overlap_v1 must not be submitted when long-term memory is off",
  );
  assert.deepEqual(helper.memoryDraftFromConfig(experimentMerged.memory), {
    thread_history_search: true,
    long_term_memory: true,
    selection_policy: "lexical_overlap_v1",
  });
  assert.deepEqual(
    helper.memoryDraftFromConfig({
      thread_history_search: true,
      long_term_memory: true,
      max_memory_tokens: 1500,
    }),
    {
      thread_history_search: true,
      long_term_memory: true,
      selection_policy: "",
    },
    "historical memory without selection_policy must stay on the legacy strategy",
  );

  const invalidRuntimeDraft = {
    max_steps: "abc",
    max_tool_calls: "0",
    max_identical_calls: "2.5",
    max_parallel_reads: "",
    reserved_output_tokens: "500",
    max_retrieval_tokens: "",
    max_tool_result_tokens: "not-a-number",
  };
  assert.deepEqual(helper.invalidRuntimeLimitFields(invalidRuntimeDraft), [
    "max_steps",
    "max_tool_calls",
    "max_identical_calls",
    "max_tool_result_tokens",
  ]);
  const currentRuntimeBeforeInvalidSave = structuredClone(currentRuntime);
  assert.throws(
    () =>
      helper.mergeRuntimeConfigDraft({
        currentRuntime,
        runtimeDraft: invalidRuntimeDraft,
        memoryDraft: {
          thread_history_search: true,
          long_term_memory: true,
        },
        contextDraft: customDraft,
      }),
    RangeError,
    "a defensive merge must reject non-empty invalid values instead of deleting stored limits",
  );
  assert.deepEqual(
    currentRuntime,
    currentRuntimeBeforeInvalidSave,
    "rejecting invalid runtime input must not mutate the server snapshot",
  );
  const emptyRuntimeDraft = {
    max_steps: "",
    max_tool_calls: "",
    max_identical_calls: "",
    max_parallel_reads: "",
    reserved_output_tokens: "",
    max_retrieval_tokens: "",
    max_tool_result_tokens: "",
  };
  const explicitClears = helper.mergeRuntimeConfigDraft({
    currentRuntime,
    runtimeDraft: emptyRuntimeDraft,
    memoryDraft: {
      thread_history_search: true,
      long_term_memory: true,
    },
    contextDraft: customDraft,
  });
  assert.equal("max_steps" in explicitClears, false);
  assert.equal("context_budget" in explicitClears, false);

  const clearMerged = helper.mergeRuntimeConfigDraft({
    currentRuntime: { ...currentRuntime, history_policy: null, summary_policy: null },
    runtimeDraft: {
      max_steps: "8",
      max_tool_calls: "12",
      max_identical_calls: "",
      max_parallel_reads: "",
      reserved_output_tokens: "2000",
      max_retrieval_tokens: "5000",
      max_tool_result_tokens: "4000",
    },
    memoryDraft: {
      thread_history_search: true,
      long_term_memory: true,
    },
    contextDraft: {
      ...defaultDraft,
      enabled: false,
    },
  });
  assert.equal(clearMerged.context_policy, null);

  const frozen = helper.extractFrozenContextPolicy({
    runtime: {
      context_policy: {
        policy_version: "adaptive_context_v1",
        preset: "GENERAL_64K",
        workspace_input_limit: 65536,
        workspace_input_limit_source: "DEFAULT",
        input_limit_tokens: 29184,
        model_available_input_tokens: 29184,
        min_total_context_tokens: 32768,
        reserved_output_tokens: 2000,
        thresholds: { soft: 20429, hard: 24807, target: 16051 },
        counter: { id: "approx_language_v1", version: 1, precision: "APPROXIMATE" },
        models: [
          {
            profile_id: "primary",
            credential_ref: "credential-primary",
            provider: "provider-a",
            model: "model-a",
            resolved_model: "model-a",
            budget: {
              reserved_output_tokens: 2000,
              applicable_input_limit_tokens: 30720,
              safety_reserve_tokens: 1536,
              available_input_tokens: 29184,
            },
            reasons: ["TOTAL_WINDOW_MINUS_OUTPUT"],
          },
          {
            profile_id: "fallback",
            credential_ref: "credential-fallback",
            provider: "provider-b",
            model: "model-b",
            resolved_model: "model-b",
            budget: {
              reserved_output_tokens: 2000,
              applicable_input_limit_tokens: 32768,
              safety_reserve_tokens: 1638,
              available_input_tokens: 31130,
            },
            reasons: ["PROVIDER_INPUT_LIMIT"],
          },
        ],
        limitations: [],
        unknowns: [],
      },
    },
  });
  assert.equal(frozen.input_limit_tokens, 29184);
  assert.equal(frozen.models.length, 2);
  assert.equal(frozen.models[1].budget.available_input_tokens, 31130);
  console.log(
    "PASS helper presets, runtime/memory validation, merge preservation, legacy removal, frozen projection",
  );
}

function assertTsxContract() {
  const component = fs.readFileSync(componentPath, "utf8");
  const page = fs.readFileSync(pagePath, "utf8");
  const helper = fs.readFileSync(helperPath, "utf8");
  const agentsApi = fs.readFileSync(agentsApiPath, "utf8");

  assert.match(component, /CONTEXT_PRESETS\.map/, "preset selector must use the shared preset catalog");
  assert.match(component, /approx_language_v1/, "the UI must label estimates with their counter");
  assert.match(component, /publishedPolicy/, "published frozen projections must be rendered");
  assert.match(component, /preflightPolicy/, "draft preflight projections must be rendered");
  assert.match(component, /model_available_input_tokens/, "the actual model-chain bound must be exposed");
  assert.match(component, /runtimeHasSummaryPolicy/, "summary preservation must remain visible");
  assert.doesNotMatch(
    component,
    /summary_policy\s*:/,
    "the context editor must not edit or promote summary policy",
  );
  assert.match(page, /mergeRuntimeConfigDraft/, "the page must merge instead of replacing runtime_config");
  assert.match(
    page,
    /invalidRuntimeLimitFields\(runtime\)/,
    "the real save path must reject invalid non-empty runtime numbers",
  );
  assert.match(
    page,
    /if \(\s*!runtimeValuesValid[\s\S]*?\) \{\s*return;/,
    "invalid runtime input must stop before PATCH and remain in the form",
  );
  assert.match(
    agentsApi,
    /selection_policy\?: MemorySelectionPolicy \| null/,
    "the memory API type must include the explicit experiment strategy",
  );
  assert.match(
    page,
    /memoryDraftValidation\.valid/,
    "the real save path must reject lexical policy when long-term memory is off",
  );
  assert.match(
    page,
    /同义词[\s\S]*跨语言[\s\S]*语义质量/,
    "the UI must state the lexical retrieval limitations",
  );
  assert.match(
    page,
    /setContextPolicy\(contextPolicyDraftFromPolicy\(result\.runtime_config\.context_policy\)\)/,
    "successful PATCH responses must hydrate the normalized server draft",
  );
  assert.doesNotMatch(
    `${component}\n${page}\n${helper}`,
    /\b(?:balanced|conservative|aggressive)\b/,
    "prototype preset vocabulary must not leak into the implementation",
  );
  assert.doesNotMatch(
    `${component}\n${page}\n${helper}`,
    /\bany\b/,
    "the context policy path must not use an any escape",
  );
  console.log(
    "PASS TSX source contract: presets, frozen chain projection, summary preservation, and server readback",
  );
}

function tryRenderWithInstalledToolchain() {
  const helper = loadTs("lib/agents/context-policy.ts");
  const Select = ({ options, ariaLabel, value }) =>
    React.createElement(
      "select",
      { "aria-label": ariaLabel, defaultValue: value },
      options.map((option) =>
        React.createElement("option", { key: option.value, value: option.value }, option.label),
      ),
    );
  const Editor = loadTs("components/agents/context-policy-editor.tsx", {
    "@/components/ui/select": { __esModule: true, default: Select },
    "@/i18n/provider": {
      useI18n: () => ({
        locale: "zh-CN",
        formatNumber: (value) => String(value),
      }),
    },
    "@/lib/agents/context-policy": helper,
    "react/jsx-runtime": requireWeb("react/jsx-runtime"),
  }).default;

  const draft = {
    ...helper.defaultContextPolicyDraft(),
    enabled: true,
    preset: "MINI_16K",
    workspace_input_limit_source: "CUSTOM",
    workspace_input_limit: "999999",
  };
  const validation = helper.validateContextPolicyDraft(draft);
  const frozen = helper.extractFrozenContextPolicy({
    runtime: {
      context_policy: {
        policy_version: "adaptive_context_v1",
        preset: "MINI_16K",
        workspace_input_limit: 16384,
        workspace_input_limit_source: "DEFAULT",
        input_limit_tokens: 12000,
        model_available_input_tokens: 12000,
        min_total_context_tokens: 16384,
        reserved_output_tokens: 2000,
        thresholds: { target: 6600, soft: 8400, hard: 10200 },
        counter: { id: "approx_language_v1", version: 1, precision: "APPROXIMATE" },
        models: [{
          profile_id: "primary",
          credential_ref: "credential-primary",
          provider: "provider-a",
          model: "model-a",
          resolved_model: "model-a",
          budget: {
            reserved_output_tokens: 2000,
            applicable_input_limit_tokens: 14000,
            safety_reserve_tokens: 2000,
            available_input_tokens: 12000,
          },
          reasons: ["TOTAL_WINDOW_MINUS_OUTPUT"],
        }],
        limitations: [],
        unknowns: [],
      },
    },
  });
  const { renderToStaticMarkup } = requireWeb("react-dom/server");
  const markup = renderToStaticMarkup(
    React.createElement(Editor, {
      draft,
      validation,
      disabled: false,
      hasExistingPolicy: true,
      hasDependentPolicies: true,
      runtimeHasSummaryPolicy: true,
      publishedPolicy: frozen,
      publishedVersionNumber: 7,
      preflightPolicy: null,
      onChange: () => {},
    }),
  );
  for (const preset of [
    "MINI_16K",
    "STANDARD_32K",
    "GENERAL_64K",
    "LONG_DOCUMENT_128K",
    "EXTENDED_256K",
  ]) {
    assert.match(markup, new RegExp(`value="${preset}"`));
  }
  assert.match(markup, /value="999999"/, "invalid custom input must stay visible");
  assert.match(markup, />12000</, "the frozen primary model bound must be rendered");
  console.log("PASS TSX render: presets, retained invalid input, frozen model bound");
}

async function main() {
  const helper = loadTs("lib/agents/context-policy.ts");
  runHelperRegression(helper);
  assertTsxContract();
  tryRenderWithInstalledToolchain();
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
