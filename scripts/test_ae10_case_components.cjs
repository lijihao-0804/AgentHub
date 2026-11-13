// Run from the repository root:
// node scripts/test_ae10_case_components.cjs
//
// This renderer is intentionally dependency-free. Shared presentation modules
// are mocked, while the two AE-10 component files and their safe view helpers
// are transpiled and executed as the actual TS/TSX modules.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');

const webRoot = path.resolve('apps/web');
const webRequire = Module.createRequire(path.join(webRoot, 'package.json'));
const ts = webRequire('typescript');
const React = webRequire('react');
const { renderToStaticMarkup } = webRequire('react-dom/server');
const jsxRuntime = webRequire('react/jsx-runtime');
const jsxDevRuntime = webRequire('react/jsx-dev-runtime');

class ApiError extends Error {
  constructor(code, message, status) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
    this.status = status;
  }
}

function resolveModuleFile(candidate) {
  const attempts = [
    candidate,
    `${candidate}.tsx`,
    `${candidate}.ts`,
    `${candidate}.jsx`,
    `${candidate}.js`,
    path.join(candidate, 'index.tsx'),
    path.join(candidate, 'index.ts'),
  ];
  return attempts.find((entry) => fs.existsSync(entry) && fs.statSync(entry).isFile()) ?? null;
}

function createRuntime(world) {
  const runtime = {
    states: [],
    refs: [],
    effects: [],
    cleanups: [],
    stateIndex: 0,
    refIndex: 0,
    render(component, props) {
      world.runtime = runtime;
      runtime.stateIndex = 0;
      runtime.refIndex = 0;
      runtime.effects = [];
      return component(props);
    },
    runEffects() {
      const effects = runtime.effects.slice();
      runtime.effects = [];
      const cleanups = effects.map((effect) => effect());
      runtime.cleanups.push(...cleanups.filter((cleanup) => typeof cleanup === 'function'));
      return cleanups;
    },
    cleanup() {
      while (runtime.cleanups.length > 0) {
        const cleanup = runtime.cleanups.pop();
        cleanup();
      }
    },
  };
  return runtime;
}

function createWorld() {
  const world = {
    runtime: null,
    locale: 'en-US',
    session: {
      connected: true,
      workspaceId: 'workspace-a',
      accessToken: 'token-a',
      sessionId: 1,
      permissions: [],
    },
    listCalls: [],
    evidenceCalls: [],
    listImpl: async () => ({ total: 0, items: [] }),
    evidenceImpl: async () => {
      throw new ApiError('EVALUATION_CASE_RESULT_NOT_FOUND', 'Case evidence not found.', 404);
    },
  };

  const ReactMock = {
    ...React,
    useState(initial) {
      const index = world.runtime.stateIndex++;
      if (!(index in world.runtime.states)) {
        world.runtime.states[index] =
          typeof initial === 'function' ? initial() : initial;
      }
      return [
        world.runtime.states[index],
        (next) => {
          world.runtime.states[index] =
            typeof next === 'function' ? next(world.runtime.states[index]) : next;
        },
      ];
    },
    useRef(initial) {
      const index = world.runtime.refIndex++;
      if (!(index in world.runtime.refs)) {
        world.runtime.refs[index] = { current: initial };
      }
      return world.runtime.refs[index];
    },
    useCallback(callback) {
      return callback;
    },
    useMemo(factory) {
      return factory();
    },
    useEffect(effect) {
      world.runtime.effects.push(effect);
    },
  };

  function Panel({ title, eyebrow, actions, children, ariaLabel }) {
    return React.createElement(
      'section',
      { 'data-testid': 'panel', 'aria-label': ariaLabel },
      eyebrow ? React.createElement('p', { className: 'eyebrow' }, eyebrow) : null,
      title ? React.createElement('h2', null, title) : null,
      actions ? React.createElement('div', { 'data-testid': 'panel-actions' }, actions) : null,
      children,
    );
  }

  function LoadingState({ label }) {
    return React.createElement(
      'div',
      { 'data-testid': 'loading-state' },
      label ?? 'Loading',
    );
  }

  function EmptyState({ title, hint }) {
    return React.createElement(
      'div',
      { 'data-testid': 'empty-state' },
      React.createElement('p', null, title),
      hint ? React.createElement('p', null, hint) : null,
    );
  }

  function ErrorState({ code, message, onRetry }) {
    const canRetry = typeof onRetry === 'function';
    return React.createElement(
      'div',
      {
        'data-testid': 'error-state',
        'data-code': code,
        'data-can-retry': canRetry ? 'yes' : 'no',
      },
      React.createElement('p', null, message),
      canRetry
        ? React.createElement(
            'button',
            { type: 'button', onClick: onRetry },
            'Retry',
          )
        : null,
    );
  }

  function InlineError({ error, fallback }) {
    if (!error) return null;
    return React.createElement(
      'p',
      { 'data-testid': 'inline-error', 'data-code': error.code },
      error.message || fallback,
    );
  }

  function SessionRequired() {
    return React.createElement(
      'div',
      { 'data-testid': 'session-required' },
      'A workspace session is required.',
    );
  }

  function StatusBadge({ status, label, tone }) {
    return React.createElement(
      'span',
      {
        'data-testid': 'status-badge',
        'data-status': status,
        'data-tone': tone ?? '',
      },
      label ?? status,
    );
  }

  function HashValue({ value }) {
    return React.createElement('code', { 'data-testid': 'hash-value' }, value ?? '—');
  }

  const statesMock = {
    EmptyState,
    ErrorState,
    InlineError,
    LoadingState,
    Panel,
    SessionRequired,
  };
  const mocks = {
    react: ReactMock,
    'react/jsx-runtime': jsxRuntime,
    'react/jsx-dev-runtime': jsxDevRuntime,
    'next/link': { __esModule: true, default: 'a' },
    '@/components/providers/session-provider': {
      useFrontendSession: () => world.session,
    },
    '@/i18n/provider': {
      useI18n: () => ({
        locale: world.locale,
        t: (key) => key,
        statusLabel: (status) => status,
        formatCount: (value) => String(value),
      }),
    },
    '@/components/ui/states': statesMock,
    '@/components/ui/status-badge': { __esModule: true, default: StatusBadge },
    '@/components/evaluation/hash-value': { __esModule: true, default: HashValue },
    '@/lib/api/client': {
      ApiError,
      toApiError(error, fallback) {
        if (error instanceof ApiError) return error;
        return new ApiError(
          'REQUEST_FAILED',
          error && error.message ? error.message : fallback,
          0,
        );
      },
    },
    '@/lib/api/evaluation': {
      listExperimentRunCaseResults: (...args) => {
        world.listCalls.push(args);
        return world.listImpl(...args);
      },
      getExperimentRunCaseEvidence: (...args) => {
        world.evidenceCalls.push(args);
        return world.evidenceImpl(...args);
      },
    },
  };

  const cache = new Map();
  function loadModule(relativeOrAbsolute) {
    const candidate = path.isAbsolute(relativeOrAbsolute)
      ? relativeOrAbsolute
      : path.join(webRoot, relativeOrAbsolute);
    const filename = resolveModuleFile(candidate);
    if (!filename) throw new Error(`Cannot resolve TS module: ${candidate}`);
    if (cache.has(filename)) return cache.get(filename).exports;

    const compiled = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
      fileName: filename,
      compilerOptions: {
        target: ts.ScriptTarget.ES2022,
        module: ts.ModuleKind.CommonJS,
        jsx: ts.JsxEmit.ReactJSX,
        esModuleInterop: true,
      },
    }).outputText;
    const moduleRecord = { exports: {} };
    cache.set(filename, moduleRecord);
    const localRequire = (name) => {
      if (Object.prototype.hasOwnProperty.call(mocks, name)) return mocks[name];
      if (name.startsWith('@/')) return loadModule(path.join(webRoot, name.slice(2)));
      if (name.startsWith('.')) return loadModule(path.resolve(path.dirname(filename), name));
      return webRequire(name);
    };
    new Function('require', 'module', 'exports', compiled)(
      localRequire,
      moduleRecord,
      moduleRecord.exports,
    );
    return moduleRecord.exports;
  }

  world.load = loadModule;
  world.mocks = { ...mocks, ErrorState, StatusBadge, statesMock };
  return world;
}

