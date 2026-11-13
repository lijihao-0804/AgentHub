#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const path = require("node:path");

function argument(name, fallback) {
  const prefix = `--${name}=`;
  const inline = process.argv.find((value) => value.startsWith(prefix));
  if (inline) return inline.slice(prefix.length);
  const index = process.argv.indexOf(`--${name}`);
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback;
}

async function request(url, options = {}) {
  const response = await fetch(url, options);
  const text = await response.text();
  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = { raw_prefix: text.slice(0, 200) };
  }
  return { status: response.status, ok: response.ok, payload };
}

function items(payload) {
  if (Array.isArray(payload)) return payload;
  return Array.isArray(payload?.items) ? payload.items : [];
}

async function main() {
  const apiBaseUrl = argument("api-url", "http://127.0.0.1:8000/api/v1").replace(/\/$/, "");
  const accountPath = path.resolve(argument("account-file"));
  const output = path.resolve(argument(
    "output",
    path.join("output", "playwright", "frontend-product-20261111", "corrected-api-smoke.json"),
  ));
  const account = JSON.parse(fs.readFileSync(accountPath, "utf8"));
  const login = await request(`${apiBaseUrl}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email: account.email, password: account.password }),
  });
  if (!login.ok || typeof login.payload?.access_token !== "string") {
    throw new Error(`API login failed with HTTP ${login.status}`);
  }
  const headers = { Authorization: `Bearer ${login.payload.access_token}` };
  const workspace = encodeURIComponent(account.workspace_id);
  const threadList = await request(`${apiBaseUrl}/workspaces/${workspace}/threads?limit=200&offset=0`, { headers });
  if (!threadList.ok) throw new Error(`Thread list failed with HTTP ${threadList.status}`);
  const threads = items(threadList.payload);
  const threadErrors = [];
  let turnCount = 0;
  let nonRedactedTurns = 0;
  for (const thread of threads) {
    const result = await request(
      `${apiBaseUrl}/workspaces/${workspace}/threads/${encodeURIComponent(thread.id)}/turns`,
      { headers },
    );
    if (!result.ok) {
      threadErrors.push({ thread_id: thread.id, status: result.status });
      continue;
    }
    const turns = items(result.payload);
    turnCount += turns.length;
    nonRedactedTurns += turns.filter((turn) => turn.user_input !== null).length;
  }

  const experimentList = await request(`${apiBaseUrl}/workspaces/${workspace}/evaluation/experiments?limit=1`, { headers });
  const experiments = items(experimentList.payload);
  let experimentDetailStatus = null;
  if (experiments[0]?.id) {
    experimentDetailStatus = (await request(
      `${apiBaseUrl}/workspaces/${workspace}/evaluation/experiments/${encodeURIComponent(experiments[0].id)}`,
      { headers },
    )).status;
  }

  const report = {
    completed_at: new Date().toISOString(),
    api_base_url: apiBaseUrl,
    workspace_role: "VIEWER",
    thread_count: threads.length,
    turn_count: turnCount,
    thread_errors: threadErrors,
    non_redacted_turns: nonRedactedTurns,
    experiment_list_status: experimentList.status,
    experiment_count_on_first_page: experiments.length,
    experiment_detail_status: experimentDetailStatus,
    pass:
      threadErrors.length === 0 &&
      nonRedactedTurns === 0 &&
      experimentList.status === 200 &&
      (experimentDetailStatus === null || experimentDetailStatus === 200),
  };
  fs.mkdirSync(path.dirname(output), { recursive: true });
  fs.writeFileSync(output, `${JSON.stringify(report, null, 2)}\n`, "utf8");
  process.stdout.write(`${JSON.stringify({ report: output, ...report }, null, 2)}\n`);
  process.exitCode = report.pass ? 0 : 1;
}

main().catch((error) => {
  process.stderr.write(`${String(error?.stack || error)}\n`);
  process.exitCode = 1;
});
