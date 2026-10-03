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
    '@/i18n/provider': { useI18n: () => ({ t: (key) => key, formatDateTime: String, formatNumber: String, statusLabel: String, purposeLabel: String }) },
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

async function main() {
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
  const threadHarness = harness({ '@/lib/api/threads': {
    listThreads: async (_auth, query) => { threadCalls.push(query); return []; },
  }});
  const List = load('components/apps/app-threads-page.tsx', threadHarness.mocks).default;
  threadHarness.render(List, { copy: { agentId: 'agent-a', kind: 'general', basePath: '/agents/agent-a/chat' }, weight: { values: () => ({}) } });
  await threadHarness.queries[0].fn(threadHarness.session);
  assert.equal(threadCalls[0].agentId, 'agent-a');
  console.log('PASS agent conversation list sends server-side agent filter');
  threadHarness.mocks['@/lib/api/threads'].getThread = async () => ({ agent_id: 'agent-b', kind: 'general' });
  const shellHarness = harness({ '@/lib/api/threads': threadHarness.mocks['@/lib/api/threads'] });
  const Shell = load('components/apps/app-thread-shell.tsx', shellHarness.mocks).default;
  shellHarness.render(Shell, { copy: { agentId: 'agent-a', kind: 'general', basePath: '/agents/agent-a/chat' }, threadId: 'thread-b', children: () => null });
  await assert.rejects(shellHarness.queries[0].fn(shellHarness.session), /not found/);
  console.log('PASS mismatched agent deep link cannot mount a conversation');

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
  const evidenceHarness = harness({ '@/hooks/use-workspace-data': {
    useWorkspaceData: () => ({ loaded: true, data: {
      events: [{ sequence: 1, type: 'tool.requested', payload: { tool_identity: 'legacy-tool' } }],
      evidence_refs: [], content_allowed: false, truncated: false,
    } }),
  } });
  const Evidence = load('components/runs/tool-evidence-panel.tsx', evidenceHarness.mocks).default;
  const body = evidenceHarness.render(Evidence, { runId: 'run-a' });
  const tree = evidenceHarness.render(body.type, body.props);
  assert.ok(JSON.stringify(tree).includes('runEvidence.summaryUnavailable'));
  assert.ok(JSON.stringify(tree).includes('runEvidence.permission'));
  evidenceHarness.states[0] = { snapshot_id: 'snapshot-a', chunk_id: 'chunk-a' };
  const a = evidenceHarness.render(body.type, body.props);
  evidenceHarness.states[0] = { snapshot_id: 'snapshot-a', chunk_id: 'chunk-b' };
  const b = evidenceHarness.render(body.type, body.props);
  const excerpt = (node) => elements(node, (el) => typeof el.type === 'function' && el.props.selected)[0];
  assert.notEqual(excerpt(a).key, excerpt(b).key, 'evidence selection must remount fetched excerpt');
  console.log('PASS historical summary fallback, content denial and evidence selection identity');

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
