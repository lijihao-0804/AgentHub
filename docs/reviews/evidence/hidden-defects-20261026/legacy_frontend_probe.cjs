// Exercise the actual composer with the repository's existing hook harness.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const source = fs.readFileSync('scripts/test_web_review.cjs', 'utf8');
const setup = source.slice(0, source.indexOf('async function main()'));
const support = { exports: {} };
new Function('require', 'module', 'exports', setup + '\nmodule.exports = { load, harness, elements };')(
  require, support, support.exports,
);
const { load, harness, elements } = support.exports;

async function main() {
  const sent = [];
  const h = harness({
    '@/lib/api/agent-runtime': {
      streamThreadTurn: async (_auth, options) => { sent.push(options); return null; },
      cancelAgentRun: async () => ({}), payloadString: () => null,
    },
    '@/hooks/use-workspace-data': {
      useWorkspaceMutation: () => ({
        pending: false, error: null, clearError() {},
        run: async (fn) => fn({ workspaceId: 'workspace-a', accessToken: 'audit-placeholder' }),
      }),
    },
    '@/components/ui/use-auto-scroll': {
      useAutoScroll: () => ({ containerRef: {}, onScroll() {}, following: true, scrollToBottom() {} }),
    },
  });
  h.session.permissions = ['agent_run'];
  const { default: Pane } = load('components/apps/app-conversation-pane.tsx', h.mocks);
  const props = {
    threadId: 'thread-a', onTurnCompleted() {},
    turns: { data: [], loaded: true, loading: false, error: null, reload() {} },
  };
  let tree = h.render(Pane, props);
  elements(tree, (node) => node.type === 'textarea')[0].props.onChange({ target: { value: 'question A' } });
  tree = h.render(Pane, props);
  await elements(tree, (node) => node.type === 'form')[0].props.onSubmit({ preventDefault() {} });
  tree = h.render(Pane, props);
  elements(tree, (node) => node.type === 'textarea')[0].props.onChange({ target: { value: 'different question B' } });
  tree = h.render(Pane, props);
  await elements(tree, (node) => node.type === 'form')[0].props.onSubmit({ preventDefault() {} });
  assert.equal(sent.length, 2);
  assert.notEqual(sent[0].inputText, sent[1].inputText);
  assert.equal(sent[0].clientToken, sent[1].clientToken);
  console.log('OBSERVED: changed input after failed submission reuses the original client_token');
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