function elements(node, predicate, found = []) {
  if (Array.isArray(node)) {
    node.forEach((child) => elements(child, predicate, found));
    return found;
  }
  if (!React.isValidElement(node)) return found;
  if (predicate(node)) found.push(node);
  for (const value of Object.values(node.props ?? {})) {
    if (React.isValidElement(value) || Array.isArray(value)) {
      elements(value, predicate, found);
    }
  }
  return found;
}

function textContent(node) {
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(textContent).join(' ');
  if (!React.isValidElement(node)) return '';
  return textContent(node.props.children);
}

function buttonByText(tree, label) {
  return elements(
    tree,
    (node) => node.type === 'button' && textContent(node) === label,
  )[0];
}

function roleSelect(tree) {
  return elements(tree, (node) => node.type === 'select')[0];
}

function rowButton(tree) {
  return elements(
    tree,
    (node) =>
      node.type === 'button' &&
      String(node.props.className ?? '').includes('case-evidence-row-select'),
  )[0];
}

function rowButtonForCase(tree, caseKey) {
  return elements(
    tree,
    (node) =>
      node.type === 'button' &&
      String(node.props.className ?? '').includes('case-evidence-row-select') &&
      textContent(node).includes(caseKey),
  )[0];
}

function html(tree) {
  return renderToStaticMarkup(tree);
}

function detailElement(world, tree) {
  const detailsModule = world.load('components/evaluation/case-evidence-details.tsx');
  return elements(
    tree,
    (node) => node.type === detailsModule.CaseEvidenceDetails,
  )[0];
}

function detailTree(world, tree) {
  const element = detailElement(world, tree);
  assert.ok(
    element,
    `selected case must render the actual details component; functions=${elements(
      tree,
      (node) => typeof node.type === 'function',
    )
      .map((node) => node.type.name || '(anonymous)')
      .join(',')} detailsDefault=${typeof world.load(
      'components/evaluation/case-evidence-details.tsx',
    ).default}`,
  );
  const detailsModule = world.load('components/evaluation/case-evidence-details.tsx');
  assert.equal(element.type, detailsModule.CaseEvidenceDetails);
  return element.type(element.props);
}

const settle = async () => {
  await Promise.resolve();
  await new Promise((resolve) => setImmediate(resolve));
  await Promise.resolve();
};

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

function mount(world, props = {}) {
  const runtime = createRuntime(world);
  const component = world.load('components/evaluation/run-case-evidence.tsx');
  const outer = runtime.render(component.RunCaseEvidence, {
    runId: 'run-a',
    variants: [{ id: 'variant-a', label: 'Candidate' }],
    ...props,
  });
  if (!React.isValidElement(outer) || outer.type === world.mocks.statesMock.SessionRequired) {
    return { runtime, outer, disconnected: true, tree: outer };
  }
  const inner = outer.type;
  const innerProps = outer.props;
  let currentInnerProps = innerProps;
  let tree = runtime.render(inner, innerProps);
  const mounted = {
    runtime,
    outer,
    disconnected: false,
    inner,
    innerProps,
    tree,
    rerender() {
      tree = runtime.render(inner, currentInnerProps);
      mounted.tree = tree;
      return mounted.tree;
    },
    rerenderWith(nextProps) {
      currentInnerProps = { ...currentInnerProps, ...nextProps };
      mounted.innerProps = currentInnerProps;
      tree = runtime.render(inner, currentInnerProps);
      mounted.tree = tree;
      return mounted.tree;
    },
  };
  return mounted;
}

async function mountReady(world, props = {}) {
  const mounted = mount(world, props);
  const cleanups = mounted.runtime.runEffects();
  await settle();
  mounted.rerender();
  mounted.cleanups = cleanups;
  return mounted;
}

async function openCase(world, mounted) {
  const button = rowButton(mounted.tree);
  assert.ok(button, 'case row must be selectable');
  button.props.onClick();
  await settle();
  return (mounted.tree = mounted.rerender());
}

function caseItem(id, overrides = {}) {
  return {
    id,
    status: 'SUCCEEDED',
    observed_agent_status: 'SUCCEEDED',
    case_execution_key: `fallback-${id}`,
    experiment_variant_id: 'variant-a',
    repetition_index: 0,
    agent_run_id: `run-${id}`,
    cost_amount: null,
    cost_currency: null,
    failure_code: null,
    observed_agent_failure_code: null,
    safe_failure_message: null,
    observation: { prompt: 'private prompt', private_facts: ['private fact'] },
    ...overrides,
  };
}

function caseEvidence(id, overrides = {}) {
  return {
    schema_version: 1,
    case_id: id,
    case_key: `case-${id}`,
    category: 'TOOLS',
    variant_label: 'Candidate',
    task_success: { status: 'AVAILABLE', value: 1, reason: null },
    evidence_level: 'CONTROLLED',
    execution_cleanup_status: 'TEARDOWN_COMPLETE',
    termination_reason: 'goal_met',
    episode: {
      episode_id: `episode-${id}`,
      execution_status: 'COMPLETED',
      cleanup_status: 'TEARDOWN_COMPLETE',
      cleanup_error: null,
      termination_reason: 'goal_met',
      thread_id: `thread-${id}`,
      observation_hash: 'a'.repeat(64),
      assertion_hash: 'b'.repeat(64),
      turns: [
        { turn_index: 0, agent_run_id: `run-${id}`, status: 'SUCCEEDED' },
      ],
    },
    steps: [],
    failure_labels: [],
    ...overrides,
  };
}

async function testLoadingAndEmpty() {
  const world = createWorld();
  const pending = deferred();
  world.listImpl = () => pending.promise;
  const mounted = mount(world);

  const loadingHtml = html(mounted.tree);
  assert.ok(loadingHtml.includes('Loading case results'));
  const cleanups = mounted.runtime.runEffects();
  await settle();
  mounted.rerender();
  assert.ok(html(mounted.tree).includes('Loading case results'));

  pending.resolve({ total: 0, items: [] });
  await settle();
  mounted.rerender();
  const emptyHtml = html(mounted.tree);
  assert.ok(emptyHtml.includes('No case results were returned'), emptyHtml);
  assert.ok(emptyHtml.includes('does not prove there were no violations'));
  cleanups.forEach((cleanup) => typeof cleanup === 'function' && cleanup());
  mounted.runtime.cleanup();

  console.log('PASS loading and empty states render from the actual component');
}

