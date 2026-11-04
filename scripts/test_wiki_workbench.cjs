// Run from the repository root. Exercises the real Wiki API client and page component.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");

const web = path.resolve(__dirname, "..", "apps", "web");
const requireWeb = Module.createRequire(path.join(web, "package.json"));
const ts = requireWeb("typescript");
const React = requireWeb("react");

const auth = { workspaceId: "workspace/a", accessToken: "token-a" };

function transpile(relativePath) {
  const source = fs.readFileSync(path.join(web, relativePath), "utf8");
  return ts.transpileModule(source, {
    compilerOptions: {
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.CommonJS,
      jsx: ts.JsxEmit.ReactJSX,
      esModuleInterop: true,
    },
    fileName: relativePath,
    reportDiagnostics: true,
  });
}

function assertNoSyntacticDiagnostics(result, surface) {
  const diagnostics = result.diagnostics ?? [];
  const errors = diagnostics.filter(
    (diagnostic) => diagnostic.category === ts.DiagnosticCategory.Error,
  );
  assert.equal(
    errors.length,
    0,
    `${surface} had syntax errors: ${errors
      .map((diagnostic) => ts.flattenDiagnosticMessageText(diagnostic.messageText, "\n"))
      .join("; ")}`,
  );
}

function loadWikiApi() {
  const calls = [];
  const compilation = {
    id: "compilation-a",
    source_snapshot_id: "snapshot-a",
    source_snapshot_hash: "a".repeat(64),
    identity_hash: "b".repeat(64),
    compiler_spec: {},
    status: "READY",
    attempt_count: 1,
    error_code: null,
    content_hash: "c".repeat(64),
    telemetry: {},
    created_at: "2026-11-05T00:00:00Z",
    published_at: null,
    published_by: null,
  };
  const page = {
    page_key: "page-a",
    title: "Page A",
    kind: "concept",
    markdown: "# Page A",
    claims: [],
    relations: [],
  };
  const query = {
    binding: {
      schema_version: 1,
      workspace_id: auth.workspaceId,
      knowledge_base_id: "kb/a",
      compilation_id: compilation.id,
      compilation_hash: compilation.content_hash,
      excluded_revision_ids: [],
      source_snapshot_id: compilation.source_snapshot_id,
      source_snapshot_hash: compilation.source_snapshot_hash,
      compilation_status: "PUBLISHED",
      require_current: true,
      validated: true,
    },
    wiki_pages: [page],
    evidence: [],
    route: "WIKI",
    intent: "overview",
    insufficient_evidence: false,
    supplemental_required: false,
    conflicts: [],
    answer: "Source excerpt",
  };
  const responses = [
    [compilation],
    compilation,
    compilation,
    [page],
    [],
    { ...compilation, status: "PUBLISHED" },
    { ...compilation, status: "PENDING" },
    query,
  ];
  class ApiError extends Error {
    constructor(code, message, status) {
      super(message);
      this.code = code;
      this.status = status;
    }
  }
  const client = {
    ApiError,
    apiRequest: async (requestPath, accessToken, init) => {
      calls.push({ path: requestPath, accessToken, init });
      return responses.shift();
    },
  };
  const tenancy = {
    listEnvelope: (payload) =>
      Array.isArray(payload)
        ? payload
        : payload && Array.isArray(payload.items)
          ? payload.items
          : null,
  };
  const compiled = transpile("lib/api/wiki.ts");
  assertNoSyntacticDiagnostics(compiled, "wiki API client");
  const module = { exports: {} };
  new Function("require", "module", "exports", compiled.outputText)(
    (name) => {
      if (name === "@/lib/api/client") return client;
      if (name === "@/lib/api/tenancy") return tenancy;
      throw new Error(`Unexpected API-client import: ${name}`);
    },
    module,
    module.exports,
  );
  return { api: module.exports, calls, ApiError };
}

