// Focused Agent Wiki-binding regression. Run from any working directory.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");

const root = path.resolve(__dirname, "..");
const webRoot = path.join(root, "apps", "web");
const componentPath = path.join(
  webRoot,
  "components",
  "agents",
  "wiki-binding-editor.tsx",
);
const pagePath = path.join(
  webRoot,
  "app",
  "agents",
  "[agentId]",
  "agent-detail-client.tsx",
);
const helperPath = path.join(webRoot, "lib", "agents", "wiki-binding.ts");

const requireWeb = Module.createRequire(path.join(webRoot, "package.json"));
const ts = requireWeb("typescript");
const React = requireWeb("react");
const { renderToStaticMarkup } = requireWeb("react-dom/server");

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

function isRecord(value) {
  return typeof value === "object" && value !== null;
}

class MockApiError extends Error {
  constructor(code, message, status) {
    super(message);
    this.name = "ApiError";
    this.code = code;
    this.status = status;
  }
}

function loadAgentsClient() {
  let response = null;
  const calls = [];
  const apiRequest = async (requestPath, accessToken, init) => {
    calls.push({ requestPath, accessToken, init });
    return response;
  };
  const clientMock = {
    __esModule: true,
    ApiError: MockApiError,
    apiRequest,
    isRecord,
  };
  const agents = loadTs("lib/api/agents.ts", {
    "@/lib/api/client": clientMock,
    "@/lib/api/tenancy": {
      __esModule: true,
      listItems: (payload) => payload.items,
    },
  });
  return {
    agents,
    calls,
    setResponse(next) {
      response = next;
    },
  };
}

async function runApiRegression() {
  const fixture = loadAgentsClient();
  const auth = { workspaceId: "workspace 1", accessToken: "real-access-token" };
  const agentId = "agent/id";
  const bindingPath =
    "/api/v1/workspaces/workspace%201/agents/agent%2Fid/wiki-binding";

  fixture.setResponse(null);
  assert.equal(await fixture.agents.getAgentWikiBinding(auth, agentId), null);
  assert.deepEqual(fixture.calls.at(-1), {
    requestPath: bindingPath,
    accessToken: "real-access-token",
    init: undefined,
  });

  const pinned = {
    workspace_id: auth.workspaceId,
    agent_id: agentId,
    knowledge_base_id: "kb-1",
    binding_mode: "PINNED",
    wiki_publication_id: "publication-1",
    publication_hash: "a".repeat(64),
  };
  fixture.setResponse(pinned);
  assert.deepEqual(
    await fixture.agents.getAgentWikiBinding(auth, agentId),
    pinned,
  );

  fixture.setResponse({
    ...pinned,
    publication_hash: null,
  });
  await assert.rejects(
    fixture.agents.getAgentWikiBinding(auth, agentId),
    (error) =>
      error instanceof MockApiError && error.code === "INVALID_RESPONSE",
    "PINNED without a hash must fail response validation",
  );

  fixture.setResponse(pinned);
  const pinnedInput = {
    knowledge_base_id: "kb-1",
    binding_mode: "PINNED",
    wiki_publication_id: "publication-1",
  };
  await fixture.agents.replaceAgentWikiBinding(auth, agentId, pinnedInput);
  assert.deepEqual(fixture.calls.at(-1), {
    requestPath: bindingPath,
    accessToken: "real-access-token",
    init: { method: "PUT", body: pinnedInput },
  });
  assert.equal(
    typeof fixture.calls.at(-1).init.body,
    "object",
    "the transport must serialize the body exactly once",
  );

  const latest = {
    workspace_id: auth.workspaceId,
    agent_id: agentId,
    knowledge_base_id: "kb-1",
    binding_mode: "LATEST",
    wiki_publication_id: null,
    publication_hash: null,
  };
  fixture.setResponse(latest);
  const latestInput = {
    knowledge_base_id: "kb-1",
    binding_mode: "LATEST",
    wiki_publication_id: null,
  };
  assert.deepEqual(
    await fixture.agents.replaceAgentWikiBinding(auth, agentId, latestInput),
    latest,
  );
  assert.deepEqual(fixture.calls.at(-1).init, {
    method: "PUT",
    body: latestInput,
  });

  fixture.setResponse(null);
  assert.equal(
    await fixture.agents.replaceAgentWikiBinding(auth, agentId, null),
    null,
  );
  assert.deepEqual(fixture.calls.at(-1).init, { method: "PUT", body: null });

  console.log(
    "PASS agents API: GET/PUT URL, encoded ids, real token, direct body, null clear, strict response",
  );
}