async function testErrorStates() {
  for (const [code, status] of [
    ['FORBIDDEN', 403],
    ['EVALUATION_EXPERIMENT_RUN_NOT_FOUND', 404],
  ]) {
    const world = createWorld();
    world.listImpl = async () => {
      throw new ApiError(code, `${code} from API`, status);
    };
    const mounted = await mountReady(world);
    const error = elements(mounted.tree, (node) => node.type === world.mocks.ErrorState)[0];
    assert.ok(error);
    assert.equal(error.props.code, code);
    assert.equal(error.props.onRetry, undefined, '403/404 must not offer a retry action');
    assert.ok(html(mounted.tree).includes(code));
    mounted.runtime.cleanup();
    console.log(`PASS ${status} ${code} state without retry`);
  }

  const world = createWorld();
  world.listImpl = async () => {
    throw new Error('socket closed');
  };
  const mounted = await mountReady(world);
  const error = elements(mounted.tree, (node) => node.type === world.mocks.ErrorState)[0];
  assert.ok(error);
  assert.equal(error.props.code, 'REQUEST_FAILED');
  assert.equal(typeof error.props.onRetry, 'function');
  const errorHtml = html(mounted.tree);
  assert.ok(errorHtml.includes('socket closed'));
  assert.ok(errorHtml.includes('>Retry</button>'));
  mounted.runtime.cleanup();
  console.log('PASS non-authorization API failure remains visible and manually retryable');
}

async function testLegacyMissingTrajectoryIsNotCleanEvidence() {
  const world = createWorld();
  world.listImpl = async () => ({
    total: 1,
    items: [caseItem('legacy')],
  });
  world.evidenceImpl = async () => ({
    case_id: 'legacy',
    observation: { prompt: 'private prompt' },
    private_facts: ['private fact'],
    arguments: { secret: true },
  });

  const mounted = await mountReady(world);
  await openCase(world, mounted);
  const detailsHtml = html(detailTree(world, mounted.tree));
  assert.ok(detailsHtml.includes('Older or incomplete evidence protocol'));
  assert.ok(detailsHtml.includes('No episode record was returned'));
  assert.ok(detailsHtml.includes('No safe steps were returned'));
  assert.ok(detailsHtml.includes('Missing labels do not prove that no violation occurred'));
  assert.ok(!detailsHtml.includes('private prompt'));
  assert.ok(!detailsHtml.includes('private fact'));
  assert.ok(!detailsHtml.includes('secret'));
  mounted.runtime.cleanup();
  console.log('PASS legacy/missing trajectory remains unavailable, not inferred clean');
}

async function testSafeListSummariesAndIdentityPrecedence() {
  const world = createWorld();
  world.listImpl = async () => ({
    total: 4,
    items: [
      caseItem('summary-a', {
        case_key: 'SAFE-CASE-A',
        category: 'BOOKING',
        variant_label: 'Summary Variant A',
        task_success: { status: 'AVAILABLE', value: 1, reason: null },
      }),
      caseItem('summary-b', {
        case_key: 'SUMMARY-CASE-B',
        category: 'SUMMARY-CATEGORY-B',
        variant_label: 'Summary Variant B',
        task_success: { status: 'AVAILABLE', value: 0, reason: 'assertion_failed' },
      }),
      caseItem('summary-c', {
        case_key: 'SAFE-CASE-C',
        category: 'POLICY',
        variant_label: 'Summary Variant C',
        task_success: { status: 'UNKNOWN', value: null, reason: 'missing_assertion' },
        observation: {
          case_key: 'PRIVATE-OBSERVATION-CASE',
          category: 'PRIVATE-OBSERVATION-CATEGORY',
          variant_label: 'Private observation variant',
          task_success: { status: 'AVAILABLE', value: 1, reason: null },
        },
      }),
      caseItem('legacy-summary', {
        case_execution_key: 'LEGACY-FALLBACK-KEY',
      }),
    ],
  });
  world.evidenceImpl = async (_auth, _runId, caseId) => {
    if (caseId === 'summary-a') {
      return caseEvidence(caseId, {
        case_key: null,
        category: null,
        variant_label: null,
        task_success: {
          status: 'NOT_AVAILABLE',
          value: null,
          reason: 'older_detail_protocol',
        },
      });
    }
    if (caseId === 'summary-b') {
      return caseEvidence(caseId, {
        case_key: 'EVIDENCE-CASE-B',
        category: 'EVIDENCE-CATEGORY-B',
        variant_label: 'Evidence Variant B',
        task_success: { status: 'AVAILABLE', value: 1, reason: null },
      });
    }
    throw new ApiError('UNEXPECTED_CASE', caseId, 500);
  };

  const mounted = await mountReady(world);
  const initialHtml = html(mounted.tree);
  assert.equal(
    elements(
      mounted.tree,
      (node) =>
        node.type === 'button' &&
        String(node.props.className ?? '').includes('case-evidence-row-select'),
    ).length,
    4,
  );
  assert.ok(initialHtml.includes('SAFE-CASE-A'));
  assert.ok(initialHtml.includes('SUMMARY-CASE-B'));
  assert.ok(initialHtml.includes('SAFE-CASE-C'));
  assert.ok(initialHtml.includes('LEGACY-FALLBACK-KEY'));
  assert.ok(initialHtml.includes('BOOKING'));
  assert.ok(initialHtml.includes('SUMMARY-CATEGORY-B'));
  assert.ok(initialHtml.includes('POLICY'));
  assert.ok(initialHtml.includes('Summary Variant A'));
  assert.ok(initialHtml.includes('Success (value 1)'));
  assert.ok(initialHtml.includes('Failure (value 0)'));
  assert.ok(initialHtml.includes('Unknown'));
  assert.ok(initialHtml.includes('Candidate'), 'old summary fields still use the variant fallback');
  const legacyRow = elements(
    mounted.tree,
    (node) =>
      node.type === 'tr' &&
      textContent(node).includes('LEGACY-FALLBACK-KEY'),
  )[0];
  assert.ok(legacyRow);
  assert.ok(
    elements(
      legacyRow,
      (node) =>
        node.type === world.mocks.StatusBadge &&
        node.props.status === 'NOT_LOADED',
    ).length > 0,
    'a row without old summary fields keeps the not-loaded fallback',
  );
  assert.ok(!initialHtml.includes('PRIVATE-OBSERVATION-CASE'));
  assert.ok(!initialHtml.includes('PRIVATE-OBSERVATION-CATEGORY'));
  assert.ok(!initialHtml.includes('Private observation variant'));
  assert.equal(world.evidenceCalls.length, 0, 'first-screen identity must not require detail requests');

  const firstButton = rowButtonForCase(mounted.tree, 'SAFE-CASE-A');
  assert.ok(firstButton);
  firstButton.props.onClick();
  await settle();
  mounted.rerender();
  const retainedSummaryHtml = html(mounted.tree);
  const retainedDetailHtml = html(detailTree(world, mounted.tree));
  assert.ok(retainedSummaryHtml.includes('SAFE-CASE-A'));
  assert.ok(retainedSummaryHtml.includes('BOOKING'));
  assert.ok(retainedSummaryHtml.includes('Summary Variant A'));
  assert.ok(retainedDetailHtml.includes('SAFE-CASE-A'));
  assert.ok(retainedDetailHtml.includes('BOOKING'));
  assert.ok(retainedDetailHtml.includes('Summary Variant A'));
  assert.ok(retainedDetailHtml.includes('Success (value 1)'));
  assert.ok(!retainedDetailHtml.includes('case-summary-a'));

  const secondButton = rowButtonForCase(mounted.tree, 'SUMMARY-CASE-B');
  assert.ok(secondButton);
  secondButton.props.onClick();
  await settle();
  mounted.rerender();
  const evidenceFirstHtml = html(mounted.tree);
  assert.ok(evidenceFirstHtml.includes('EVIDENCE-CASE-B'));
  assert.ok(evidenceFirstHtml.includes('EVIDENCE-CATEGORY-B'));
  assert.ok(evidenceFirstHtml.includes('Evidence Variant B'));
  assert.ok(evidenceFirstHtml.includes('Success (value 1)'));
  assert.ok(!evidenceFirstHtml.includes('SUMMARY-CASE-B'));
  assert.ok(!evidenceFirstHtml.includes('SUMMARY-CATEGORY-B'));
  assert.ok(!evidenceFirstHtml.includes('Summary Variant B'));
  assert.equal(world.evidenceCalls.length, 2);
  mounted.runtime.cleanup();
  console.log('PASS safe list summaries identify four cases immediately and evidence overrides or falls back without raw observation reads');
}