async function testApiContract() {
  const { api, calls } = loadWikiApi();
  const knowledgeBaseId = "kb/a";
  const compilationId = "compilation/a";

  await api.listCompilations(auth, knowledgeBaseId);
  await api.getCompilation(auth, knowledgeBaseId, compilationId);
  await api.createCompilation(auth, knowledgeBaseId, {
    model_profile_id: "model-a",
    source_snapshot_id: "snapshot-a",
    review_mode: "MODEL",
    output_mode: "JSON_SCHEMA",
  });
  await api.getCompilationPages(auth, knowledgeBaseId, compilationId);
  await api.getCompilationLint(auth, knowledgeBaseId, compilationId);
  await api.publishCompilation(auth, knowledgeBaseId, compilationId);
  await api.retryCompilation(auth, knowledgeBaseId, compilationId);
  await api.queryWikiCompilation(auth, knowledgeBaseId, compilationId, {
    source_snapshot_id: "snapshot-a",
    query: "What changed?",
    mode: "HYBRID",
    intent: "fact",
    require_current: false,
    model_profile_id: "model-a",
  });

  const prefix =
    "/api/v1/workspaces/workspace%2Fa/knowledge-bases/kb%2Fa/wiki/compilations";
  const item = `${prefix}/compilation%2Fa`;
  assert.deepEqual(
    calls.map((call) => call.path),
    [
      prefix,
      item,
      prefix,
      `${item}/pages`,
      `${item}/lint`,
      `${item}/publish`,
      `${item}/retry`,
      `${item}/query`,
    ],
  );
  assert.ok(calls.every((call) => call.accessToken === "token-a"));

  assert.deepEqual(calls[2].init, {
    method: "POST",
    body: {
      model_profile_id: "model-a",
      source_snapshot_id: "snapshot-a",
      output_mode: "JSON_SCHEMA",
      review_mode: "MODEL",
    },
  });
  assert.deepEqual(calls[5].init, { method: "POST", body: {} });
  assert.deepEqual(calls[6].init, { method: "POST", body: {} });
  assert.deepEqual(calls[7].init, {
    method: "POST",
    body: {
      source_snapshot_id: "snapshot-a",
      query: "What changed?",
      mode: "HYBRID",
      intent: "fact",
      require_current: false,
      model_profile_id: "model-a",
    },
  });
  console.log("PASS Wiki API paths, encoded identifiers, auth token and request bodies");
}

