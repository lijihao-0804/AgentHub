// Run from the repository root: node scripts/test_web_review.cjs
// Exercise actual TSX render output and async handlers without new dependencies.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const web = path.resolve('apps/web');
const webRequire = Module.createRequire(path.join(web, 'package.json'));
const ts = webRequire('typescript');
const React = webRequire('react');
const { renderToStaticMarkup } = webRequire('react-dom/server');

function load(relative, mocks = {}) {
  const filename = path.join(web, relative);
  const compiled = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
  }).outputText;
  const mod = { exports: {} };
  const localRequire = (name) => {
    if (name in mocks) return mocks[name];
    if (name.startsWith('@/')) return new Proxy({}, { get: (_, key) => key === '__esModule' ? true : key });
    if (name === 'next/link') return { __esModule: true, default: 'a' };
    if (name === 'next/navigation') return { useRouter: () => ({ push() {} }) };
    return webRequire(name);
  };
  new Function('require', 'module', 'exports', compiled)(localRequire, mod, mod.exports);
  return mod.exports;
}

function harness(api = {}) {
  let stateIndex = 0, refIndex = 0;
  const states = [], refs = [], effects = [], queries = [];
  const session = { connected: true, workspaceId: 'workspace-a', organizationId: 'org-a', accessToken: 'token', sessionId: 1, permissions: [], userId: 'self' };
  const mocks = {
    react: { ...React,
      useState(initial) {
        const index = stateIndex++;
        if (!(index in states)) states[index] = typeof initial === 'function' ? initial() : initial;
        return [states[index], (value) => { states[index] = typeof value === 'function' ? value(states[index]) : value; }];
      },
      useRef(initial) { const index = refIndex++; return refs[index] ??= { current: initial }; },
      useCallback: (fn) => fn,
      useMemo: (fn) => fn(),
      useEffect: (fn) => effects.push(fn),
    },
    '@/components/providers/session-provider': { useFrontendSession: () => session },
    '@/i18n/provider': { useI18n: () => ({ t: (key) => key, locale: 'en-US', formatDateTime: String, formatNumber: String, formatRelativeTime: String, formatCount: String, statusLabel: String, purposeLabel: String }) },
    '@/lib/api/client': { toApiError: (error) => error, errorHintKey: () => null, ApiError: class extends Error { constructor(code, message, status) { super(message); this.code = code; this.status = status; } } },
    '@/hooks/use-workspace-data': {
      useWorkspaceData: (fn, key) => { queries.push({ fn, key }); return { data: null, loaded: false }; },
      useWorkspaceMutation: () => ({ run: (fn) => fn(session) }),
    },
    ...api,
  };
  return { mocks, states, refs, effects, queries, session,
    render(component, props) { stateIndex = refIndex = 0; effects.length = queries.length = 0; return component(props); },
  };
}

function elements(node, predicate, result = []) {
  if (Array.isArray(node)) { node.forEach((child) => elements(child, predicate, result)); return result; }
  if (!React.isValidElement(node)) return result;
  if (predicate(node)) result.push(node);
  elements(node.props.children, predicate, result);
  return result;
}
const tick = () => new Promise((resolve) => setImmediate(resolve));

async function testIngestionAndPreferences() {
  const auth = { workspaceId: 'team/a', accessToken: 'token' };
  const calls = [];
  const client = { apiRequest: (...args) => calls.push(args), apiUpload: (...args) => calls.push(args) };
  const preferences = load('lib/api/approvals.ts', { '@/lib/api/client': client });
  await preferences.getApprovalPreference(auth);
  await preferences.saveApprovalPreference(auth, { level: 'LOW', max_auto_denials: 0 });
  assert.deepEqual(calls[0], ['/api/v1/workspaces/team%2Fa/approval-preferences/me', 'token']);
  assert.deepEqual(calls[1], [...calls[0], { method: 'PUT', body: { level: 'LOW', max_auto_denials: 0 } }]);
  let preference = { level: 'MANUAL', effective_level: 'MANUAL', identity_ceiling: 'MANUAL', max_auto_denials: 2 };
  const writes = [];
  const prefHarness = harness({
    '@/lib/api/approvals': {
      ...preferences, saveApprovalPreference: async (_, next) => { writes.push(next); return { ...preference, ...next }; },
    },
    '@/hooks/use-workspace-data': {
      useWorkspaceData: () => ({ data: preference, reload() {} }),
      useWorkspaceMutation: () => ({ run: (update) => update(auth) }),
    },
  });
  const Panel = load('components/approvals/personal-preference.tsx', prefHarness.mocks).default;
  const renderPanel = () => prefHarness.render(Panel);
  renderPanel().props.onToggle({ currentTarget: { open: true } });
  const formProps = () => elements(renderPanel(), node => typeof node.type === 'function' && !!node.props.preference)[0];
  let form = formProps();
  // PreferenceForm closes over prefHarness's hook implementations.
  const renderForm = () => prefHarness.render(form.type, form.props);
  prefHarness.states.length = 0;
  let view = renderForm();
  assert.deepEqual(elements(view, node => node.type === 'option').map(node => node.props.value), ['MANUAL']);
  preference = { ...preference, identity_ceiling: 'MEDIUM' };
  form = { type: form.type, props: { preference, onSaved() {} } };
  prefHarness.states.length = 0;
  view = renderForm();
  elements(view, node => node.type === 'select')[0].props.onChange({ target: { value: 'LOW' } });
  view = renderForm();
  elements(view, node => node.type === 'input')[0].props.onChange({ target: { value: '11' } });
  await renderForm().props.onSubmit({ preventDefault() {} });
  assert.equal(writes.length, 0);
  elements(renderForm(), node => node.type === 'input')[0].props.onChange({ target: { value: '0' } });
  await renderForm().props.onSubmit({ preventDefault() {} });
  assert.deepEqual(writes, [{ level: 'LOW', max_auto_denials: 0 }]);
  const knowledge = load('lib/api/knowledge.ts', { '@/lib/api/client': client });
  const file = new Blob(['name,total\norder,1001'], { type: 'text/csv' });
  await knowledge.uploadDocument(auth, 'kb', file);
  await knowledge.uploadDocumentRevision(auth, 'kb', 'doc', file, 'adaptive_v1');
  assert.equal(calls[2][2].has('chunking_strategy'), false);
  assert.equal(calls[3][2].get('chunking_strategy'), 'adaptive_v1');
  const { formatLocator } = load('lib/format/locator.ts');
  const labels = (key, values) => Object.values(values).join(',');
  assert.equal(formatLocator({ type: 'table_row', page: 2, block_index: 0, row: 3, column_start: 1, column_end: 4 }, labels), '2 · 1 · 3,1,4');
  assert.equal(formatLocator({ type: 'text_range', table_index: 2, row_start: 3, row_end: 5 }, labels), '2,3,5');
  console.log('PASS personal delegation scope, revision strategy inheritance and table citation labels');
}