async function testTaskZeroAndStepBoundaries() {
  const world = createWorld();
  world.listImpl = async () => ({
    total: 1,
    items: [
      caseItem('task-zero', {
        status: 'SUCCEEDED',
        observed_agent_status: 'SUCCEEDED',
        cost_amount: 0,
        cost_currency: 'CNY',
      }),
    ],
  });
  world.evidenceImpl = async () =>
    caseEvidence('task-zero', {
      case_key: 'case-alpha',
      category: 'TOOLS',
      task_success: { status: 'AVAILABLE', value: 0, reason: 'assertion_failed' },
      steps: [
        {
          step_order: 1,
          step_type: 'TOOL_PROPOSAL',
          agent_run_id: 'run-task-zero',
          logical_action_id: 'action-1',
          arguments_hash: 'c'.repeat(64),
          status: 'SUCCEEDED',
          error_code: null,
          tool_name: 'write_ticket',
          policy_decision: null,
        },
        {
          step_order: 2,
          step_type: 'POLICY',
          agent_run_id: 'run-task-zero',
          logical_action_id: 'action-2',
          arguments_hash: null,
          status: 'DENIED',
          error_code: 'APPROVAL_REJECTED',
          tool_name: 'write_ticket',
          policy_decision: 'DENIED',
        },
        {
          step_order: 3,
          step_type: 'TOOL_EXECUTE',
          agent_run_id: 'run-task-zero',
          logical_action_id: 'action-3',
          arguments_hash: 'd'.repeat(64),
          status: 'FAILED',
          error_code: 'TOOL_FAILED',
          tool_name: 'write_ticket',
          policy_decision: 'APPROVED',
        },
      ],
      failure_labels: [{ code: 'TOOL_EXECUTION_FAILED', step_orders: [3] }],
    });

  const mounted = await mountReady(world);
  const initialHtml = html(mounted.tree);
  assert.ok(initialHtml.includes('Completed'));
  assert.ok(initialHtml.includes('Recorded pricing estimate (not a bill)'));
  assert.ok(initialHtml.includes('Pricing source not recorded'));
  assert.ok(initialHtml.includes('0 CNY'));

  await openCase(world, mounted);
  const detailHtml = html(detailTree(world, mounted.tree));
  const fullHtml = html(mounted.tree);
  assert.ok(detailHtml.includes('Failure (value 0)'));
  assert.ok(detailHtml.includes('value: 0'));
  assert.ok(!detailHtml.includes('Success (value 1)'));
  assert.ok(detailHtml.includes('Proposed'));
  assert.ok(detailHtml.includes('Policy/approval rejected'));
  assert.ok(detailHtml.includes('Execution failed'));
  assert.ok(!detailHtml.includes('Execution step recorded'));
  assert.ok(detailHtml.includes('These labels locate related steps'));
  assert.ok(detailHtml.includes('/runs/run-task-zero'));
  assert.ok(fullHtml.includes('case-alpha'));
  assert.ok(fullHtml.includes('TOOLS'));
  mounted.runtime.cleanup();
  console.log('PASS task=0 differs from execution success; proposal, rejection and execution are distinct');
}

async function testPricingEstimateCopyAndUnknownCost() {
  const world = createWorld();
  world.listImpl = async () => ({
    total: 2,
    items: [
      caseItem('known-pricing', {
        cost_amount: '1.25',
        cost_currency: 'CNY',
      }),
      caseItem('unknown-pricing', {
        cost_amount: null,
        cost_currency: 'CNY',
      }),
    ],
  });
  world.evidenceImpl = async (_auth, _runId, caseId) =>
    caseEvidence(caseId);

  const mounted = await mountReady(world);
  const englishListHtml = html(mounted.tree);
  assert.ok(englishListHtml.includes('Recorded pricing estimate (not a bill)'));
  assert.ok(englishListHtml.includes('Pricing source not recorded'));
  assert.ok(englishListHtml.includes('UNKNOWN'));
  assert.ok(englishListHtml.includes('Not zero; not free'));
  assert.ok(!englishListHtml.includes('Recorded amount (not a bill)'));

  await openCase(world, mounted);
  const englishDetailHtml = html(detailTree(world, mounted.tree));
  assert.ok(englishDetailHtml.includes('1.25 CNY'));
  assert.ok(englishDetailHtml.includes('Recorded pricing estimate (not a bill)'));
  assert.ok(englishDetailHtml.includes('Pricing source not recorded.'));
  assert.ok(!englishDetailHtml.includes('Recorded amount (not a bill)'));

  world.locale = 'zh-CN';
  mounted.rerender();
  const chineseHtml = html(mounted.tree);
  const chineseDetailHtml = html(detailTree(world, mounted.tree));
  assert.ok(chineseHtml.includes('记录的计价估算（非账单）'));
  assert.ok(chineseHtml.includes('费用来源未记录'));
  assert.ok(chineseHtml.includes('不等于零，也不表示免费'));
  assert.ok(chineseDetailHtml.includes('1.25 CNY'));
  assert.ok(chineseDetailHtml.includes('记录的计价估算（非账单）'));
  assert.ok(chineseDetailHtml.includes('费用来源未记录。'));
  assert.ok(!chineseHtml.includes('实付'));
  assert.ok(!chineseDetailHtml.includes('实付'));
  mounted.runtime.cleanup();
  console.log('PASS known amount is a pricing estimate, missing source is explicit, and UNKNOWN is not zero');
}

async function testApprovalExecutionFallbackSteps() {
  const world = createWorld();
  world.listImpl = async () => ({
    total: 1,
    items: [caseItem('approval-execution')],
  });
  world.evidenceImpl = async () =>
    caseEvidence('approval-execution', {
      steps: [
        {
          step_order: 1,
          step_type: 'APPROVAL_EXECUTION',
          agent_run_id: 'run-approval-execution',
          logical_action_id: null,
          arguments_hash: null,
          status: 'SUCCEEDED',
          error_code: null,
          tool_name: 'approve_ticket',
          policy_decision: 'APPROVED',
        },
        {
          step_order: 2,
          step_type: 'APPROVAL_EXECUTION',
          agent_run_id: 'run-approval-execution',
          logical_action_id: null,
          arguments_hash: null,
          status: 'FAILED',
          error_code: 'APPROVAL_EXECUTION_FAILED',
          tool_name: 'approve_ticket',
          policy_decision: 'APPROVED',
        },
        {
          step_order: 3,
          step_type: 'APPROVAL_EXECUTION',
          agent_run_id: 'run-approval-execution',
          logical_action_id: null,
          arguments_hash: null,
          status: 'UNKNOWN_OUTCOME',
          error_code: 'APPROVAL_EXECUTION_OUTCOME_UNKNOWN',
          tool_name: 'approve_ticket',
          policy_decision: 'APPROVED',
        },
      ],
    });

  const mounted = await mountReady(world);
  await openCase(world, mounted);
  const tree = detailTree(world, mounted.tree);
  const stepTrees = elements(
    tree,
    (node) =>
      node.type === 'li' &&
      String(node.props.className ?? '').includes('case-evidence-step'),
  );
  assert.equal(stepTrees.length, 3);
  assert.ok(
    stepTrees.some((step) =>
      String(step.props.className ?? '').includes('case-evidence-step-executed'),
    ),
  );
  assert.ok(
    stepTrees.some((step) =>
      String(step.props.className ?? '').includes('case-evidence-step-failed_execution'),
    ),
  );
  assert.ok(
    stepTrees.some((step) =>
      String(step.props.className ?? '').includes('case-evidence-step-unknown_outcome'),
    ),
  );
  const englishHtml = html(tree);
  assert.ok(englishHtml.includes('APPROVAL_EXECUTION'));
  assert.ok(
    englishHtml.includes(
      'APPROVAL_EXECUTION comes from a persisted approval execution record and may have no corresponding ACTION step.',
    ),
  );
  assert.ok(englishHtml.includes('Execution step recorded'));
  assert.ok(englishHtml.includes('Execution failed'));
  assert.ok(englishHtml.includes('UNKNOWN_OUTCOME'));

  world.locale = 'zh-CN';
  mounted.rerender();
  const chineseHtml = html(detailTree(world, mounted.tree));
  assert.ok(
    chineseHtml.includes(
      'APPROVAL_EXECUTION 来自持久审批执行记录，可能没有对应 ACTION 步骤。',
    ),
  );
  mounted.runtime.cleanup();
  console.log('PASS persisted approval execution fallback steps retain success, failure, and unknown semantics');
}