function componentHarness() {
  const states = [];
  const refs = [];
  let stateIndex = 0;
  let refIndex = 0;
  const calls = [];
  const reloads = [];
  const session = {
    connected: true,
    workspaceId: "workspace-a",
    accessToken: "token-a",
    sessionId: 1,
    permissions: ["knowledge_run", "knowledge_edit"],
  };
  const i18n = {
    locale: "en-US",
    t: (key) => key,
    formatDateTime: (value) => value,
    formatNumber: (value) => String(value),
    statusLabel: (value) => value,
  };
  let compilationStatus = "READY";
  const compilation = () => ({
    id: "compilation-a",
    source_snapshot_id: "snapshot-a",
    source_snapshot_hash: "a".repeat(64),
    identity_hash: "b".repeat(64),
    compiler_spec: {},
    status: compilationStatus,
    attempt_count: 1,
    error_code: compilationStatus === "FAILED" ? "WIKI_COMPILE_FAILED" : null,
    content_hash: compilationStatus === "PUBLISHED" ? "c".repeat(64) : null,
    telemetry: {},
    created_at: "2026-11-05T00:00:00Z",
    published_at: compilationStatus === "PUBLISHED" ? "2026-11-05T01:00:00Z" : null,
    published_by: null,
  });
  const queryResult = {
    binding: {
      schema_version: 1,
      workspace_id: "workspace-a",
      knowledge_base_id: "kb-a",
      compilation_id: "compilation-a",
      compilation_hash: "c".repeat(64),
      excluded_revision_ids: [],
      source_snapshot_id: "snapshot-a",
      source_snapshot_hash: "a".repeat(64),
      compilation_status: "PUBLISHED",
      require_current: true,
      validated: true,
    },
    wiki_pages: [],
    evidence: [],
    route: "WIKI",
    intent: "overview",
    insufficient_evidence: false,
    supplemental_required: false,
    conflicts: [],
    answer: "Fresh server-backed answer",
  };
  const wikiApi = new Proxy(
    {
      publishCompilation: async (...args) => {
        calls.push({ method: "publishCompilation", args });
        return { ...compilation(), status: "PUBLISHED" };
      },
      retryCompilation: async (...args) => {
        calls.push({ method: "retryCompilation", args });
        return { ...compilation(), status: "PENDING" };
      },
      getCompilation: async (...args) => {
        calls.push({ method: "getCompilation", args });
        return {
          ...compilation(),
          status: calls.some((call) => call.method === "publishCompilation")
            ? "PUBLISHED"
            : compilationStatus === "FAILED"
              ? "PENDING"
              : compilationStatus,
        };
      },
      queryWikiCompilation: async (...args) => {
        calls.push({ method: "queryWikiCompilation", args });
        return queryResult;
      },
    },
    {
      get(target, key) {
        if (key === "__esModule") return true;
        if (key in target) return target[key];
        return async (...args) => {
          calls.push({ method: key, args });
          return [];
        };
      },
    },
  );
  const knowledgeApi = {
    listKnowledgeBases: async () => [
      {
        id: "kb-a",
        workspace_id: "workspace-a",
        name: "Knowledge A",
        created_at: "2026-11-05T00:00:00Z",
      },
    ],
    listSnapshots: async () => [],
  };
  const modelApi = {
    listModelProfiles: async () => [
      {
        id: "model-a",
        workspace_id: "workspace-a",
        provider_credential_id: "credential-a",
        model: "model-a",
        temperature: 0,
        max_tokens: 1000,
        timeout_seconds: 30,
        fallback_profile_id: null,
        capabilities: {},
        enabled: true,
      },
    ],
  };

  const mocks = {
    react: {
      ...React,
      useState(initial) {
        const index = stateIndex++;
        if (!(index in states)) {
          states[index] = typeof initial === "function" ? initial() : initial;
        }
        return [
          states[index],
          (value) => {
            states[index] = typeof value === "function" ? value(states[index]) : value;
          },
        ];
      },
      useRef(initial) {
        const index = refIndex++;
        return (refs[index] ??= { current: initial });
      },
      useEffect() {},
      useCallback: (callback) => callback,
    },
    "@/components/providers/session-provider": {
      useFrontendSession: () => session,
    },
    "@/i18n/provider": { useI18n: () => i18n },
    "@/hooks/use-workspace-data": {
      useWorkspaceData(load, resourceKey, options) {
        let data = [];
        if (resourceKey.startsWith("wiki-bases:")) {
          data = [
            {
              id: "kb-a",
              workspace_id: "workspace-a",
              name: "Knowledge A",
              created_at: "2026-11-05T00:00:00Z",
            },
          ];
        } else if (resourceKey.startsWith("wiki-model-profiles:")) {
          data = [
            {
              id: "model-a",
              workspace_id: "workspace-a",
              provider_credential_id: "credential-a",
              model: "model-a",
              temperature: 0,
              max_tokens: 1000,
              timeout_seconds: 30,
              fallback_profile_id: null,
              capabilities: {},
              enabled: true,
            },
          ];
        } else if (resourceKey.startsWith("wiki-compilations:")) {
          data = [compilation()];
        } else if (resourceKey.startsWith("wiki-pages:")) {
          data = [];
        } else if (resourceKey.startsWith("wiki-lint:")) {
          data = [];
        }
        return {
          data,
          error: null,
          loading: false,
          loaded: options?.enabled !== false,
          reload: () => reloads.push(resourceKey),
        };
      },
      useWorkspaceMutation() {
        return {
          pending: false,
          error: null,
          clearError() {},
          async run(mutate) {
            return mutate({
              workspaceId: session.workspaceId,
              accessToken: session.accessToken,
            });
          },
        };
      },
    },
    "@/lib/api/client": {
      ApiError: class ApiError extends Error {},
      errorHintKey: () => null,
    },
    "@/lib/api/knowledge": knowledgeApi,
    "@/lib/api/models": modelApi,
    "@/lib/api/wiki": wikiApi,
    "next/link": { __esModule: true, default: "a" },
  };

  const compiled = transpile("app/knowledge/wiki/page.tsx");
  assertNoSyntacticDiagnostics(compiled, "Wiki workbench page");
  const module = { exports: {} };
  new Function("require", "module", "exports", compiled.outputText)(
    (name) => {
      if (name in mocks) return mocks[name];
      if (name.startsWith("@/")) {
        return new Proxy(
          {},
          {
            get(_, key) {
              if (key === "__esModule") return true;
              if (key === "default") return key === "default" ? "component" : key;
              return String(key);
            },
          },
        );
      }
      return requireWeb(name);
    },
    module,
    module.exports,
  );

  function render(elementType, props) {
    stateIndex = 0;
    refIndex = 0;
    return elementType(props);
  }

  return {
    Page: module.exports.default,
    calls,
    reloads,
    render,
    refs,
    session,
    states,
    setCompilationStatus(status) {
      compilationStatus = status;
    },
  };
}