function testAppearance() {
  const appearance = load('lib/appearance.ts');
  const h = harness({ '@/lib/appearance': appearance });
  // Load before the fake window is installed so the provider mounts with the
  // harness's effect implementation, just as it would after server rendering.
  const { ThemeProvider } = load('components/providers/theme-provider.tsx', h.mocks);
  const current = () => h.render(ThemeProvider, { children: null }).props.value;
  const media = Object.assign(new EventTarget(), { matches: true });
  const stored = new Map([[appearance.THEME_STORAGE_KEY, 'dark']]);
  const localStorage = {
    getItem: (key) => stored.get(key) ?? null,
    setItem: (key, value) => stored.set(key, value),
  };
  const root = { dataset: {}, style: new Map() };
  root.style.setProperty = root.style.set;
  const document = { documentElement: root };
  const window = Object.assign(new EventTarget(), { matchMedia: () => media, localStorage });
  const originalWindow = globalThis.window, originalDocument = globalThis.document;
  Object.assign(globalThis, { window, document });
  let unmount;
  try {
    const boot = new Function('document', 'localStorage', 'matchMedia', appearance.appearanceBootstrap);
    for (const [mode, color, dark] of [
      ['dark', null, true], ['system', ' Ab34cD ', false], ['system', '#FFFFFF', true],
      ['unexpected', 'var(--danger)', true],
    ]) {
      stored.set(appearance.THEME_STORAGE_KEY, mode);
      stored.set(appearance.ACCENT_STORAGE_KEY, color);
      media.matches = dark;
      boot(document, localStorage, window.matchMedia);
      const firstPaint = { ...root.dataset };
      appearance.applyAppearance(appearance.readAppearance(), dark);
      assert.deepEqual(root.dataset, firstPaint, 'first paint must agree with hydration');
    }
    boot(document, { getItem() { throw Error('private browsing'); } }, window.matchMedia);
    assert.deepEqual(root.dataset, { theme: 'light', accent: '#3458aa' });
    stored.clear();
    stored.set(appearance.THEME_STORAGE_KEY, 'dark');
    current();
    unmount = h.effects[0]();
    assert.equal(current().mode, 'dark', 'existing explicit choice must survive');
    current().setAccent('#6FdBAa');
    current().setAccent('red; color:blue');
    assert.equal(root.dataset.accent, '#6fdbaa');
    assert.equal(stored.get(appearance.ACCENT_STORAGE_KEY), '#6fdbaa');
    current().setMode('system');
    media.matches = false;
    media.dispatchEvent(new Event('change'));
    assert.equal(root.dataset.theme, 'light');
    current().setMode('dark');
    media.dispatchEvent(new Event('change'));
    assert.equal(root.dataset.theme, 'dark');
    stored.set(appearance.ACCENT_STORAGE_KEY, '#ffffff');
    window.dispatchEvent(Object.assign(new Event('storage'), { key: appearance.ACCENT_STORAGE_KEY }));
    assert.equal(current().accent, '#ffffff');
    current().resetAppearance();
    assert.equal(root.dataset.accent, appearance.DEFAULT_APPEARANCE.accent);
    assert.equal(stored.get(appearance.THEME_STORAGE_KEY), 'light');
    localStorage.setItem = () => { throw Error('blocked'); };
    current().setAccent('#000000');
    assert.equal(current().canPersist, false);
    assert.equal(root.dataset.accent, '#000000', 'blocked storage still allows a local preview');
  } finally {
    unmount?.();
    if (originalWindow === undefined) delete globalThis.window; else globalThis.window = originalWindow;
    if (originalDocument === undefined) delete globalThis.document; else globalThis.document = originalDocument;
  }
  console.log('PASS appearance first paint, legacy choice, live OS changes, accent persistence, storage events and private browsing');
}

