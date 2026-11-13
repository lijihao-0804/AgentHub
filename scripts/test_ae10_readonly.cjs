// Run from the repository root. Exercise the actual evaluation authoring TSX.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const Module = require("node:module");
const path = require("node:path");

const web = path.resolve("apps/web");
const requireWeb = Module.createRequire(path.join(web, "package.json"));
const ts = requireWeb("typescript");
const React = requireWeb("react");

function createHarness() {
  const states = [];
  const refs = [];
  const calls = [];
  let stateIndex = 0;
  let refIndex = 0;
  const session = {
    connected: true,
    workspaceId: "workspace-a",
    accessToken: "token",
    sessionId: 1,
    permissions: [],
  };

  const api = new Proxy(
    {},
    {
      get: (_target, key) => {
        if (key === "__esModule") return true;
        return async (...args) => {
          calls.push({ method: String(key), args });
          if (String(key).startsWith("list")) return [];
          if (String(key).startsWith("get")) return {};
          return {};
        };
      },
    },
  );

  const mocks = {
    react: {
      ...React,
      useState(initial) {
        const index = stateIndex++;
        if (!(index in states)) {
          states[index] =
            typeof initial === "function" ? initial() : initial;
        }
        return [
          states[index],
          (value) => {
            states[index] =
              typeof value === "function" ? value(states[index]) : value;
          },
        ];
      },
      useRef(initial) {
        const index = refIndex++;
        return (refs[index] ??= { current: initial });
      },
      useEffect() {},
      useCallback: (fn) => fn,
      useMemo: (fn) => fn(),
    },
    "@/components/providers/session-provider": {
      useFrontendSession: () => session,
    },
    "@/i18n/provider": {
      useI18n: () => ({
        locale: "en-US",
        t: (key) => key,
        formatDateTime: String,
        formatNumber: String,
        statusLabel: String,
        purposeLabel: String,
      }),
    },
    "@/hooks/use-workspace-data": {
      useWorkspaceData: () => ({
        data: { name: "Dataset A" },
        loaded: true,
        loading: false,
        error: null,
        reload: async () => {},
      }),
    },
    "@/lib/api/evaluation": api,
    "@/lib/api/client": {
      ApiError: Error,
      toApiError: (error) => error,
    },
    "next/navigation": {
      useRouter: () => ({ push() {} }),
    },
    "next/link": {
      __esModule: true,
      default: "a",
    },
  };

  function unresolvedModule() {
    return new Proxy(
      {},
      {
        get: (_target, key) => {
          if (key === "__esModule") return true;
          if (key === "default") return "section";
          return String(key);
        },
      },
    );
  }

  function load(relativePath) {
    const filename = path.join(web, relativePath);
    const compiled = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
      compilerOptions: {
        target: ts.ScriptTarget.ES2022,
        module: ts.ModuleKind.CommonJS,
        jsx: ts.JsxEmit.ReactJSX,
      },
    }).outputText;
    const module = { exports: {} };
    new Function("require", "module", "exports", compiled)(
      (name) => {
        if (name in mocks) return mocks[name];
        if (name.startsWith("@/")) return unresolvedModule();
        return requireWeb(name);
      },
      module,
      module.exports,
    );
    return module.exports;
  }

  return {
    calls,
    load,
    render(component, props) {
      stateIndex = 0;
      refIndex = 0;
      return component(props);
    },
    session,
    states,
  };
}

function elements(node, predicate, found = []) {
  if (Array.isArray(node)) {
    for (const child of node) elements(child, predicate, found);
    return found;
  }
  if (!React.isValidElement(node)) return found;
  if (predicate(node)) found.push(node);
  elements(node.props.children, predicate, found);
  elements(node.props.actions, predicate, found);
  return found;
}

function buttonsWithLabel(tree, label) {
  return elements(
    tree,
    (node) => node.type === "button" && node.props.children === label,
  );
}

function containsText(node, expected) {
  if (typeof node === "string" || typeof node === "number") {
    return String(node).includes(expected);
  }
  if (Array.isArray(node)) {
    return node.some((child) => containsText(child, expected));
  }
  return React.isValidElement(node) && containsText(node.props.children, expected);
}

function assertReadableWithoutManagement({
  component,
  createLabel,
  expectedText,
  loadedState,
  props,
  resultState,
  states,
}) {
  const h = createHarness();
  h.states[resultState] = states.result;
  h.states[loadedState] = true;
  const Component = h.load(component).default;

  for (const permissions of [
    null,
    [],
    ["evaluation_read"],
    ["evaluation_run"],
  ]) {
    h.session.permissions = permissions;
    const tree = h.render(Component, props);
    assert.equal(
      buttonsWithLabel(tree, createLabel).length,
      0,
      `${component}: ${String(permissions)} must not render the management button`,
    );
    assert.equal(
      elements(tree, (node) => node.type === "form").length,
      0,
      `${component}: ${String(permissions)} must not render a management form`,
    );
    assert.equal(
      elements(tree, (node) => node.type === "table").length,
      1,
      `${component}: authorized results must remain readable`,
    );
    assert.ok(
      containsText(tree, expectedText),
      `${component}: authorized result text must remain visible`,
    );
  }
  assert.equal(h.calls.length, 0, `${component}: render must not invoke a mutation`);

  h.session.permissions = ["evaluation_manage"];
  const managedTree = h.render(Component, props);
  const [createButton] = buttonsWithLabel(managedTree, createLabel);
  assert.ok(createButton, `${component}: evaluation_manage must expose the management button`);
  createButton.props.onClick();
  const openTree = h.render(Component, props);
  assert.equal(
    elements(openTree, (node) => node.type === "form").length +
      elements(
        openTree,
        (node) => node.props?.title === "evaluation.datasets.version.publishConfirmTitle",
      ).length,
    1,
    `${component}: evaluation_manage must expose its authoring control`,
  );

  h.session.permissions = ["evaluation_read"];
  const revokedTree = h.render(Component, props);
  assert.equal(
    elements(revokedTree, (node) => node.type === "form").length +
      elements(
        revokedTree,
        (node) => node.props?.title === "evaluation.datasets.version.publishConfirmTitle",
      ).length,
    0,
    `${component}: removing evaluation_manage must close the authoring control`,
  );
}

