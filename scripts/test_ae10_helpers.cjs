// Run the TypeScript semantic tests on the repository's supported Node 20+ runtime.
const fs = require('node:fs');
const path = require('node:path');
const Module = require('node:module');
const requireWeb = Module.createRequire(path.resolve('apps/web/package.json'));
const ts = requireWeb('typescript');
function compile(module, filename) {
  const code = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, esModuleInterop: true },
  }).outputText;
  module._compile(code, filename);
}
require.extensions['.ts'] = compile;
require.extensions['.mts'] = compile;
require(path.resolve('apps/web/lib/evaluation-results.test.mts'));
require(path.resolve('apps/web/lib/case-evidence-view.test.mts'));