async function testRunHistoryFilters() {
  const params = new URLSearchParams();
  const requests = [];
  const makeRun = (id, status) => ({
    id, status, trace_id: id, agent_version_number: 1, started_at: '2026-01-01',
    duration_ms: null, total_tokens: null, total_cost_amount: null, tool_call_count: 0,
  });
  const ui = harness({
    'next/navigation': { useSearchParams: () => params },
    '@/lib/api/agents': { listAgents: async () => [] },
    '@/lib/api/runs': { listRuns: async (input) => {
      requests.push(input);
      if (input.status) return { items: [makeRun('attention-run', input.status)], next_cursor: null };
      return input.cursor
        ? { items: [makeRun('older-run', 'SUCCEEDED')], next_cursor: null }
        : { items: [makeRun('recent-run', 'SUCCEEDED')], next_cursor: 'page-two' };
    } },
  });
  ui.mocks.react.useDeferredValue = value => value;
  const Page = load('app/runs/page.tsx', ui.mocks).default;
  const view = Page().props.children.type;
  const render = () => ui.render(view);
  async function flushChanges() {
    render();
    ui.effects.forEach(effect => effect());
    await tick();
  }
  await flushChanges();
  const toolbar = elements(render(), node => typeof node.props.onSearchChange === 'function')[0];
  toolbar.props.onSearchChange('older-run');
  let tree = render();
  assert.equal(elements(tree, node => node.type === 'table').length, 0);
  const loadMore = elements(tree, node => node.type === 'button' && node.props.children === 'runs.loadMore')[0];
  assert.ok(loadMore, 'an empty search among loaded rows must preserve the next-page action');
  loadMore.props.onClick();
  await tick();
  tree = render();
  assert.ok(elements(tree, node => node.props.href === '/runs/older-run').length);
  assert.deepEqual(requests.map(({ cursor }) => cursor), [null, 'page-two']);

  ui.session.accessToken = 'renewed';
  await flushChanges();
  assert.equal(requests.length, 2, 'token renewal preserves pagination');
  params.set('status', 'NEEDS_ATTENTION');
  await flushChanges();
  assert.equal(requests.length, 3);
  assert.equal(requests.at(-1).cursor, null);
  assert.equal(requests.at(-1).status, 'NEEDS_ATTENTION');
  tree = render();
  assert.ok(elements(tree, node => node.props.value === 'NEEDS_ATTENTION').length);
  assert.ok(elements(tree, node => node.props.href === '/runs/attention-run').length);
  assert.equal(elements(tree, node => node.props.href === '/runs/older-run').length, 0);
  console.log('PASS run-history URL filters, pagination after empty searches and token renewal');
}

function testLabeledSelect() {
  const ui = harness();
  const Select = load('components/ui/select.tsx', ui.mocks).default;
  let value = 'one', focused = false;
  const options = [{ value: 'one', label: 'One' }, { value: 'two', label: 'Two' }];
  const render = () => ui.render(Select, { value, options, onChange: next => { value = next; } });
  const trigger = elements(render(), node => node.props.role === 'combobox')[0];
  trigger.props.ref.current = { focus: () => { focused = true; } };
  trigger.props.onClick();
  const option = elements(render(), node => node.props.role === 'option')[1];
  let forwardedToLabel = true;
  option.props.onClick({ preventDefault() { forwardedToLabel = false; } });
  assert.equal(forwardedToLabel, false, 'a label must not reactivate the trigger after a pointer selection');
  assert.equal(value, 'two');
  assert.equal(focused, true);
  assert.equal(elements(render(), node => node.props.role === 'listbox').length, 0);
  console.log('PASS labeled select closes after a pointer selection and returns focus to its trigger');
}