function elements(node, predicate, found = []) {
  if (Array.isArray(node)) {
    node.forEach((child) => elements(child, predicate, found));
    return found;
  }
  if (!React.isValidElement(node)) return found;
  if (predicate(node)) found.push(node);
  for (const value of Object.values(node.props)) elements(value, predicate, found);
  return found;
}

function prepareContent(h, permissions, status, overrides = {}) {
  h.session.workspaceId = "workspace-a";
  h.session.accessToken = "token-a";
  h.session.sessionId = 1;
  h.session.permissions = permissions;
  h.setCompilationStatus(status);
  h.states[0] = "kb-a";
  h.states[1] = "compilation-a";
  h.states[2] = "pages";
  h.states[3] = null;
  h.states[4] = null;
  h.states[5] = false;
  h.states[6] = false;
  h.states[7] = "model-a";
  h.states[8] = "LATEST";
  h.states[9] = "BASIC";
  h.states[10] = "PROMPT_JSON";
  h.states[11] = "";
  h.states[12] = "AUTO";
  h.states[13] = "overview";
  h.states[14] = true;
  h.states[15] = false;
  h.states[16] = "model-a";
  h.states[17] = null;
  h.states[18] = 1;
  for (const [index, value] of Object.entries(overrides)) h.states[Number(index)] = value;
  const wrapper = h.render(h.Page, {});
  return { wrapper, tree: h.render(wrapper.type, {}) };
}