async function testAmbiguousApprovalWaitsWithFallbackFact() {
  const world = createWorld();
  world.listImpl = async () => ({
    total: 1,
    items: [caseItem('approval-waits')],
  });
  world.evidenceImpl = async () =>
    caseEvidence('approval-waits', {
      steps: [
        {
          step_order: 5,
          step_type: 'APPROVAL_WAIT',
          agent_run_id: 'run-approval-waits',
          logical_action_id: null,
          arguments_hash: null,
          status: 'PENDING',
          error_code: null,
          tool_name: 'approve_ticket',
          policy_decision: 'APPROVED',
        },
        {
          step_order: 5,
          step_type: 'APPROVAL_WAIT',
          agent_run_id: 'run-approval-waits',
          logical_action_id: null,
          arguments_hash: null,
          status: 'PENDING',
          error_code: null,
          tool_name: 'approve_ticket',
          policy_decision: 'APPROVED',
        },
        {
          step_order: 0,
          step_type: 'APPROVAL_EXECUTION',
          agent_run_id: 'run-approval-waits',
          logical_action_id: 'approval-action-fact',
          arguments_hash: '6'.repeat(64),
          status: 'UNKNOWN_OUTCOME',
          error_code: 'APPROVAL_EXECUTION_OUTCOME_UNKNOWN',
          tool_name: 'approve_ticket',
          policy_decision: 'APPROVED',
        },
      ],
      failure_labels: [
        { code: 'WAIT_PAIR_AMBIGUOUS', step_orders: [5] },
        { code: 'APPROVAL_FACT_UNKNOWN', step_orders: [0] },
      ],
    });

  const mounted = await mountReady(world);
  await openCase(world, mounted);
  const tree = detailTree(world, mounted.tree);
  const stepTrees = elements(
    tree,
    (node) =>
      node.type === 'li' &&
      String(node.props.className ?? '').includes('case-evidence-step'),
  );
  assert.equal(stepTrees.length, 3);
  assert.equal(
    stepTrees.filter((step) => textContent(step).includes('APPROVAL_WAIT')).length,
    2,
  );
  const approvalExecution = stepTrees.find((step) =>
    textContent(step).includes('APPROVAL_EXECUTION'),
  );
  assert.ok(approvalExecution);
  assert.ok(
    String(approvalExecution.props.className ?? '').includes(
      'case-evidence-step-unknown_outcome',
    ),
  );
  assert.ok(
    textContent(approvalExecution).includes(
      'Approval execution record (no step ordinal)',
    ),
  );
  for (const step of stepTrees) {
    assert.equal(
      elements(
        step,
        (node) => String(node.props.className ?? '').includes('case-evidence-label'),
      ).length,
      0,
      'step_order 0 and duplicate wait orders must not receive inline failure labels',
    );
  }
  const englishHtml = html(tree);
  assert.ok(englishHtml.includes('List position cannot be used to infer exact execution timing.'));
  assert.ok(!englishHtml.includes('Step 0'));
  assert.ok(englishHtml.includes('approval-action-fact'));
  assert.ok(englishHtml.includes('666666'));

  world.locale = 'zh-CN';
  mounted.rerender();
  const chineseHtml = html(detailTree(world, mounted.tree));
  assert.ok(chineseHtml.includes('审批执行记录（无步骤序号）'));
  assert.ok(chineseHtml.includes('列表位置不能用于推断精确执行时序。'));
  assert.ok(!chineseHtml.includes('步骤 0'));
  mounted.runtime.cleanup();
  console.log('PASS two ambiguous approval waits and one fallback fact avoid invented pairing, labels, and step zero');
}

async function testPerCallStatusesAndAmbiguousFailureLabels() {
  const world = createWorld();
  world.listImpl = async () => ({
    total: 1,
    items: [caseItem('per-call')],
  });
  world.evidenceImpl = async () =>
    caseEvidence('per-call', {
      steps: [
        {
          step_order: 7,
          step_type: 'TOOL_EXECUTE',
          agent_run_id: 'run-per-call',
          logical_action_id: 'action-reused',
          arguments_hash: '4'.repeat(64),
          status: 'SUCCEEDED',
          error_code: null,
          tool_name: 'write_ticket',
          policy_decision: 'APPROVED',
        },
        {
          step_order: 7,
          step_type: 'TOOL_EXECUTE',
          agent_run_id: 'run-per-call',
          logical_action_id: 'action-reused',
          arguments_hash: '4'.repeat(64),
          status: 'FAILED',
          error_code: 'TOOL_EXECUTION_FAILED',
          tool_name: 'write_ticket',
          policy_decision: 'APPROVED',
        },
        {
          step_order: 7,
          step_type: 'ACTION',
          agent_run_id: 'run-per-call-next-turn',
          logical_action_id: 'action-reused',
          arguments_hash: '5'.repeat(64),
          status: 'UNKNOWN_OUTCOME',
          error_code: 'ACTION_OUTCOME_UNKNOWN',
          tool_name: 'create_ticket',
          policy_decision: 'APPROVED',
        },
      ],
      failure_labels: [
        { code: 'TOOL_EXECUTION_FAILED', step_orders: [7] },
        { code: 'RULES_INDETERMINATE', step_orders: [] },
      ],
    });

  const mounted = await mountReady(world);
  await openCase(world, mounted);
  const tree = detailTree(world, mounted.tree);
  const stepTrees = elements(
    tree,
    (node) =>
      node.type === 'li' &&
      String(node.props.className ?? '').includes('case-evidence-step'),
  );
  assert.equal(stepTrees.length, 3);
  assert.equal(
    new Set(stepTrees.map((step) => String(step.key))).size,
    3,
    'duplicate step_order values still require unique React keys',
  );

  const successfulStep = stepTrees.find((step) =>
    String(step.props.className ?? '').includes('case-evidence-step-executed'),
  );
  const failedStep = stepTrees.find((step) =>
    String(step.props.className ?? '').includes('case-evidence-step-failed_execution'),
  );
  const unknownStep = stepTrees.find((step) =>
    String(step.props.className ?? '').includes('case-evidence-step-unknown_outcome'),
  );
  assert.ok(successfulStep, 'successful call keeps its execution success label');
  assert.ok(failedStep, 'FAILED call must have an execution failure label');
  assert.ok(unknownStep, 'UNKNOWN call keeps its unknown-outcome label');

  const evidenceBadge = (step) =>
    elements(
      step,
      (node) => node.type === world.mocks.StatusBadge,
    ).find((node) => String(node.props.status).startsWith('EVIDENCE_'));
  assert.equal(evidenceBadge(successfulStep).props.label, 'Execution step recorded');
  assert.equal(evidenceBadge(failedStep).props.label, 'Execution failed');
  assert.equal(evidenceBadge(unknownStep).props.label, 'UNKNOWN_OUTCOME');
  assert.equal(
    String(failedStep.props.className ?? '').includes('case-evidence-step-executed'),
    false,
  );
  assert.equal(
    String(unknownStep.props.className ?? '').includes('case-evidence-step-executed'),
    false,
  );
  assert.ok(
    elements(
      failedStep,
      (node) =>
        node.type === world.mocks.StatusBadge &&
        node.props.status === 'FAILED',
    ).length > 0,
    'the per-call FAILED status remains visible',
  );
  assert.ok(
    elements(
      failedStep,
      (node) =>
        node.type === world.mocks.StatusBadge &&
        node.props.status === 'EVIDENCE_FAILED_EXECUTION' &&
        node.props.tone === 'danger',
    ).length > 0,
    'the FAILED evidence classification has danger tone',
  );
  assert.ok(
    elements(
      unknownStep,
      (node) =>
        node.type === world.mocks.StatusBadge &&
        node.props.status === 'UNKNOWN_OUTCOME',
    ).length > 0,
    'the per-call UNKNOWN status remains visible',
  );
  assert.ok(
    elements(
      unknownStep,
      (node) =>
        node.type === world.mocks.StatusBadge &&
        node.props.status === 'EVIDENCE_UNKNOWN_OUTCOME' &&
        node.props.tone === 'attention',
    ).length > 0,
    'the unknown evidence classification has attention tone',
  );

  for (const step of stepTrees) {
    assert.equal(
      elements(
        step,
        (node) => String(node.props.className ?? '').includes('case-evidence-label'),
      ).length,
      0,
      'a duplicate step_order must not attach a failure label to every call',
    );
  }
  const detailHtml = html(tree);
  assert.ok(detailHtml.includes('TOOL_EXECUTION_FAILED'));
  assert.ok(detailHtml.includes('RULES_INDETERMINATE'));
  assert.ok(detailHtml.includes('Related step orders: Not recorded'));
  mounted.runtime.cleanup();
  console.log('PASS duplicate-order per-call FAILED/UNKNOWN statuses stay truthful and labels remain unambiguous');
}