const cases = [
  {
    component: "app/evaluations/experiments/page.tsx",
    createLabel: "evaluation.experiments.create",
    expectedText: "Experiment A",
    resultState: 0,
    loadedState: 7,
    props: undefined,
    states: {
      result: [
        {
          id: "experiment-a",
          name: "Experiment A",
          status: "READY",
          split: "DEV",
          purpose: "DEVELOPMENT",
          repetitions: 1,
          build_sha: "0123456789",
          holdout_exposure_count: 0,
          created_at: "2026-11-04",
        },
      ],
    },
  },
  {
    component: "app/evaluations/datasets/page.tsx",
    createLabel: "evaluation.datasets.create",
    expectedText: "Dataset A",
    resultState: 0,
    loadedState: 7,
    props: undefined,
    states: {
      result: [
        {
          id: "dataset-a",
          name: "Dataset A",
          description: null,
          created_at: "2026-11-04",
        },
      ],
    },
  },
  {
    component: "app/evaluations/datasets/[datasetId]/dataset-detail-client.tsx",
    createLabel: "evaluation.datasets.versions.create",
    expectedText: "Dataset A",
    resultState: 0,
    loadedState: 3,
    props: { datasetId: "dataset-a" },
    states: {
      result: [
        {
          id: "version-a",
          version_number: 1,
          status: "DRAFT",
          item_count: 1,
          content_hash: "hash",
          created_at: "2026-11-04",
          published_at: null,
        },
      ],
    },
  },
  {
    component:
      "app/evaluations/datasets/[datasetId]/versions/[versionId]/version-detail-client.tsx",
    createLabel: "evaluation.datasets.version.publish",
    expectedText: "case-a",
    resultState: 0,
    loadedState: 3,
    props: { datasetId: "dataset-a", versionId: "version-a" },
    states: {
      result: {
        id: "version-a",
        version_number: 1,
        schema_version: 1,
        content_hash: "hash",
        status: "DRAFT",
        item_count: 1,
        created_at: "2026-11-04",
        published_at: null,
        items: [
          {
            id: "item-a",
            category: "task",
            split: "DEV",
            ordinal: 0,
            case_key: "case-a",
            input: { value: "safe" },
            expected: {},
          },
        ],
      },
    },
  },
  {
    component: "app/evaluations/pricing/page.tsx",
    createLabel: "evaluation.pricing.create",
    expectedText: "Pricing A",
    resultState: 0,
    loadedState: 3,
    props: undefined,
    states: {
      result: [
        {
          id: "pricing-a",
          name: "Pricing A",
          provider: "provider",
          model: "model",
          currency: "USD",
          input_price_per_1m: "1.00",
          output_price_per_1m: "2.00",
          cached_input_price_per_1m: null,
          content_hash: "hash",
          effective_at: "2026-11-04",
        },
      ],
    },
  },
  {
    component: "app/evaluations/release-gates/page.tsx",
    createLabel: "evaluation.gates.create",
    expectedText: "Gate A",
    resultState: 0,
    loadedState: 3,
    props: undefined,
    states: {
      result: [
        {
          id: "gate-a",
          name: "Gate A",
          description: null,
          policy_json: { rules: [] },
          policy_hash: "hash",
          created_at: "2026-11-04",
        },
      ],
    },
  },
];

for (const testCase of cases) {
  assertReadableWithoutManagement(testCase);
  console.log(`PASS ${testCase.component}`);
}

const handlerChecks = [
  {
    file: "apps/web/app/evaluations/experiments/page.tsx",
    handler: "function submitCreate",
    mutation: "createExperiment(",
  },
  {
    file: "apps/web/app/evaluations/datasets/page.tsx",
    handler: "function submitCreate",
    mutation: "createDataset(",
  },
  {
    file: "apps/web/app/evaluations/datasets/[datasetId]/dataset-detail-client.tsx",
    handler: "function submitVersion",
    mutation: "createDatasetVersion(",
  },
  {
    file:
      "apps/web/app/evaluations/datasets/[datasetId]/versions/[versionId]/version-detail-client.tsx",
    handler: "function publish",
    mutation: "publishDatasetVersion(",
  },
  {
    file: "apps/web/app/evaluations/pricing/page.tsx",
    handler: "function submitCreate",
    mutation: "createPricingSnapshot(",
  },
  {
    file: "apps/web/app/evaluations/release-gates/page.tsx",
    handler: "function submitCreate",
    mutation: "createReleaseGatePolicy(",
  },
];

for (const check of handlerChecks) {
  const source = fs.readFileSync(check.file, "utf8");
  assert.ok(
    source.includes(
      'const canManage = permissions?.includes("evaluation_manage") === true;',
    ),
    `${check.file}: management permission must be explicit and strict`,
  );
  const handlerStart = source.indexOf(check.handler);
  const guard = source.indexOf("if (!canManage) return;", handlerStart);
  const mutation = source.indexOf(check.mutation, handlerStart);
  assert.ok(handlerStart >= 0, `${check.file}: handler not found`);
  assert.ok(
    guard > handlerStart && guard < mutation,
    `${check.file}: handler must reject before calling the mutation`,
  );
}
console.log("PASS management handlers enforce evaluation_manage before mutation");
