// Run from the repository root. Render and exercise the actual evaluation components.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const web = path.resolve('apps/web');
const requireWeb = Module.createRequire(path.join(web, 'package.json'));
const ts = requireWeb('typescript');
const React = requireWeb('react');

function harness() {
  const states = [], refs = [], calls = [];
  let stateIndex = 0, refIndex = 0;
  const session = { connected: true, workspaceId: 'workspace-a', accessToken: 'token', sessionId: 1, permissions: [] };
  const api = new Proxy({}, { get: (_, key) => {
    if (key === '__esModule') return true;
    if (key === 'CANCELLABLE_RUN_STATUSES') return new Set(['QUEUED', 'RUNNING']);
    if (key === 'TERMINAL_RUN_STATUSES') return new Set(['SUCCEEDED', 'FAILED', 'CANCELLED']);
    return async (...args) => { calls.push({ method: key, args }); return {}; };
  } });
  const mocks = {
    react: { ...React,
      useState(initial) { const i = stateIndex++; if (!(i in states)) states[i] = typeof initial === 'function' ? initial() : initial; return [states[i], value => { states[i] = typeof value === 'function' ? value(states[i]) : value; }]; },
      useRef(initial) { return refs[refIndex++] ??= { current: initial }; },
      useEffect() {}, useCallback: fn => fn, useMemo: fn => fn(),
    },
    '@/components/providers/session-provider': { useFrontendSession: () => session },
    '@/i18n/provider': { useI18n: () => ({ locale: 'en-US', t: key => key, formatDateTime: String, formatNumber: String, statusLabel: String, purposeLabel: String }) },
    '@/hooks/use-workspace-data': { useWorkspaceData: () => ({ data: [], loaded: true, loading: false }) },
    '@/lib/api/evaluation': api,
    '@/lib/api/client': { ApiError: Error, toApiError: error => error },
    'next/navigation': { useRouter: () => ({ push() {} }) },
    'next/link': { __esModule: true, default: 'a' },
  };
  function load(relative) {
    const file = path.join(web, relative);
    const compiled = ts.transpileModule(fs.readFileSync(file, 'utf8'), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } }).outputText;
    // Expose the existing gate view only in the compiled test module.
    const testedCode = relative.endsWith('workflow-sections.tsx') ? compiled + '\nmodule.exports.gateView = GateDecisionView;' : compiled;
    const mod = { exports: {} };
    new Function('require', 'module', 'exports', testedCode)(name => {
      if (name in mocks) return mocks[name];
      if (name.startsWith('@/')) return new Proxy({}, { get: (_, key) => key === '__esModule' ? true : (key === 'default' ? 'section' : String(key)) });
      return requireWeb(name);
    }, mod, mod.exports);
    return mod.exports;
  }
  return { session, states, calls, load, render(component, props) { stateIndex = refIndex = 0; return component(props); } };
}
function elements(node, predicate, found = []) {
  if (Array.isArray(node)) { node.forEach(child => elements(child, predicate, found)); return found; }
  if (!React.isValidElement(node)) return found;
  if (predicate(node)) found.push(node);
  elements(node.props.children, predicate, found);
  return found;
}

async function main() {
  const h = harness();
  const Workflow = h.load('app/evaluations/runs/[runId]/workflow-sections.tsx').default;
  const props = { input: { ...h.session }, run: { id: 'run-a', purpose: 'DEVELOPMENT', split: 'DEV' }, variants: [], terminal: true };
  h.states[0] = { kind: 'notMaterialized' };
  for (const permissions of [null, [], ['evaluation_read'], ['evaluation_manage']]) {
    h.session.permissions = permissions;
    assert.equal(elements(h.render(Workflow, props), n => n.type === 'button').length, 0, 'read-only/unknown permissions cannot materialize metrics');
  }
  h.session.permissions = ['evaluation_read', 'evaluation_run'];
  const buttons = elements(h.render(Workflow, props), n => n.type === 'button');
  assert.equal(buttons.length, 1);
  await buttons[0].props.onClick();
  assert.equal(h.calls.filter(call => call.method === 'materializeExperimentRunMetrics').length, 1);
  console.log('PASS metrics mutation follows evaluation_run; null/read-only/manage-only roles cannot invoke it');

  const e = harness();
  const Experiment = e.load('app/evaluations/experiments/[experimentId]/experiment-detail-client.tsx').default;
  e.states[0] = { id: 'experiment-a', name: 'Frozen', status: 'READY', split: 'DEV', purpose: 'DEVELOPMENT', repetitions: 1, dataset_schema_version: 1, build_sha: '0123456789', dataset_content_hash: 'hash', dataset_id: 'dataset', dataset_version_id: 'version', spec_hash: 'hash', variants: [], created_at: '2026-11-04' };
  for (const permissions of [null, [], ['evaluation_read'], ['evaluation_manage']]) {
    e.session.permissions = permissions;
    const tree = e.render(Experiment, { experimentId: 'experiment-a' });
    assert.equal(elements(tree, n => n.type === 'button' && n.props.children === 'evaluation.experiment.startRun').length, 0);
  }
  e.session.permissions = ['evaluation_run'];
  assert.equal(elements(e.render(Experiment, { experimentId: 'experiment-a' }), n => n.type === 'button' && n.props.children === 'evaluation.experiment.startRun').length, 1);
  console.log('PASS experiment start is separated from read/manage permissions');

  const r = harness();
  const Run = r.load('app/evaluations/runs/[runId]/run-detail-client.tsx').default;
  const first = r.render(Run, { runId: 'run-a' });
  r.session.sessionId = 2;
  const switched = r.render(Run, { runId: 'run-a' });
  const otherRun = r.render(Run, { runId: 'run-b' });
  assert.notEqual(first.key, switched.key, 'workspace/session change remounts all run state before painting');
  assert.notEqual(switched.key, otherRun.key, 'run navigation remounts all run state before painting');
  console.log('PASS run state is isolated by session and route identity');

  const gate = h.load('app/evaluations/runs/[runId]/workflow-sections.tsx').gateView;
  for (const status of ['PASS', 'FAIL', 'INCONCLUSIVE']) {
    const tree = h.render(gate, {
      decision: { status, policy_id: 'policy-a', created_at: '2026-11-04', reasons: ['minimum_paired_cases_not_met'], rule_results: [{ metric: 'task_success', rule: 'non_inferiority', status, baseline: 0, candidate: null, reason: 'coverage_incomplete' }] },
      policy: null, candidateVersionHref: '/agents/a/versions/frozen', candidatePlaygroundHref: '/agents/a/playground?version=frozen',
    });
    assert.equal(elements(tree, n => n.props.status === status).length, 2, 'gate and rule preserve backend verdict');
    assert.equal(elements(tree, n => n.type === 'a' && n.props.href.includes('playground')).length, status === 'PASS' ? 1 : 0, 'INCONCLUSIVE never offers the passed-version action');
    assert.ok(elements(tree, n => n.type === 'td' && n.props.children === '0').length);
    assert.ok(elements(tree, n => n.type === 'td' && n.props.children === '—').length);
    const reasons = elements(tree, n => n.type === 'p').flatMap(n => React.Children.toArray(n.props.children));
    assert.ok(reasons.includes('minimum_paired_cases_not_met'));
  }
  console.log('PASS gate PASS/FAIL/INCONCLUSIVE, reasons, zero and missing values remain distinct');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
