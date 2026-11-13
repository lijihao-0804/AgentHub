// One entry for the current web contracts. Run from any working directory.
const path = require('node:path');
const { spawnSync } = require('node:child_process');

const root = path.resolve(__dirname, '..');
const frontendChecks = [
  ['web-review', 'test_web_review.cjs'],
  ['ae10-helpers', 'test_ae10_helpers.cjs'],
  ['ae10-run-permissions-gates', 'test_ae10_ui.cjs'],
  ['ae10-case-evidence', 'test_ae10_case_components.cjs'],
  ['ae10-readonly', 'test_ae10_readonly.cjs'],
  ['context-policy-editor', 'test_context_policy_ui.cjs'],
  ['wiki-workbench', 'test_wiki_workbench.cjs'],
  ['agent-wiki-binding', 'test_agent_wiki_binding.cjs'],
].map(([name, file]) => ({ name, command: process.execPath, args: [path.join(__dirname, file)] }));

function runChecks(checks) {
  let exitCode = 0;
  const results = [];
  for (const check of checks) {
    console.log(`\n[web-regression] START ${check.name}`);
    const result = spawnSync(check.command, check.args, {
      cwd: root,
      stdio: 'inherit',
      env: { ...process.env, ...check.env },
    });
    const code = result.status ?? 1;
    if (result.error) console.error(`[web-regression] ${check.name}: ${result.error.message}`);
    if (result.signal) console.error(`[web-regression] ${check.name}: signal=${result.signal}`);
    console.log(`[web-regression] END ${check.name} exit=${code}`);
    results.push({ name: check.name, exitCode: code });
    if (code !== 0 && exitCode === 0) exitCode = code;
  }
  console.log(`[web-regression] ${JSON.stringify(results)}`);
  return exitCode;
}

function main(args) {
  if (args.length === 1 && args[0] === '--help') {
    console.log('node scripts/test_web_regressions.cjs [--with-api-evidence]');
    console.log('Default: current web contracts, all four AE-10 suites, context policy and Wiki editors.');
    console.log('--with-api-evidence also runs the real PostgreSQL owner/Viewer/pagination privacy regression.');
    console.log('It requires AGENTHUB_TEST_DATABASE_URL and AGENTHUB_TEST_PYTHON (existing project Python).');
    console.log('CI runs the frontend entry once and covers API evidence in its existing full backend integration job.');
    return 0;
  }
  if (args.some((arg) => arg !== '--with-api-evidence') || args.length > 1) {
    console.error('Unknown arguments; use --help.');
    return 2;
  }
  const checks = [...frontendChecks];
  if (args.includes('--with-api-evidence')) {
    const database = process.env.AGENTHUB_TEST_DATABASE_URL?.trim();
    const python = process.env.AGENTHUB_TEST_PYTHON?.trim();
    if (!database || !python) {
      console.error('API evidence requires AGENTHUB_TEST_DATABASE_URL and AGENTHUB_TEST_PYTHON; it cannot be skipped as passed.');
      return 2;
    }
    checks.push({
      name: 'ae10-api-privacy-real-postgresql',
      command: python,
      args: ['-m', 'pytest', 'tests/integration/test_ae10_evidence_view.py', '-vv', '-rs'],
      env: { AGENTHUB_DATABASE_URL: database, AGENTHUB_TESTING: 'true' },
    });
  }
  return runChecks(checks);
}

module.exports = { runChecks };
if (require.main === module) process.exitCode = main(process.argv.slice(2));