async function main() {
  testAppearance();
  await testRunHistoryFilters();
  testLabeledSelect();
  await testIngestionAndPreferences();
  const { autoBreadcrumbs } = load('components/layout/breadcrumbs.tsx');
  const trialRun = '12345678-1234-4234-8234-123456789abc';
  assert.deepEqual(autoBreadcrumbs(`/evaluations/runs/${trialRun}`, String), [
    { label: 'nav.evaluations', href: '/evaluations' }, { label: 'common.detail', href: undefined },
  ]);
  assert.equal(autoBreadcrumbs(`/runs/${trialRun}`, String)[0].href, '/runs');
  console.log('PASS evaluation run loading trail links only to an existing index');
  const { Markdown } = load('components/ui/markdown.tsx');
  const html = renderToStaticMarkup(React.createElement(Markdown, {
    text: '<script>alert(1)</script> & "ok"\n\n[search](https://example.com/?a=1&b=2)\n\n- bullet\n1. numbered\n- last\n\n[bad](javascript:alert(1))',
  }));
  assert.ok(html.includes('&lt;script&gt;'));
  assert.ok(!html.includes('&amp;lt;'));
  assert.ok(html.includes('href="https://example.com/?a=1&amp;b=2"'));
  assert.ok(!html.includes('href="javascript:'));
  assert.ok(html.includes('<ul><li>bullet</li></ul><ol><li>numbered</li></ol><ul><li>last</li></ul>'));
  console.log('PASS Markdown text, link query, XSS protocol and mixed lists');

  let previewResult = { data: null, loaded: false, loading: false, error: null, reload() {} };
  const previewHarness = harness({ '@/hooks/use-workspace-data': { useWorkspaceData: () => previewResult } });
  const { SnapshotChunkPreview } = load('components/knowledge/snapshot-preview.tsx', previewHarness.mocks);
  const previewProps = { knowledgeBaseId: 'kb', snapshotId: 'snapshot' };
  previewHarness.session.permissions = null;
  let previewTree = previewHarness.render(SnapshotChunkPreview, previewProps);
  assert.equal(elements(previewTree, (node) => node.type === 'button')[0].props.disabled, true);
  previewHarness.session.permissions = ['knowledge_run'];
  previewTree = previewHarness.render(SnapshotChunkPreview, previewProps);
  elements(previewTree, (node) => node.type === 'button')[0].props.onClick();
  previewResult = { ...previewResult, loaded: true, data: { offset: 0, total: 12, items: [{ chunk_id: 'old', document_revision_id: 'revision', text: 'frozen', truncated: true }] } };
  previewTree = previewHarness.render(SnapshotChunkPreview, previewProps);
  assert.equal(elements(previewTree, (node) => node.type === 'pre').length, 1);
  elements(previewTree, (node) => node.type === 'button' && node.props.children === 'snapshotPreview.next')[0].props.onClick();
  previewTree = previewHarness.render(SnapshotChunkPreview, previewProps);
  assert.equal(elements(previewTree, (node) => node.type === 'pre').length, 0, 'old page cannot flash under a new offset');
  previewHarness.session.permissions = [];
  previewTree = previewHarness.render(SnapshotChunkPreview, previewProps);
  assert.equal(elements(previewTree, (node) => node.type === 'section').length, 0, 'permission loss removes content');
  console.log('PASS snapshot permission loading, pagination stale data and permission loss');

  let snapshotPayload = { workspace_id: 'workspace-a', knowledge_base_id: 'kb', snapshot_id: 'snapshot', total: 1, offset: 0, limit: 10, items: [{ chunk_id: 'chunk', document_revision_id: 'revision', ordinal: 0, text: 'frozen', truncated: false }] };
  const snapshotApi = load('lib/api/knowledge.ts', { '@/lib/api/client': {
    apiRequest: async () => snapshotPayload,
    isRecord: (value) => value !== null && typeof value === 'object' && !Array.isArray(value),
    ApiError: class extends Error {},
  } });
  const previewAuth = { workspaceId: 'workspace-a', accessToken: 'token' };
  assert.equal((await snapshotApi.previewSnapshotChunks(previewAuth, 'kb', 'snapshot', 0)).items[0].text, 'frozen');
  const validSnapshot = snapshotPayload;
  for (const bad of [
    { ...validSnapshot, workspace_id: 'other' },
    { ...validSnapshot, snapshot_id: 'other' },
    { ...validSnapshot, offset: 10 },
    { ...validSnapshot, items: [{ ...validSnapshot.items[0], text: 'x'.repeat(4097) }] },
  ]) {
    snapshotPayload = bad;
    await assert.rejects(snapshotApi.previewSnapshotChunks(previewAuth, 'kb', 'snapshot', 0));
  }
  console.log('PASS snapshot response workspace, frozen identity, offset and content bounds');

  const knowledgeHarness = harness();
  const KnowledgeDetail = load('app/knowledge/[knowledgeBaseId]/knowledge-detail-client.tsx', knowledgeHarness.mocks).default;
  knowledgeHarness.session.connected = false;
  knowledgeHarness.render(KnowledgeDetail, { knowledgeBaseId: 'kb' });
  const disconnectedEffects = knowledgeHarness.effects.length;
  knowledgeHarness.session.connected = true;
  knowledgeHarness.render(KnowledgeDetail, { knowledgeBaseId: 'kb' });
  assert.equal(knowledgeHarness.effects.length, disconnectedEffects, 'login hydration must not change hook order');
  console.log('PASS knowledge detail hook order across login hydration');

  const copyHarness = harness();
  const { CodeBlock } = load('components/ui/markdown.tsx', copyHarness.mocks);
  const originalNavigator = Object.getOwnPropertyDescriptor(globalThis, 'navigator');
  try {
    let copied;
    Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { clipboard: { writeText: async (text) => { copied = text; } } } });
    const code = '<script>unsafe()</script>\n\tconst x = 1;';
    const { Markdown: SafeMarkdown } = load('components/ui/markdown.tsx', { '@/i18n/provider': copyHarness.mocks['@/i18n/provider'] });
    const fencedHtml = renderToStaticMarkup(React.createElement(SafeMarkdown, { text: '```\n' + code + '\n```' }));
    assert.ok(!fencedHtml.includes('<script>'));
    assert.ok(fencedHtml.includes('&lt;script&gt;'));
    assert.ok(fencedHtml.includes('contain:inline-size'));
    let tree = copyHarness.render(CodeBlock, { code });
    await elements(tree, (node) => node.type === 'button')[0].props.onClick();
    assert.equal(copied, code);
    assert.equal(copyHarness.states[0], 'copied');
    navigator.clipboard.writeText = async () => { throw new Error('denied'); };
    tree = copyHarness.render(CodeBlock, { code });
    await elements(tree, (node) => node.type === 'button')[0].props.onClick();
    assert.equal(copyHarness.states[0], 'failed');
    let resolve;
    navigator.clipboard.writeText = () => new Promise((done) => { resolve = done; });
    const pending = elements(copyHarness.render(CodeBlock, { code }), (node) => node.type === 'button')[0].props.onClick();
    const cleanup = copyHarness.effects[0]();
    cleanup();
    resolve(); await pending;
    assert.equal(copyHarness.states[0], 'idle', 'stale copy cannot update after content change/unmount');
    console.log('PASS fenced code exact copy, permission failure and stale completion');
  } finally {
    if (originalNavigator) Object.defineProperty(globalThis, 'navigator', originalNavigator);
    else delete globalThis.navigator;
  }

  for (const kind of ['datasets', 'experiments']) {
    const calls = [];
    let pending;
    const h = harness({ '@/lib/api/evaluation': {
      [kind === 'datasets' ? 'listDatasets' : 'listExperiments']: async (_auth, query) => {
        calls.push(query.offset);
        if (pending) return pending;
        return { items: Array.from({ length: query.offset === 50 ? 50 : 21 }, (_, i) => ({ id: String(query.offset + i), build_sha: 'abc1234' })), total: 121 };
      },
    }});
    const Page = load(`app/evaluations/${kind}/page.tsx`, h.mocks).default;
    h.render(Page);
    const first = Array.from({ length: 50 }, (_, i) => ({ id: String(i), build_sha: 'abc1234' }));
    h.states[0] = first; h.states[1] = 121; h.states[7] = true;
    const clickMore = () => {
      const tree = h.render(Page);
      const button = elements(tree, (node) => node.type === 'button' && node.props.children === `evaluation.${kind}.loadMore`)[0];
      assert.ok(button, `${kind} must offer pagination`);
      button.props.onClick();
    };
    clickMore(); await tick();
    clickMore(); await tick();
    assert.deepEqual(calls, [50, 100]);
    assert.equal(h.states[2].flat().length, 71);
    assert.equal(first.length, 50, 'render must not mutate first-page state');
    assert.equal(h.states[4], false, 'end of listing hides pagination');
    h.states[2] = []; h.states[4] = true;
    let resolve;
    pending = new Promise((done) => { resolve = done; });
    clickMore();
    h.session.sessionId = 2;
    h.render(Page);
    resolve({ items: [{ id: 'old-workspace-row' }], total: 121 });
    await tick();
    assert.deepEqual(h.states[2], [], 'old workspace page must not land');
    console.log(`PASS ${kind} three pages, immutable state, end detection and stale session`);
  }

  const h = harness({ '@/lib/api/tenancy': {
    listWorkspaceMembers: async () => [{ user_id: 'self', email: 'self@example.com', role: 'VIEWER' }],
    listOrganizationMembers: async () => { throw new Error('must not query org as ordinary member'); },
  }});
  const Members = load('app/settings/members/page.tsx', h.mocks).default;
  h.render(Members);
  h.effects.at(-1)(); await tick();
  assert.equal(h.states[1].length, 1);
  assert.equal(h.states[2], null);
  console.log('PASS ordinary member can read workspace roster without org-admin request');
  h.session.permissions = null;
  h.mocks['@/lib/api/tenancy'].listOrganizationMembers = async () => { throw { status: 403 }; };
  h.render(Members);
  h.effects.at(-1)(); await tick();
  assert.equal(h.states[1].length, 1);
  assert.equal(h.states[2], null);
  console.log('PASS org-only 403 during permission loading preserves workspace roster');

  const threadCalls = [];
  const threadApi = {
    listThreads: async (_auth, query) => { threadCalls.push(query); return []; },
    listThreadTurns: async () => [],
    getThread: async () => ({ agent_id: 'agent-b', kind: 'general' }),
    createThread: async () => { throw new Error('create was not requested'); },
    deleteThread: async () => { throw new Error('delete was not requested'); },
    patchThread: async () => { throw new Error('rename was not requested'); },
  };
  const workbenchHarness = harness({
    '@/lib/api/threads': threadApi,
    '@/lib/api/artifacts': { listThreadArtifacts: async () => [] },
    '@/lib/api/agents': { listAgents: async () => [{ id: 'agent-a', name: 'Agent A' }] },
    '@/components/apps/app-conversation-pane': { __esModule: true, default: () => React.createElement('section', { 'data-testid': 'conversation' }) },
    '@/components/ui/icon': { __esModule: true, default: () => React.createElement('span', { 'aria-hidden': 'true' }) },
    '@/components/ui/states': {
      EmptyState: ({ title }) => React.createElement('div', null, title),
      ErrorState: ({ message }) => React.createElement('div', { role: 'alert' }, message),
      InlineError: ({ error, fallback }) => error
        ? React.createElement('div', { role: 'alert' }, error.message ?? fallback)
        : null,
      LoadingState: () => React.createElement('div', { role: 'status' }, 'loading'),
      SessionRequired: () => React.createElement('div', null, 'session required'),
    },
    '@/components/evaluation/hash-value': {
      InlineConfirm: ({ title }) => React.createElement('div', null, title),
    },
  });
  const Workbench = load('components/apps/app-workbench.tsx', workbenchHarness.mocks).default;
  workbenchHarness.render(Workbench, {
    copy: { agentId: 'agent-a', kind: 'general', basePath: '/agents/agent-a/chat' },
    threadId: 'thread-b',
    children: () => null,
  });
  const listQuery = workbenchHarness.queries.find((query) => query.key.startsWith('app-threads:'));
  await listQuery.fn(workbenchHarness.session);
  assert.deepEqual(threadCalls[0], { kind: 'general', agentId: 'agent-a', limit: 20, offset: 0 });
  console.log('PASS AppWorkbench thread list sends the server-side app and agent filters');
  const countedCalls = [];
  const countsHarness = harness({
    '@/lib/api/threads': {
      listThreads: async (_auth, options) => {
        countedCalls.push(options);
        return [{ id: 'with-0', turn_count: 0 }, { id: 'with-7', turn_count: 7 }, { id: 'older-server' }];
      },
      listThreadTurns: () => { throw new Error('do not download conversation bodies for the rail'); },
    },
    '@/lib/api/artifacts': { listThreadArtifacts: () => { throw new Error('do not download artifacts for a turn count'); } },
  });
  const CountedWorkbench = load('components/apps/app-workbench.tsx', countsHarness.mocks).default;
  for (const kind of ['research', 'support']) {
    countsHarness.render(CountedWorkbench, { copy: { kind, basePath: '/' + kind }, children: () => null });
    const query = countsHarness.queries.find((item) => item.key.startsWith('app-threads:'));
    const result = await query.fn(countsHarness.session, 20);
    assert.deepEqual(countedCalls.at(-1), { kind, agentId: undefined, limit: 20, offset: 20, includeTurnCount: true });
    assert.deepEqual(result.map((item) => item.values), [{ turns: 0 }, { turns: 7 }, null]);
  }
  console.log('PASS support and research rails use the paged count, retain true zero, and skip missing metadata');
  const threadQuery = workbenchHarness.queries.find((query) => query.key.startsWith('app-thread:'));
  await assert.rejects(threadQuery.fn(workbenchHarness.session), (error) =>
    error.code === 'THREAD_NOT_FOUND' && error.status === 404);
  console.log('PASS AppWorkbench rejects a mismatched agent or app deep link before mounting the conversation');

  workbenchHarness.session.permissions = [];
  let viewerTree = workbenchHarness.render(Workbench, {
    copy: { agentId: 'agent-a', kind: 'general', basePath: '/agents/agent-a/chat' },
    children: () => null,
  });
  const viewerNewButton = elements(viewerTree, (node) => node.type === 'button' && String(node.props.className).includes('thread-new'))[0];
  const viewerCreateButton = elements(viewerTree, (node) => node.type === 'button' && node.props.type === 'submit')[0];
  assert.equal(viewerNewButton.props.disabled, true);
  assert.equal(viewerCreateButton.props.disabled, true);

  workbenchHarness.session.permissions = ['agent_run'];
  const authorizedTree = workbenchHarness.render(Workbench, {
    copy: { agentId: 'agent-a', kind: 'general', basePath: '/agents/agent-a/chat' },
    children: () => null,
  });
  const authorizedNewButton = elements(authorizedTree, (node) => node.type === 'button' && String(node.props.className).includes('thread-new'))[0];
  assert.equal(authorizedNewButton.props.disabled, false);

  const permissionHarness = harness({
    '@/hooks/use-workspace-data': {
      useWorkspaceData: (_fn, key) => ({
        data: key.startsWith('app-thread:')
          ? { id: 'thread-a', title: 'Thread A', agent_id: 'agent-a', kind: 'general', updated_at: 'now' }
          : key.startsWith('app-turns:') || key.startsWith('app-artifacts:') || key.startsWith('app-threads:')
            ? []
            : null,
        loaded: true,
        error: null,
      }),
      useWorkspaceMutation: () => ({ run: (fn) => fn(permissionHarness.session) }),
    },
    '@/lib/api/threads': threadApi,
    '@/lib/api/artifacts': { listThreadArtifacts: async () => [] },
    '@/lib/api/agents': { listAgents: async () => [{ id: 'agent-a', name: 'Agent A' }] },
    '@/components/apps/app-conversation-pane': { __esModule: true, default: () => React.createElement('section', { 'data-testid': 'conversation' }) },
    '@/components/ui/icon': { __esModule: true, default: () => React.createElement('span', { 'aria-hidden': 'true' }) },
    '@/components/ui/states': {
      EmptyState: ({ title }) => React.createElement('div', null, title),
      ErrorState: ({ message }) => React.createElement('div', { role: 'alert' }, message),
      InlineError: ({ error, fallback }) => error ? React.createElement('div', { role: 'alert' }, error.message ?? fallback) : null,
      LoadingState: () => React.createElement('div', { role: 'status' }, 'loading'),
      SessionRequired: () => React.createElement('div', null, 'session required'),
    },
    '@/components/evaluation/hash-value': { InlineConfirm: ({ title }) => React.createElement('div', null, title) },
  });
  const PermissionWorkbench = load('components/apps/app-workbench.tsx', permissionHarness.mocks).default;
  permissionHarness.session.permissions = [];
  viewerTree = permissionHarness.render(PermissionWorkbench, {
    copy: { agentId: 'agent-a', kind: 'general', basePath: '/agents/agent-a/chat' },
    threadId: 'thread-a',
    children: () => null,
  });
  const viewerDeleteButton = elements(viewerTree, (node) => node.type === 'button' && node.props.children === 'appThread.threads.delete')[0];
  assert.equal(viewerDeleteButton.props.disabled, true);
  permissionHarness.session.permissions = ['agent_run'];
  const deleteAuthorizedTree = permissionHarness.render(PermissionWorkbench, {
    copy: { agentId: 'agent-a', kind: 'general', basePath: '/agents/agent-a/chat' },
    threadId: 'thread-a',
    children: () => null,
  });
  const authorizedDeleteButton = elements(deleteAuthorizedTree, (node) => node.type === 'button' && node.props.children === 'appThread.threads.delete')[0];
  assert.equal(authorizedDeleteButton.props.disabled, false);
  console.log('PASS AppWorkbench gates create and delete controls on agent_run permission');

  for (const file of ['feedback/feedback-panel', 'runs/tool-evidence-panel']) {
    const scoped = harness();
    const Panel = load(`components/${file}.tsx`, scoped.mocks).default;
    const first = scoped.render(Panel, { runId: 'run-a', turnId: 'turn-a' });
    scoped.session.sessionId = 2;
    const switched = scoped.render(Panel, { runId: 'run-a', turnId: 'turn-a' });
    assert.notEqual(first.key, switched.key, 'workspace switch must remount local form/evidence');
    const nextRun = scoped.render(Panel, { runId: 'run-b', turnId: 'turn-b' });
    assert.notEqual(switched.key, nextRun.key, 'run switch must remount local state');
    console.log(`PASS ${file} identity boundaries remount on workspace and run switch`);
  }
  let evidenceData = {
    events: [{ sequence: 1, type: 'tool.requested', payload: { tool_identity: 'legacy-tool' } }],
    evidence_refs: [{ snapshot_id: 'snapshot-a', knowledge_base_id: 'kb', document_revision_id: 'revision-a', chunk_id: 'chunk-a' }],
    content_allowed: false,
    truncated: false,
  };
  const evidenceHarness = harness({ '@/hooks/use-workspace-data': {
    useWorkspaceData: () => ({ loaded: true, data: evidenceData }),
  } });
  const Evidence = load('components/runs/tool-evidence-panel.tsx', evidenceHarness.mocks).default;
  const body = evidenceHarness.render(Evidence, { runId: 'run-a' });
  let evidenceTree = evidenceHarness.render(body.type, body.props);
  const eventRow = elements(evidenceTree, (el) => typeof el.type === 'function' && el.type.name === 'ToolEventRow')[0];
  assert.ok(eventRow, 'tool events must render through the compact row');
  const eventMarkup = JSON.stringify(evidenceHarness.render(eventRow.type, eventRow.props));
  assert.ok(eventMarkup.includes('legacy-tool'));
  assert.ok(eventMarkup.includes('runEvidence.events.requested'));
  assert.ok(!eventMarkup.includes('runEvidence.arguments'));
  assert.ok(!eventMarkup.includes('runEvidence.summaryUnavailable'));
  const deniedOpen = elements(evidenceTree, (el) => el.type === 'button' && el.props.children === 'runEvidence.open')[0];
  assert.ok(deniedOpen);
  assert.equal(deniedOpen.props.disabled, true);
  assert.equal(deniedOpen.props.title, 'runEvidence.permission');

  evidenceData = {
    ...evidenceData,
    evidence_refs: [
      ...evidenceData.evidence_refs,
      { snapshot_id: 'snapshot-a', knowledge_base_id: 'kb', document_revision_id: 'revision-b', chunk_id: 'chunk-b' },
    ],
    content_allowed: true,
  };
  evidenceTree = evidenceHarness.render(body.type, body.props);
  const openButtons = elements(evidenceTree, (el) => el.type === 'button' && el.props.children === 'runEvidence.open');
  openButtons[0].props.onClick();
  const selectedA = evidenceHarness.render(body.type, body.props);
  const firstExcerpt = elements(selectedA, (el) => typeof el.type === 'function' && el.type.name === 'EvidenceExcerpt')[0];
  const switchedButtons = elements(selectedA, (el) => el.type === 'button' && el.props.children === 'runEvidence.open');
  switchedButtons[1].props.onClick();
  const selectedB = evidenceHarness.render(body.type, body.props);
  const secondExcerpt = elements(selectedB, (el) => typeof el.type === 'function' && el.type.name === 'EvidenceExcerpt')[0];
  assert.ok(firstExcerpt && secondExcerpt);
  assert.notEqual(firstExcerpt.key, secondExcerpt.key, 'evidence selection must remount fetched excerpt');
  console.log('PASS historical missing-summary omission, content denial and evidence selection identity');

  const lifecycle = { id: 'handoff-a', workspace_id: 'workspace-a', thread_id: 'thread-a', source_artifact_id: 'artifact-a', source_hash: 'abc', status: 'OPEN', version: 1,
    created_by: 'self', assignee_id: null, claimed_by: null, closed_by: null, closure_reason: null, unresolved_items: [], created_at: 'now', updated_at: 'now', claimed_at: null, closed_at: null };
  let handoffData = null, handoffLoaded = true;
  const operations = [];
  const handoffHarness = harness({ '@/hooks/use-workspace-data': {
    useWorkspaceData: (_fn, key) => ({ data: key.startsWith('handoff-assignees:') ? [] : handoffData, loaded: handoffLoaded, loading: false, error: null, reload() {} }),
    useWorkspaceMutation: () => ({ pending: false, error: null, run: fn => fn(handoffHarness.session) }),
  }, '@/lib/api/handoffs': { getArtifactHandoff() {}, listHandoffAssignees() {}, openHandoff: async () => lifecycle,
    changeHandoff: async (_auth, id, action, payload) => { operations.push({ id, action, payload }); return { ...lifecycle, status: 'IN_PROGRESS' }; } },
  });
  const Handoff = load('components/support/handoff-controls.tsx', handoffHarness.mocks).default;
  const wrapper = handoffHarness.render(Handoff, { artifactId: 'artifact-a' });
  const pane = () => handoffHarness.render(wrapper.type, wrapper.props);
  const handoffButton = (key) => elements(pane(), el => el.type === 'button' && el.props.children === key)[0];
  handoffHarness.session.permissions = null;
  assert.equal(handoffButton('handoffLifecycle.start').props.disabled, true);
  handoffLoaded = false;
  assert.equal(handoffButton('handoffLifecycle.start'), undefined, 'failed/unknown reads must not imply unopened');
  handoffLoaded = true; handoffData = lifecycle;
  handoffHarness.session.permissions = ['handoff_handle'];
  await handoffButton('handoffLifecycle.claim').props.onClick();
  assert.deepEqual(operations[0], { id: 'handoff-a', action: 'claim', payload: { expected_version: 1 } });
  handoffData = { ...lifecycle, status: 'ASSIGNED', assignee_id: 'other' };
  assert.equal(handoffButton('handoffLifecycle.claim'), undefined);
  handoffData = { ...lifecycle, status: 'CLOSED', closure_reason: 'inspected', unresolved_items: ['unknown effect'] };
  assert.equal(handoffButton('handoffLifecycle.close'), undefined);
  assert.ok(JSON.stringify(pane()).includes('unknown effect'));
  handoffHarness.session.sessionId += 1;
  const switchedHandoff = handoffHarness.render(Handoff, { artifactId: 'artifact-a' });
  assert.notEqual(wrapper.key, switchedHandoff.key);
  console.log('PASS handoff unknown permission, failed read, assignment, version payload, retained unresolved items and session remount');

  let apiValue = lifecycle;
  const clientApi = load('lib/api/handoffs.ts', { '@/lib/api/client': {
    apiRequest: async () => apiValue, isRecord: v => v !== null && typeof v === 'object' && !Array.isArray(v),
    ApiError: class extends Error {},
  } });
  const auth = { workspaceId: 'workspace-a', accessToken: 'token' };
  assert.equal((await clientApi.getArtifactHandoff(auth, 'artifact-a')).id, 'handoff-a');
  apiValue = { ...lifecycle, workspace_id: 'workspace-b' };
  await assert.rejects(clientApi.getArtifactHandoff(auth, 'artifact-a'));
  apiValue = { ...lifecycle, version: true };
  await assert.rejects(clientApi.getArtifactHandoff(auth, 'artifact-a'));
  apiValue = { ...lifecycle, source_artifact_id: 'other' };
  await assert.rejects(clientApi.getArtifactHandoff(auth, 'artifact-a'));
  console.log('PASS handoff client strict response and workspace/artifact identity');
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