async function testUnknownOutcome() {
  const world = createWorld();
  world.listImpl = async () => ({
    total: 1,
    items: [
      caseItem('unknown', {
        observed_agent_status: 'NEEDS_ATTENTION',
        failure_code: 'UNKNOWN_OUTCOME',
      }),
    ],
  });
  world.evidenceImpl = async () =>
    caseEvidence('unknown', {
      task_success: { status: 'AVAILABLE', value: 0, reason: 'side_effect_unknown' },
      execution_cleanup_status: 'CLEANUP_BLOCKED',
      termination_reason: 'UNKNOWN_OUTCOME',
      episode: {
        episode_id: 'episode-unknown',
        execution_status: 'NEEDS_ATTENTION',
        cleanup_status: 'CLEANUP_BLOCKED',
        cleanup_error: 'INTERACTIVE_CLEANUP_UNKNOWN_OUTCOME',
        termination_reason: 'UNKNOWN_OUTCOME',
        thread_id: 'thread-unknown',
        observation_hash: 'e'.repeat(64),
        assertion_hash: null,
        turns: [],
      },
      steps: [
        {
          step_order: 1,
          step_type: 'ACTION',
          agent_run_id: 'run-unknown',
          logical_action_id: 'action-unknown',
          arguments_hash: 'f'.repeat(64),
          status: 'UNKNOWN_OUTCOME',
          error_code: 'ACTION_OUTCOME_UNKNOWN',
          tool_name: 'create_ticket',
          policy_decision: 'APPROVED',
        },
      ],
      failure_labels: [{ code: 'UNKNOWN_OUTCOME', step_orders: [1] }],
    });

  const mounted = await mountReady(world);
  await openCase(world, mounted);
  const detailHtml = html(detailTree(world, mounted.tree));
  assert.ok(detailHtml.includes('UNKNOWN_OUTCOME'));
  assert.ok(detailHtml.includes('Unknown side effect'));
  assert.ok(detailHtml.includes('Do not retry it automatically'));
  assert.ok(detailHtml.includes('fixture for reconciliation'));
  assert.ok(!detailHtml.includes('recovered successfully'));
  assert.ok(!detailHtml.includes('>Retry</button>'));
  assert.ok(!detailHtml.includes('Force cleanup'));
  mounted.runtime.cleanup();
  console.log('PASS UNKNOWN_OUTCOME requires reconciliation and exposes no automatic retry/force cleanup');
}

async function testCleanupRecoveryStates() {
  const world = createWorld();
  world.listImpl = async () => ({
    total: 1,
    items: [
      caseItem('recovery', {
        status: 'FAILED',
        observed_agent_status: 'SUCCEEDED',
        failure_code: 'EVALUATION_INTERACTIVE_RECOVERY_REQUIRED',
      }),
    ],
  });
  world.evidenceImpl = async () =>
    caseEvidence('recovery', {
      task_success: { status: 'NOT_AVAILABLE', value: null, reason: 'interrupted' },
      execution_cleanup_status: 'TEARDOWN_FAILED',
      termination_reason: 'EVALUATION_INTERACTIVE_RECOVERY_REQUIRED',
      episode: {
        episode_id: 'episode-recovery',
        execution_status: 'RECOVERY_REQUIRED',
        cleanup_status: 'CLEANUP_BLOCKED',
        cleanup_error: 'INTERACTIVE_CLEANUP_UNKNOWN_OUTCOME',
        termination_reason: 'EVALUATION_INTERACTIVE_RECOVERY_REQUIRED',
        thread_id: 'thread-recovery',
        observation_hash: null,
        assertion_hash: '1'.repeat(64),
        turns: [
          {
            turn_index: 0,
            agent_run_id: 'run-recovery',
            status: 'SUCCEEDED',
          },
          { turn_index: 1, agent_run_id: null, status: 'RESERVED' },
        ],
      },
    });

  const mounted = await mountReady(world);
  await openCase(world, mounted);
  const tree = detailTree(world, mounted.tree);
  const detailHtml = html(tree);
  const badges = elements(
    tree,
    (node) => node.type === world.mocks.StatusBadge,
  ).map((node) => node.props);
  assert.ok(detailHtml.includes('Historical execution cleanup result'));
  assert.ok(detailHtml.includes('Current episode cleanup status'));
  assert.ok(detailHtml.includes('TEARDOWN_FAILED'));
  assert.ok(detailHtml.includes('CLEANUP_BLOCKED'));
  assert.ok(detailHtml.includes('RECOVERY_REQUIRED'));
  assert.ok(detailHtml.includes('EVALUATION_INTERACTIVE_RECOVERY_REQUIRED'));
  assert.ok(
    badges.some(
      (badge) => badge.status === 'TEARDOWN_FAILED' && badge.tone === 'danger',
    ),
  );
  assert.ok(
    badges.some(
      (badge) => badge.status === 'CLEANUP_BLOCKED' && badge.tone === 'attention',
    ),
  );
  mounted.runtime.cleanup();
  console.log('PASS historical teardown, current cleanup and recovery-required states stay separate');
}

