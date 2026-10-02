// Exercise the real SSE client without installing a browser or new dependencies.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const requireWeb = Module.createRequire(path.resolve('apps/web/package.json'));
const ts = requireWeb('typescript');
const compiled = ts.transpileModule(fs.readFileSync('apps/web/lib/api/agent-runtime.ts', 'utf8'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;
const mod = { exports: {} };
new Function('require', 'module', 'exports', compiled)(() => ({
  ApiError: class extends Error {}, apiBaseUrl: 'http://test.invalid',
  isRecord: value => value !== null && typeof value === 'object' && !Array.isArray(value),
}), mod, mod.exports);
const api = mod.exports;
const auth = { workspaceId: 'workspace', accessToken: 'token' };
const signal = new AbortController().signal;
function stream(events) {
  return new Response(events.map((event, i) => `data: ${JSON.stringify({
    type: event[0], payload: event[1], sequence: i + 1, run_id: 'run',
  })}\n\n`).join(''), { headers: {
    'Content-Type': 'text/event-stream', 'X-AgentHub-Run-Id': 'run',
  }});
}
async function main() {
  let timings = [], events = [];
  global.fetch = async () => stream([
    ['run.started', {}], ['message.delta', { delta: '' }],
    ['message.delta', { delta: 'text' }], ['message.delta', { delta: 'more' }],
  ]);
  await api.streamAgentRun(auth, {
    agentVersionId: 'version', inputText: 'private', signal,
    onEvent: event => events.push(event), onTiming: timing => timings.push(timing),
  });
  assert.equal(events.length, 4);
  assert.equal(timings.length, 1);
  assert.equal(timings[0].outcome, 'completed');
  assert.ok(timings[0].clientTtftMs >= 0);
  assert.ok(timings[0].elapsedMs >= timings[0].clientTtftMs);
  assert.ok(!JSON.stringify(timings).includes('text'));
  global.fetch = async () => stream([['run.started', {}]]);
  let started = false;
  const result = await api.streamThreadTurn(auth, {
    threadId: 'thread', inputText: 'q', signal, onEvent() {},
    onStarted: run => { started = run === 'run'; },
    onTiming: timing => timings.push(timing),
  });
  assert.ok(started);
  assert.equal(result.runId, 'run');
  assert.equal(timings.at(-1).clientTtftMs, null);
  const aborted = new Error('cancel'); aborted.name = 'AbortError';
  global.fetch = async () => { throw aborted; };
  await assert.rejects(api.streamThreadTurn(auth, {
    threadId: 'thread', inputText: 'q', signal, onEvent() {},
    onTiming: timing => timings.push(timing),
  }), { name: 'AbortError' });
  assert.equal(timings.at(-1).outcome, 'aborted');
  global.fetch = async () => { throw new Error('network'); };
  await assert.rejects(api.streamAgentRun(auth, {
    agentVersionId: 'v', inputText: 'q', signal, onEvent() {},
    onTiming: timing => { timings.push(timing); throw new Error('telemetry'); },
  }), /network/);
  assert.equal(timings.at(-1).outcome, 'failed');
  global.fetch = async () => stream([['run.started', {}]]);
  await api.streamAgentRun(auth, {
    agentVersionId: 'v', inputText: 'q', signal, onEvent() {},
    onTiming() { throw new Error('optional telemetry'); },
  });
  console.log('PASS: first text, no text, abort, failure, optional callback isolation');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