async function testComponentContracts() {
  const h = componentHarness();

  let view = prepareContent(h, null, "READY");
  assert.ok(
    elements(view.tree, (node) => node.type === "LoadingState").length > 0,
    "unknown permissions show a loading state",
  );
  assert.equal(
    elements(view.tree, (node) => node.props?.children === "New compilation").length,
    0,
  );
  console.log("PASS permission loading never exposes Wiki mutations");

  h.calls.length = 0;
  view = prepareContent(h, ["knowledge_run"], "PUBLISHED");
  assert.equal(
    elements(view.tree, (node) => node.props?.children === "New compilation").length,
    0,
    "read-only users cannot create compilations",
  );
  assert.equal(
    elements(view.tree, (node) => node.props?.children === "Publish…").length,
    0,
    "read-only users cannot publish",
  );
  assert.ok(
    elements(view.tree, (node) => node.props?.children === "Query").length > 0,
    "knowledge_run exposes the published query tab",
  );
  assert.equal(h.calls.length, 0, "rendering a published Wiki never mutates it");
  console.log("PASS read-only knowledge_run can query but cannot create or publish");

  view = prepareContent(h, ["knowledge_run", "knowledge_edit"], "READY");
  assert.ok(
    elements(view.tree, (node) => node.props?.children === "New compilation").length > 0,
  );
  const publishButton = elements(
    view.tree,
    (node) => node.type === "button" && node.props?.children === "Publish…",
  )[0];
  assert.ok(publishButton, "an editor gets the explicit Publish control");
  await publishButton.props.onClick();
  assert.equal(h.states[6], true, "the Publish control opens explicit confirmation");
  view = prepareContent(h, ["knowledge_run", "knowledge_edit"], "READY", { 6: true });
  const confirm = elements(view.tree, (node) => node.props?.confirmLabel === "Publish")[0];
  assert.ok(confirm, "publishing requires a second explicit confirmation");
  assert.equal(h.calls.length, 0, "opening publication confirmation performs no write");
  await confirm.props.onConfirm();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(
    h.calls.map((call) => call.method),
    ["publishCompilation", "getCompilation"],
    "publication is followed by a server-state readback",
  );
  console.log("PASS publication is explicit and confirmed by a post-write GET");

  h.calls.length = 0;
  view = prepareContent(h, ["knowledge_run", "knowledge_edit"], "FAILED");
  const retryButton = elements(
    view.tree,
    (node) => node.type === "button" && node.props?.children === "Retry failed compilation",
  )[0];
  assert.ok(retryButton);
  await retryButton.props.onClick();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(
    h.calls.map((call) => call.method),
    ["retryCompilation", "getCompilation"],
    "retry is followed by a server-state readback",
  );
  console.log("PASS failed compilation retry reads back the accepted job state");

  h.calls.length = 0;
  view = prepareContent(h, ["knowledge_run"], "PUBLISHED", {
    2: "query",
    11: "Which policy applies?",
  });
  const queryButton = elements(
    view.tree,
    (node) => node.type === "button" && node.props?.children === "Run query",
  )[0];
  assert.ok(queryButton, "read-only users get the query action");
  const queryForm = elements(view.tree, (node) => node.type === "form")[0];
  assert.ok(queryForm);
  await queryForm.props.onSubmit({ preventDefault() {} });
  const queryCall = h.calls.find((call) => call.method === "queryWikiCompilation");
  assert.ok(queryCall);
  assert.deepEqual(queryCall.args[0], {
    workspaceId: "workspace-a",
    accessToken: "token-a",
  });
  assert.equal(queryCall.args[3].source_snapshot_id, "snapshot-a");
  assert.equal(queryCall.args[3].query, "Which policy applies?");
  assert.equal(queryCall.args[3].require_current, true);
  console.log("PASS query uses the current session token and frozen snapshot");

  view = prepareContent(h, ["knowledge_run"], "PUBLISHED", {
    2: "query",
    11: "prior query",
    17: {
      query: "prior query",
      mode: "AUTO",
      intent: "overview",
      requireCurrent: true,
      generatedModelId: "",
      result: {
        binding: {},
        wiki_pages: [],
        evidence: [],
        route: "WIKI",
        intent: "overview",
        insufficient_evidence: false,
        supplemental_required: false,
        conflicts: [],
        answer: "Prior result must disappear",
      },
    },
  });
  h.refs[0].current = "token-a";
  h.session.accessToken = "token-b";
  const refreshedTree = h.render(view.wrapper.type, {});
  assert.ok(
    elements(
      refreshedTree,
      (node) =>
        node.type === "LoadingState" &&
        node.props?.label === "Refreshing workspace session…",
    ).length > 0,
  );
  assert.equal(
    elements(
      refreshedTree,
      (node) => node.props?.text === "Prior result must disappear",
    ).length,
    0,
    "token expiry hides prior content while permissions are unavailable",
  );
  console.log("PASS token-expiry permission reset cannot surface stale query content");

  h.session.permissions = ["knowledge_run"];
  h.session.sessionId = 1;
  h.session.workspaceId = "workspace-a";
  const firstWorkspace = h.render(h.Page, {});
  h.session.sessionId = 2;
  h.session.workspaceId = "workspace-b";
  const nextWorkspace = h.render(h.Page, {});
  assert.notEqual(
    firstWorkspace.key,
    nextWorkspace.key,
    "workspace/session switches remount the client state",
  );
  console.log("PASS workspace switches invalidate all Wiki workbench state");
}

async function main() {
  await testApiContract();
  await testComponentContracts();
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