async function testRetainedFinalObservation() {
  const world = createWorld();
  world.listImpl = async () => ({
    total: 1,
    items: [
      caseItem('final-observation', {
        status: 'SUCCEEDED',
        observed_agent_status: 'SUCCEEDED',
      }),
    ],
  });
  const observationHash = '2'.repeat(64);
  const assertionHash = '3'.repeat(64);
  world.evidenceImpl = async () =>
    caseEvidence('final-observation', {
      task_success: { status: 'AVAILABLE', value: 1, reason: null },
      episode: {
        episode_id: 'episode-final-observation',
        execution_status: 'COMPLETED',
        cleanup_status: 'TEARDOWN_COMPLETE',
        cleanup_error: null,
        termination_reason: 'goal_met',
        thread_id: 'thread-final-observation',
        observation_hash: observationHash,
        assertion_hash: assertionHash,
        turns: [
          {
            turn_index: 0,
            agent_run_id: 'run-final-observation',
            status: 'SUCCEEDED',
          },
        ],
      },
    });

  const mounted = await mountReady(world);
  await openCase(world, mounted);
  const detailHtml = html(detailTree(world, mounted.tree));
  assert.ok(detailHtml.includes('Success (value 1)'));
  assert.ok(detailHtml.includes('COMPLETED'));
  assert.ok(detailHtml.includes('TEARDOWN_COMPLETE'));
  assert.ok(detailHtml.includes(observationHash));
  assert.ok(detailHtml.includes(assertionHash));
  assert.ok(!detailHtml.includes('Unknown side effect'));

  const selectedButton = rowButton(mounted.tree);
  selectedButton.props.onClick();
  await settle();
  mounted.rerender();
  assert.equal(world.evidenceCalls.length, 1, 'opening a cached case must not refetch evidence');
  assert.equal(world.listCalls.length, 1, 'reading a final observation must not rerun the list');
  mounted.runtime.cleanup();
  console.log('PASS retained final observation renders hashes/statuses without refetch or rerun');
}

async function testPaginationExpiry() {
  const world = createWorld();
  const stalePage = deferred();
  const staleFirstPage = deferred();
  let unfilteredFirstRequests = 0;
  world.listImpl = async (_auth, _runId, options) => {
    if (options.offset === 50) return stalePage.promise;
    if (options.status === 'FAILED') {
      return {
        total: 1,
        items: [caseItem('filtered-failed', { status: 'FAILED' })],
      };
    }
    unfilteredFirstRequests += 1;
    if (unfilteredFirstRequests > 1) return staleFirstPage.promise;
    return {
      total: 100,
      items: Array.from({ length: 50 }, (_, index) => caseItem(`page-${index}`)),
    };
  };

  const mounted = await mountReady(world);
  const moreButton = buttonByText(mounted.tree, 'Load more');
  assert.ok(moreButton);
  moreButton.props.onClick();
  await settle();
  mounted.rerender();
  assert.equal(world.listCalls.length, 2);
  assert.deepEqual(world.listCalls[1][2], {
    status: undefined,
    limit: 50,
    offset: 50,
  });

  const refreshButton = buttonByText(mounted.tree, 'Refresh');
  assert.ok(refreshButton);
  refreshButton.props.onClick();
  await settle();
  mounted.rerender();
  assert.equal(world.listCalls.length, 3);
  assert.deepEqual(world.listCalls[2][2], {
    status: undefined,
    limit: 50,
    offset: 0,
  });

  const select = roleSelect(mounted.tree);
  assert.ok(select);
  const callsBeforeFilter = world.listCalls.length;
  select.props.onChange({ currentTarget: { value: 'FAILED' } });
  await settle();
  mounted.rerender();
  assert.equal(
    world.listCalls.length,
    callsBeforeFilter + 1,
    'changing the visible filter must issue one first-page request',
  );
  assert.ok(html(mounted.tree).includes('filtered-failed'));
  assert.equal(roleSelect(mounted.tree).props.value, 'FAILED');
  assert.deepEqual(world.listCalls[3][2], {
    status: 'FAILED',
    limit: 50,
    offset: 0,
  });

  stalePage.resolve({
    total: 100,
    items: [caseItem('stale-page-row')],
  });
  staleFirstPage.resolve({
    total: 100,
    items: [caseItem('stale-first-page-row')],
  });
  await settle();
  mounted.rerender();
  const afterStaleHtml = html(mounted.tree);
  assert.ok(afterStaleHtml.includes('filtered-failed'));
  assert.ok(!afterStaleHtml.includes('stale-page-row'));
  assert.ok(!afterStaleHtml.includes('stale-first-page-row'));
  mounted.runtime.cleanup();
  console.log('PASS stale first-page and pagination responses cannot overwrite a newer filter');
}

