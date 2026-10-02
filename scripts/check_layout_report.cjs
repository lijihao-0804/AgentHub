/** Validate recorded browser QA coverage and reject stale style evidence. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const crypto = require('node:crypto');
const path = require('node:path');

function validate(rows) {
  assert.equal(rows.length, 204, 'expected 204 browser cases');
  const keys = new Set();
  for (const row of rows) {
    const key = `${row.locale}:${row.width}:${row.url}`;
    assert(!keys.has(key), `duplicate case: ${key}`);
    keys.add(key);
    assert(row.heading && row.textRects > 0, `page not rendered: ${key}`);
    assert(row.pageWidth <= row.width + 2, `page overflow: ${key}`);
    assert.deepEqual(row.escaped, [], `element overflow: ${key}`);
    assert.deepEqual(row.topbarCollisions, [], `control collision: ${key}`);
    assert.deepEqual(row.textOverlaps, [], `text overlap: ${key}`);
    if (row.url.startsWith('/support/')) {
      assert.equal(row.waitedFor, 'loaded EXTERNAL_PENDING long unresolved item');
      assert.equal(row.workbench.length, 3);
      const stack = row.workbench[2];
      if (row.width > 900 && row.width <= 1280) {
        assert.equal(stack.col, '1 / -1', `narrow artifact column: ${key}`);
        assert(stack.width > row.workbench[0].width * 2);
      }
    }
  }
  for (const locale of ['en-US', 'zh-CN']) {
    for (const width of [360, 390, 768, 1024, 1280, 1440]) {
      assert.equal(rows.filter(r => r.locale === locale && r.width === width).length,
        [360, 768, 1280].includes(width) ? 23 : 11);
    }
  }
}

const root = path.resolve(__dirname, '..');
const directory = path.join(root, 'docs/reviews/evidence/layout-20261003');
const rows = JSON.parse(fs.readFileSync(path.join(directory, 'matrix.json'), 'utf8'));
validate(rows);
const edges = JSON.parse(fs.readFileSync(path.join(directory, 'edges.json'), 'utf8'));
assert.equal(edges.length, 24);
assert.equal(new Set(edges.map(r => `${r.locale}:${r.width}:${r.url}`)).size, 24);
for (const row of edges) {
  assert(row.heading && row.textRects > 0);
  assert(row.pageWidth <= row.width + 2);
  assert.deepEqual(row.escaped, []);
  assert.deepEqual(row.topbarCollisions, []);
  assert.deepEqual(row.textOverlaps, []);
}
const manifest = JSON.parse(fs.readFileSync(path.join(directory, 'manifest.json'), 'utf8'));
for (const [file, expected] of Object.entries(manifest.sha256)) {
  const actual = crypto.createHash('sha256').update(fs.readFileSync(path.join(root, file))).digest('hex');
  assert.equal(actual, expected, `evidence is stale: ${file}`);
}
// Prove the gate rejects the regressions it claims to cover.
for (const mutate of [
  row => { row.pageWidth = row.width + 100; },
  row => { row.textOverlaps = [{ a: 'title', b: 'button' }]; },
  row => { row.topbarCollisions = [['workspace', 'account']]; },
  row => { row.escaped = [{ class: 'breadcrumbs' }]; },
]) {
  const broken = structuredClone(rows);
  mutate(broken[0]);
  assert.throws(() => validate(broken));
}
console.log('PASS 204 rendered browser cases + 24 breakpoint cases, style/evidence hashes and four rejecting gate checks');