function loadHelper() {
  return loadTs("lib/agents/wiki-binding.ts");
}

function compilation(overrides) {
  return {
    id: "publication-1",
    source_snapshot_id: "snapshot-1",
    source_snapshot_hash: "b".repeat(64),
    identity_hash: "c".repeat(64),
    compiler_spec: {},
    status: "PUBLISHED",
    attempt_count: 1,
    error_code: null,
    content_hash: "d".repeat(64),
    telemetry: {},
    created_at: "2026-11-04T10:00:00Z",
    published_at: "2026-11-04T10:01:00Z",
    published_by: "user-1",
    ...overrides,
  };
}

function runHelperRegression(helper) {
  const pinned = {
    workspace_id: "workspace-1",
    agent_id: "agent-1",
    knowledge_base_id: "kb-1",
    binding_mode: "PINNED",
    wiki_publication_id: "publication-old",
    publication_hash: "e".repeat(64),
  };
  const loaded = helper.wikiBindingDraftFromBinding(pinned);
  assert.deepEqual(loaded, {
    mode: "PINNED",
    knowledge_base_id: "kb-1",
    wiki_publication_id: "publication-old",
  });
  assert.equal(helper.validateWikiBindingDraft(loaded).valid, true);

  const changedKb = helper.changeWikiBindingKnowledgeBase(loaded, "kb-2");
  assert.equal(changedKb.wiki_publication_id, "");
  assert.equal(
    helper.validateWikiBindingDraft(changedKb).issues.some(
      (issue) => issue.code === "PUBLICATION_REQUIRED",
    ),
    true,
    "changing the knowledge base must require a new publication",
  );
  assert.deepEqual(
    helper.validateWikiBindingDraft(
      helper.switchWikiBindingMode(loaded, "LATEST"),
    ).request,
    {
      knowledge_base_id: "kb-1",
      binding_mode: "LATEST",
      wiki_publication_id: null,
    },
  );
  assert.deepEqual(
    helper.validateWikiBindingDraft(helper.defaultWikiBindingDraft()),
    {
      valid: true,
      request: null,
      issues: [],
    },
  );

  const options = helper.publishedWikiOptions(
    [
      compilation({ id: "publication-new" }),
      compilation({
        id: "publication-ready",
        status: "READY",
        published_at: null,
      }),
      compilation({
        id: "publication-second",
        content_hash: "f".repeat(64),
        published_at: "2026-11-03T10:01:00Z",
      }),
    ],
    { knowledgeBaseId: "kb-1", binding: pinned },
  );
  assert.deepEqual(
    options.map((option) => [option.id, option.preservedFromCurrentBinding]),
    [
      ["publication-new", false],
      ["publication-second", false],
      ["publication-old", true],
    ],
    "only PUBLISHED entries appear and an out-of-page current binding is retained",
  );

  const frozen =
    helper.extractFrozenWikiBinding({
      wiki_binding: {
        schema_version: 1,
        binding_mode: "LATEST",
        knowledge_base_id: "kb-1",
        wiki_publication_id: "publication-frozen-at-v7",
        publication_hash: "1".repeat(64),
      },
    });
  assert.equal(frozen.wiki_publication_id, "publication-frozen-at-v7");
  const latest = helper.latestFrozenWikiBinding([
    {
      id: "version-7",
      workspace_id: "workspace-1",
      agent_id: "agent-1",
      version_number: 7,
      spec_schema_version: 2,
      resolved_spec: { wiki_binding: frozen },
      resolved_spec_hash: "2".repeat(64),
      created_at: "2026-11-01T00:00:00Z",
    },
    {
      id: "version-8",
      workspace_id: "workspace-1",
      agent_id: "agent-1",
      version_number: 8,
      spec_schema_version: 2,
      resolved_spec: {},
      resolved_spec_hash: "3".repeat(64),
      created_at: "2026-11-02T00:00:00Z",
    },
  ]);
  assert.equal(latest.version.version_number, 7);
  assert.equal(
    latest.binding.wiki_publication_id,
    "publication-frozen-at-v7",
    "historical LATEST must remain the frozen publication",
  );

  assert.notEqual(
    helper.credentialRevision("token-a"),
    helper.credentialRevision("token-b"),
  );
  assert.deepEqual(
    helper.evaluateWikiBindingReadback({
      pending: true,
      sawLoading: true,
      loading: false,
      loaded: true,
      error: null,
      data: null,
    }),
    { status: "SUCCESS", binding: null },
    "a successful clear is recognized from GET, not PUT truthiness",
  );
  assert.deepEqual(
    helper.evaluateWikiBindingReadback({
      pending: true,
      sawLoading: true,
      loading: false,
      loaded: false,
      error: { code: "WIKI_BINDING_READ_FAILED" },
      data: null,
    }),
    {
      status: "ERROR",
      error: { code: "WIKI_BINDING_READ_FAILED" },
    },
  );

  console.log(
    "PASS Wiki helper: draft validation, PUBLISHED filtering, old binding retention, frozen LATEST, readback",
  );
}

