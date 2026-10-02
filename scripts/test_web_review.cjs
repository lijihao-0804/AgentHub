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
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
