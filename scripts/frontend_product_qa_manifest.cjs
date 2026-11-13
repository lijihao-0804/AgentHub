#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "..");
const outputRoot = path.resolve(argument(
  "output-root",
  path.join(ROOT, "output", "playwright", "frontend-product-20261111"),
));

function argument(name, fallback) {
  const prefix = `--${name}=`;
  const inline = process.argv.find((value) => value.startsWith(prefix));
  if (inline) return inline.slice(prefix.length);
  const index = process.argv.indexOf(`--${name}`);
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback;
}

function readReport(name) {
  const filename = path.join(outputRoot, `${name}-report.json`);
  return { filename, report: JSON.parse(fs.readFileSync(filename, "utf8")) };
}

function caseKey(item) {
  return [item.path, item.width, item.theme, item.locale].join("|");
}

function main() {
  const original = readReport("final");
  const corrected = readReport("corrected");
  const correction = readReport("corrected-r2");
  const visual = readReport("visual-verified");
  const targetedResearch = readReport("post-freeze-targeted-r2-research");
  const targetedWiki = readReport("post-freeze-targeted-r2-wiki");
  const apiSmokePath = path.join(outputRoot, "corrected-api-smoke.json");
  const apiSmoke = JSON.parse(fs.readFileSync(apiSmokePath, "utf8"));
  const correctionCases = new Map(correction.report.cases.map((item) => [caseKey(item), item]));
  const effectiveCases = corrected.report.cases.map((item) => {
    const retest = correctionCases.get(caseKey(item));
    return item.pass || !retest
      ? { ...item, evidence_phase: "corrected" }
      : { ...retest, evidence_phase: "corrected-r2" };
  });
  const remainingFailures = effectiveCases.filter((item) => !item.pass);
  const initialCorrectedFailures = corrected.report.cases.filter((item) => !item.pass);
  const targetedCases = [...targetedResearch.report.cases, ...targetedWiki.report.cases];
  const targetedFailures = targetedCases.filter((item) => !item.pass);
  const buildLog = path.join(outputRoot, "post-freeze-production-build.log");
  const typecheckLog = path.join(outputRoot, "post-freeze-tsc-final.log");
  const harnessLog = path.join(outputRoot, "post-freeze-harness.log");
  const buildText = fs.readFileSync(buildLog, "utf8");
  const typecheckText = fs.readFileSync(typecheckLog, "utf8");
  const harnessText = fs.readFileSync(harnessLog, "utf8");
  const buildPass = buildText.includes("PRODUCTION BUILD PASS") && buildText.includes("Generating static pages (24/24)");
  const typecheckPass = typecheckText.includes("TSC PASS");
  const harnessPassCount = harnessText.split(/\r?\n/).filter((line) => line.startsWith("PASS ")).length;
  const harnessPass = harnessPassCount >= 17 && !harnessText.includes("AssertionError");
  const manifest = {
    generated_at: new Date().toISOString(),
    original_full_matrix: {
      report: original.filename,
      summary: path.join(outputRoot, "final-summary.md"),
      screenshots: path.join(outputRoot, "final"),
      totals: original.report.totals,
      preserved: true,
    },
    corrected_scope: {
      requested_cases: corrected.report.cases.length,
      effective_passed: effectiveCases.filter((item) => item.pass).length,
      effective_failed: remainingFailures.length,
      initial_report: corrected.filename,
      targeted_correction_report: correction.filename,
      screenshots: [path.join(outputRoot, "corrected"), path.join(outputRoot, "corrected-r2")],
      initial_failed_cases: initialCorrectedFailures.map((item) => ({
        path: item.path,
        width: item.width,
        theme: item.theme,
        locale: item.locale,
        failed_checks: item.checks.filter((check) => !check.pass),
        screenshot: item.screenshot,
      })),
      remaining_failures: remainingFailures.map((item) => ({
        path: item.path,
        width: item.width,
        theme: item.theme,
        locale: item.locale,
        failed_checks: item.checks.filter((check) => !check.pass),
      })),
    },
    post_freeze_targeted: {
      reports: [targetedResearch.filename, targetedWiki.filename],
      screenshots: [
        path.join(outputRoot, "post-freeze-targeted-r2-research"),
        path.join(outputRoot, "post-freeze-targeted-r2-wiki"),
      ],
      totals: {
        cases: targetedCases.length,
        passed: targetedCases.filter((item) => item.pass).length,
        failed: targetedFailures.length,
      },
      cases: targetedCases.map((item) => ({
        path: item.path,
        width: item.width,
        theme: item.theme,
        locale: item.locale,
        pass: item.pass,
        screenshot: item.screenshot,
        readiness: item.checks.find((check) => check.name === "fixture-terminal-state")?.detail ?? null,
        request_failures: item.browserSignals.requestFailures,
      })),
      preserved_earlier_runs: [
        path.join(outputRoot, "post-freeze-targeted-research-report.json"),
        path.join(outputRoot, "post-freeze-targeted-wiki-report.json"),
      ],
    },
    visual_verified: {
      report: visual.filename,
      summary: path.join(outputRoot, "visual-verified-summary.md"),
      screenshots: path.join(outputRoot, "visual-verified"),
      totals: visual.report.totals,
    },
    static_verification: {
      production_build: {
        pass: buildPass,
        log: buildLog,
        isolated_dist_dir: "apps/web/.next-product-build",
        static_pages: "24/24",
      },
      typecheck: {
        pass: typecheckPass,
        log: typecheckLog,
        command: "tsc --noEmit --incremental false --project apps/web/tsconfig.json",
      },
      review_harness: {
        pass: harnessPass,
        log: harnessLog,
        passed_assertions: harnessPassCount,
      },
    },
    api_smoke: {
      report: apiSmokePath,
      ...apiSmoke,
    },
    pass:
      remainingFailures.length === 0 &&
      visual.report.totals.failed === 0 &&
      visual.report.totals.interaction_failures === 0 &&
      targetedFailures.length === 0 &&
      buildPass &&
      typecheckPass &&
      harnessPass &&
      apiSmoke.pass === true,
    not_exercised: [
      "model generation",
      "formal experiments or holdout exposure",
      "business deletion, publication, or approval decisions",
    ],
  };
  const jsonPath = path.join(outputRoot, "final-verified-manifest.json");
  fs.writeFileSync(jsonPath, `${JSON.stringify(manifest, null, 2)}\n`, "utf8");
  const markdown = [
    "# AgentHub frontend QA final merged verification",
    "",
    `- Generated: ${manifest.generated_at}`,
    `- Original full matrix preserved: ${original.report.totals.passed}/${original.report.totals.cases} passed`,
    `- Corrected effective scope: ${manifest.corrected_scope.effective_passed}/${manifest.corrected_scope.requested_cases} passed`,
    `- Post-freeze targeted retest: ${manifest.post_freeze_targeted.totals.passed}/${manifest.post_freeze_targeted.totals.cases} passed (Research 768/390 and Wiki 768)`,
    `- Visual verified home/agents: ${visual.report.totals.passed}/${visual.report.totals.cases} passed`,
    `- Production build: ${buildPass ? "PASS" : "FAIL"} (24/24 static pages)`,
    `- TypeScript: ${typecheckPass ? "PASS" : "FAIL"}`,
    `- Review harness: ${harnessPass ? `PASS (${harnessPassCount}/17)` : "FAIL"}`,
    `- API smoke: ${apiSmoke.pass ? "PASS" : "FAIL"}; ${apiSmoke.thread_count} threads, ${apiSmoke.turn_count} turns, ${apiSmoke.non_redacted_turns} non-redacted turns`,
    `- Result: ${manifest.pass ? "PASS" : "FAIL"}`,
    "",
    "## Evidence",
    "",
    `- Original report: ${original.filename}`,
    `- Corrected report: ${corrected.filename}`,
    `- Targeted correction report: ${correction.filename}`,
    `- Post-freeze Research report: ${targetedResearch.filename}`,
    `- Post-freeze Wiki report: ${targetedWiki.filename}`,
    `- Visual report: ${visual.filename}`,
    `- Production build log: ${buildLog}`,
    `- TypeScript log: ${typecheckLog}`,
    `- Review harness log: ${harnessLog}`,
    `- API smoke: ${apiSmokePath}`,
    "",
    "## Remaining failures",
    "",
    ...(remainingFailures.length ? remainingFailures.map((item) => `- ${item.path} @ ${item.width}px, ${item.theme}, ${item.locale}`) : ["- None."]),
    "",
    "## Preserved initial corrected failures",
    "",
    ...(initialCorrectedFailures.length
      ? initialCorrectedFailures.map((item) => `- ${item.path} @ ${item.width}px, ${item.theme}, ${item.locale}: ${item.checks.filter((check) => !check.pass).map((check) => check.name).join(", ")}`)
      : ["- None."]),
    "",
    "## Not exercised",
    "",
    ...manifest.not_exercised.map((item) => `- ${item}`),
    "",
  ];
  const markdownPath = path.join(outputRoot, "final-verified-summary.md");
  fs.writeFileSync(markdownPath, markdown.join("\n"), "utf8");
  process.stdout.write(`${JSON.stringify({ manifest: jsonPath, summary: markdownPath, pass: manifest.pass }, null, 2)}\n`);
  process.exitCode = manifest.pass ? 0 : 1;
}

main();