function loadComponent(helper, queryState) {
  const Select = (props) =>
    React.createElement(
      "select",
      {
        "aria-label": props.placeholder || "select",
        value: props.value,
        disabled: props.disabled,
        readOnly: true,
      },
      (props.options || []).map((option) =>
        React.createElement(
          "option",
          { key: option.value, value: option.value },
          option.label,
        ),
      ),
    );
  const State = ({ children }) =>
    React.createElement("div", null, children || null);
  const translation = {
    "common.created": "创建时间",
    "errors.requestFailed": "请求失败",
  };

  return loadTs("components/agents/wiki-binding-editor.tsx", {
    "@/components/providers/session-provider": {
      __esModule: true,
      useFrontendSession: () => ({
        workspaceId: "workspace-1",
        sessionId: 4,
        accessToken: "real-access-token",
      }),
    },
    "@/components/ui/select": { __esModule: true, default: Select },
    "@/components/ui/states": {
      __esModule: true,
      ErrorState: ({ code, message }) =>
        React.createElement("div", { role: "alert" }, `${code}: ${message}`),
      InlineError: ({ error }) =>
        error
          ? React.createElement("div", { role: "alert" }, error.code)
          : null,
      LoadingState: () => React.createElement(State, null, "loading"),
    },
    "@/i18n/provider": {
      __esModule: true,
      useI18n: () => ({
        t: (key) => translation[key] || key,
        locale: "zh-CN",
        formatDateTime: (value) => `date:${value}`,
      }),
    },
    "@/hooks/use-workspace-data": {
      __esModule: true,
      useWorkspaceData: (_load, resourceKey) =>
        resourceKey.includes("wiki-compilations")
          ? queryState.compilations
          : queryState.binding,
      useWorkspaceMutation: () => ({
        run: async (mutate, onSuccess) => {
          queryState.mutateCalls += 1;
          const result = await mutate({
            workspaceId: "workspace-1",
            accessToken: "real-access-token",
          });
          onSuccess?.(result);
          return result;
        },
        pending: false,
        error: null,
        clearError: () => {},
      }),
    },
    "@/lib/api/agents": {
      __esModule: true,
      getAgentWikiBinding: async () => queryState.binding.data,
      replaceAgentWikiBinding: async (_auth, _agentId, body) => {
        queryState.lastWrite = body;
        return body === null ? null : queryState.binding.data;
      },
    },
    "@/lib/api/client": {
      __esModule: true,
      errorHintKey: () => null,
    },
    "@/lib/api/wiki": {
      __esModule: true,
      listCompilations: async () => queryState.compilations.data || [],
    },
    "@/lib/agents/wiki-binding": helper,
    "react/jsx-runtime": requireWeb("react/jsx-runtime"),
  }).default;
}

function query(data, overrides = {}) {
  return {
    data,
    error: null,
    loading: false,
    loaded: true,
    reload: () => {},
    ...overrides,
  };
}

function renderComponent(helper, state) {
  const Editor = loadComponent(helper, state);
  const firstCompilation = compilation({ id: "publication-current" });
  const secondCompilation = compilation({
    id: "publication-newer",
    content_hash: "9".repeat(64),
    created_at: "2026-11-05T10:00:00Z",
    published_at: "2026-11-05T10:01:00Z",
  });
  const unpublished = compilation({
    id: "publication-ready-do-not-show",
    status: "READY",
    content_hash: null,
    published_at: null,
  });
  state.compilations = query([
    secondCompilation,
    firstCompilation,
    unpublished,
  ]);
  return renderToStaticMarkup(
    React.createElement(Editor, {
      agentId: "agent-1",
      bases: [{ id: "kb-1", workspace_id: "workspace-1", name: "Docs", created_at: "" }],
      versions: [
        {
          id: "version-7",
          workspace_id: "workspace-1",
          agent_id: "agent-1",
          version_number: 7,
          spec_schema_version: 2,
          resolved_spec: {
            wiki_binding: {
              schema_version: 1,
              binding_mode: "LATEST",
              knowledge_base_id: "kb-1",
              wiki_publication_id: "publication-frozen-at-v7",
              publication_hash: "frozen-hash-at-v7",
            },
          },
          resolved_spec_hash: "4".repeat(64),
          created_at: "2026-11-01T00:00:00Z",
        },
      ],
      canEdit: true,
      onChanged: () => {},
    }),
  );
}