async function testTokenRenewalKeepsFilterAndExpiresStaleWork() {
  const world = createWorld();
  const staleFilteredFirstPage = deferred();
  const renewedFirstPage = deferred();
  const staleLoadMore = deferred();
  const renewedAfterLoadMore = deferred();
  const currentLoadMore = deferred();
  const staleDetail = deferred();
  let filteredFirstPageRequests = 0;
  const failedPage = (prefix, start = 0) =>
    Array.from({ length: 50 }, (_, index) =>
      caseItem(`${prefix}-${start + index}`, {
        status: 'FAILED',
        observed_agent_status: 'FAILED',
      }),
    );

  world.listImpl = async (auth, _runId, options) => {
    if (auth.accessToken === 'token-a') {
      if (options.status === 'FAILED' && options.offset === 0) {
        filteredFirstPageRequests += 1;
        if (filteredFirstPageRequests === 1) {
          return { total: 100, items: failedPage('token-a-filtered') };
        }
        return staleFilteredFirstPage.promise;
      }
      return { total: 1, items: [caseItem('initial-success')] };
    }
    if (auth.accessToken === 'token-b') {
      if (options.status !== 'FAILED') {
        throw new Error('token renewal must preserve the FAILED filter');
      }
      if (options.offset === 0) return renewedFirstPage.promise;
      if (options.offset === 50) return staleLoadMore.promise;
    }
    if (auth.accessToken === 'token-c') {
      if (options.status !== 'FAILED') {
        throw new Error('second token renewal must preserve the FAILED filter');
      }
      if (options.offset === 0) return renewedAfterLoadMore.promise;
      if (options.offset === 50) return currentLoadMore.promise;
    }
    throw new Error(`unexpected list request: ${auth.accessToken} ${JSON.stringify(options)}`);
  };
  world.evidenceImpl = async (auth) => {
    if (auth.accessToken === 'token-b') return staleDetail.promise;
    throw new Error(`unexpected evidence request: ${auth.accessToken}`);
  };

  const mounted = await mountReady(world);
  const select = roleSelect(mounted.tree);
  assert.ok(select);
  select.props.onChange({ currentTarget: { value: 'FAILED' } });
  await settle();
  mounted.rerender();
  assert.equal(roleSelect(mounted.tree).props.value, 'FAILED');
  assert.deepEqual(world.listCalls.at(-1)[2], {
    status: 'FAILED',
    limit: 50,
    offset: 0,
  });

  const refreshButton = buttonByText(mounted.tree, 'Refresh');
  assert.ok(refreshButton);
  refreshButton.props.onClick();
  await settle();
  mounted.rerender();
  assert.equal(world.listCalls.at(-1)[0].accessToken, 'token-a');
  assert.equal(world.listCalls.at(-1)[2].status, 'FAILED');

  mounted.rerenderWith({ accessToken: 'token-b' });
  const tokenBRunCleanups = mounted.runtime.runEffects();
  assert.equal(world.listCalls.at(-1)[0].accessToken, 'token-b');
  assert.deepEqual(world.listCalls.at(-1)[2], {
    status: 'FAILED',
    limit: 50,
    offset: 0,
  });
  renewedFirstPage.resolve({ total: 100, items: failedPage('token-b-filtered') });
  await settle();
  mounted.rerender();
  assert.equal(roleSelect(mounted.tree).props.value, 'FAILED');

  staleFilteredFirstPage.resolve({
    total: 1,
    items: [caseItem('stale-token-a-first-page')],
  });
  await settle();
  mounted.rerender();
  let currentHtml = html(mounted.tree);
  assert.ok(currentHtml.includes('token-b-filtered-0'));
  assert.ok(!currentHtml.includes('stale-token-a-first-page'));

  const moreButton = buttonByText(mounted.tree, 'Load more');
  assert.ok(moreButton);
  moreButton.props.onClick();
  await settle();
  mounted.rerender();
  assert.equal(world.listCalls.at(-1)[0].accessToken, 'token-b');
  assert.deepEqual(world.listCalls.at(-1)[2], {
    status: 'FAILED',
    limit: 50,
    offset: 50,
  });

  const firstRow = rowButton(mounted.tree);
  assert.ok(firstRow);
  firstRow.props.onClick();
  await settle();

  mounted.rerenderWith({ accessToken: 'token-c' });
  const tokenCRunCleanups = mounted.runtime.runEffects();
  assert.equal(world.listCalls.at(-1)[0].accessToken, 'token-c');
  assert.deepEqual(world.listCalls.at(-1)[2], {
    status: 'FAILED',
    limit: 50,
    offset: 0,
  });
  renewedAfterLoadMore.resolve({
    total: 100,
    items: failedPage('token-c-filtered'),
  });
  await settle();
  mounted.rerender();

  staleLoadMore.resolve({
    total: 100,
    items: [caseItem('stale-token-b-page')],
  });
  staleDetail.resolve(
    caseEvidence('token-b-filtered-0', { case_key: 'STALE-TOKEN-B-DETAIL' }),
  );
  await settle();
  mounted.rerender();
  currentHtml = html(mounted.tree);
  assert.ok(currentHtml.includes('token-c-filtered-0'));
  assert.ok(!currentHtml.includes('stale-token-b-page'));
  assert.ok(!currentHtml.includes('STALE-TOKEN-B-DETAIL'));

  const currentMoreButton = buttonByText(mounted.tree, 'Load more');
  assert.ok(currentMoreButton);
  assert.equal(currentMoreButton.props.disabled, false);
  currentMoreButton.props.onClick();
  await settle();
  mounted.rerender();
  assert.equal(world.listCalls.at(-1)[0].accessToken, 'token-c');
  assert.deepEqual(world.listCalls.at(-1)[2], {
    status: 'FAILED',
    limit: 50,
    offset: 50,
  });
  currentLoadMore.resolve({
    total: 100,
    items: failedPage('token-c-filtered', 50),
  });
  await settle();
  mounted.rerender();

  const caseStatuses = elements(
    mounted.tree,
    (node) => node.type === world.mocks.StatusBadge,
  ).map((node) => node.props.status);
  assert.ok(caseStatuses.includes('FAILED'));
  assert.ok(
    !caseStatuses.includes('SUCCEEDED'),
    'renewed filtered pagination must not mix in SUCCEEDED case rows',
  );
  tokenBRunCleanups.forEach((cleanup) => typeof cleanup === 'function' && cleanup());
  tokenCRunCleanups.forEach((cleanup) => typeof cleanup === 'function' && cleanup());
  mounted.runtime.cleanup();
  console.log('PASS token renewal keeps FAILED/offset pagination and expires stale page/detail work');
}

async function testSessionIsolationAndStaleDetail() {
  const keyWorld = createWorld();
  const keyRuntime = createRuntime(keyWorld);
  const KeyComponent = keyWorld.load('components/evaluation/run-case-evidence.tsx').RunCaseEvidence;
  const first = keyRuntime.render(KeyComponent, { runId: 'run-a', variants: [] });
  keyWorld.session = {
    ...keyWorld.session,
    sessionId: 2,
    workspaceId: 'workspace-b',
    accessToken: 'token-b',
  };
  const switched = keyRuntime.render(KeyComponent, { runId: 'run-a', variants: [] });
  const otherRun = keyRuntime.render(KeyComponent, { runId: 'run-b', variants: [] });
  const refreshed = keyRuntime.render(KeyComponent, {
    runId: 'run-a',
    variants: [],
    refreshKey: 'refresh-2',
  });
  assert.notEqual(first.key, switched.key, 'session/workspace switch must remount');
  assert.notEqual(switched.key, otherRun.key, 'run switch must remount');
  assert.notEqual(switched.key, refreshed.key, 'refresh identity change must remount');

  keyWorld.session.connected = false;
  const disconnected = keyRuntime.render(KeyComponent, { runId: 'run-a', variants: [] });
  assert.equal(disconnected.type, keyWorld.mocks.statesMock.SessionRequired);
  assert.equal(keyWorld.listCalls.length, 0);
  console.log('PASS session/workspace/run/refresh keys and signed-out session guard');

  const world = createWorld();
  const oldEvidence = deferred();
  world.listImpl = async (auth) => ({
    total: 1,
    items: [
      caseItem(auth.workspaceId === 'workspace-a' ? 'old-session' : 'new-session'),
    ],
  });
  world.evidenceImpl = async (auth, _runId, caseId) => {
    if (auth.workspaceId === 'workspace-a') return oldEvidence.promise;
    return caseEvidence(caseId, { case_key: 'new-session-case' });
  };

  const mounted = await mountReady(world);
  const firstCleanup = mounted.cleanups;
  const button = rowButton(mounted.tree);
  button.props.onClick();
  await settle();

  firstCleanup.forEach((cleanup) => typeof cleanup === 'function' && cleanup());
  const nextSessionProps = {
    sessionId: 2,
    workspaceId: 'workspace-b',
    accessToken: 'token-b',
  };
  mounted.rerenderWith(nextSessionProps);
  const nextCleanups = mounted.runtime.runEffects();
  await settle();
  mounted.rerender();
  assert.ok(html(mounted.tree).includes('new-session'));

  oldEvidence.resolve(
    caseEvidence('old-session', { case_key: 'stale-private-evidence' }),
  );
  await settle();
  mounted.rerender();
  const afterOldResponse = html(mounted.tree);
  assert.ok(afterOldResponse.includes('new-session'));
  assert.ok(!afterOldResponse.includes('stale-private-evidence'));
  nextCleanups.forEach((cleanup) => typeof cleanup === 'function' && cleanup());
  mounted.runtime.cleanup();
  console.log('PASS stale in-flight detail from a previous session cannot replace new-session results');
}

async function main() {
  await testLoadingAndEmpty();
  await testErrorStates();
  await testLegacyMissingTrajectoryIsNotCleanEvidence();
  await testSafeListSummariesAndIdentityPrecedence();
  await testTaskZeroAndStepBoundaries();
  await testPricingEstimateCopyAndUnknownCost();
  await testApprovalExecutionFallbackSteps();
  await testAmbiguousApprovalWaitsWithFallbackFact();
  await testPerCallStatusesAndAmbiguousFailureLabels();
  await testUnknownOutcome();
  await testCleanupRecoveryStates();
  await testRetainedFinalObservation();
  await testPaginationExpiry();
  await testTokenRenewalKeepsFilterAndExpiresStaleWork();
  await testSessionIsolationAndStaleDetail();
  console.log('PASS AE-10 case component render/async contract');
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