function runComponentRegression(helper) {
  const state = {
    mutateCalls: 0,
    lastWrite: undefined,
    binding: query({
      workspace_id: "workspace-1",
      agent_id: "agent-1",
      knowledge_base_id: "kb-1",
      binding_mode: "PINNED",
      wiki_publication_id: "publication-current",
      publication_hash: "8".repeat(64),
    }),
  };
  const markup = renderComponent(helper, state);
  assert.match(markup, /Wiki 绑定/);
  assert.match(markup, /publication-current/);
  assert.match(markup, /publication-newer/);
  assert.doesNotMatch(
    markup,
    /publication-ready-do-not-show/,
    "the actual component must not offer unpublished compilations",
  );
  assert.match(markup, /publication-frozen-at-v7/);
  assert.match(markup, /v7/);
  const frozenMarkup = markup.slice(markup.indexOf("已发布版本中的冻结结果"));
  assert.match(frozenMarkup, /publication-frozen-at-v7/);
  assert.doesNotMatch(
    frozenMarkup,
    /publication-newer/,
    "the frozen version must not be replaced by the current latest compilation",
  );

  const emptyState = {
    mutateCalls: 0,
    lastWrite: undefined,
    binding: query(null),
  };
  const emptyMarkup = renderComponent(helper, emptyState);
  assert.match(emptyMarkup, /服务端当前草稿绑定/);
  assert.match(emptyMarkup, /未绑定/);
  assert.match(emptyMarkup, /清除 Wiki 绑定/);

  const errorState = {
    mutateCalls: 0,
    lastWrite: undefined,
    binding: query(null, {
      loaded: false,
      error: {
        code: "WIKI_BINDING_LOAD_FAILED",
        message: "Could not load binding",
      },
    }),
  };
  const errorMarkup = renderComponent(helper, errorState);
  assert.match(errorMarkup, /WIKI_BINDING_LOAD_FAILED/);
  assert.doesNotMatch(errorMarkup, /服务端回读成功/);

  console.log(
    "PASS TSX render: loaded/null/error states, PUBLISHED-only selector, frozen historical LATEST",
  );
}

function assertSourceContract() {
  const component = fs.readFileSync(componentPath, "utf8");
  const page = fs.readFileSync(pagePath, "utf8");
  const helper = fs.readFileSync(helperPath, "utf8");

  assert.match(component, /useWorkspaceData/);
  assert.match(component, /useWorkspaceMutation/);
  assert.match(component, /binding\.reload\(\)/);
  assert.match(
    component,
    /mutation\.run\([\s\S]*?binding\.reload\(\)/,
    "the write-success callback must request a real server readback",
  );
  assert.match(component, /InlineError error=\{mutation\.error\}/);
  assert.doesNotMatch(component, /if\s*\(\s*result\s*\)/);
  assert.match(component, /credentialRevision\(accessToken\)/);
  assert.doesNotMatch(component, /:\s*any\b|<any>|\bas\s+any\b/);
  assert.doesNotMatch(`${component}\n${helper}`, /publishAgent|publishCompilation/);
  assert.match(page, /<WikiBindingEditor/);
  assert.match(page, /onChanged=\{invalidatePreflight\}/);
  assert.match(page, /mergeRuntimeConfigDraft/, "Wiki integration must preserve existing runtime fields");
  assert.match(component, /only PUBLISHED|只包含 PUBLISHED|PUBLISHED compilations/);
  console.log(
    "PASS source contract: existing hooks, GET readback, no truthy-clear, permission/token scope, no auto-publish",
  );
}

async function main() {
  await runApiRegression();
  const helper = loadHelper();
  runHelperRegression(helper);
  runComponentRegression(helper);
  assertSourceContract();
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
