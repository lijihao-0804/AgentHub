#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "..");
const DEFAULT_BASE_URL = "http://127.0.0.1:3000";
const DEFAULT_API_URL = "http://127.0.0.1:8000/api/v1";
const DEFAULT_ACCOUNT = path.join(
  process.env.USERPROFILE || process.env.HOME || ROOT,
  ".codex",
  "frontend-product-qa-account.json",
);

function argument(name, fallback) {
  const prefix = `--${name}=`;
  const inline = process.argv.find((value) => value.startsWith(prefix));
  if (inline) return inline.slice(prefix.length);
  const index = process.argv.indexOf(`--${name}`);
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback;
}

function listArgument(name) {
  const raw = argument(name, null);
  return raw ? raw.split(",").map((value) => value.trim()).filter(Boolean) : null;
}

function findPlaywright() {
  if (process.env.PLAYWRIGHT_MODULE_PATH) return require(process.env.PLAYWRIGHT_MODULE_PATH);
  const cacheRoot = path.join(
    process.env.LOCALAPPDATA || path.join(process.env.USERPROFILE || "", "AppData", "Local"),
    "npm-cache",
    "_npx",
  );
  const candidates = fs.existsSync(cacheRoot)
    ? fs.readdirSync(cacheRoot, { withFileTypes: true })
        .filter((entry) => entry.isDirectory())
        .map((entry) => path.join(cacheRoot, entry.name, "node_modules", "playwright"))
        .filter((candidate) => fs.existsSync(path.join(candidate, "package.json")))
        .sort((left, right) => fs.statSync(right).mtimeMs - fs.statSync(left).mtimeMs)
    : [];
  if (!candidates.length) {
    throw new Error("No cached Playwright module found. Set PLAYWRIGHT_MODULE_PATH.");
  }
  return require(candidates[0]);
}

const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

async function jsonResponse(response) {
  const text = await response.text();
  try {
    return text ? JSON.parse(text) : null;
  } catch {
    return { raw: text.slice(0, 500) };
  }
}

async function discoverFixtures(apiBaseUrl, account) {
  const login = await fetch(`${apiBaseUrl}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email: account.email, password: account.password }),
  });
  if (!login.ok) throw new Error(`Fixture API login failed with HTTP ${login.status}`);
  const auth = await jsonResponse(login);
  const headers = { Authorization: `Bearer ${auth.access_token}` };
  const workspaceId = encodeURIComponent(account.workspace_id);
  const [agentsResponse, threadsResponse, workspaceResponse] = await Promise.all([
    fetch(`${apiBaseUrl}/workspaces/${workspaceId}/agents`, { headers }),
    fetch(`${apiBaseUrl}/workspaces/${workspaceId}/threads?kind=research&limit=10&offset=0`, { headers }),
    fetch(`${apiBaseUrl}/workspaces/${workspaceId}`, { headers }),
  ]);
  for (const response of [agentsResponse, threadsResponse, workspaceResponse]) {
    if (!response.ok) throw new Error(`Fixture discovery failed with HTTP ${response.status}`);
  }
  const agentsPayload = await jsonResponse(agentsResponse);
  const threadsPayload = await jsonResponse(threadsResponse);
  const workspace = await jsonResponse(workspaceResponse);
  const agents = Array.isArray(agentsPayload) ? agentsPayload : agentsPayload?.items ?? [];
  const threads = Array.isArray(threadsPayload) ? threadsPayload : threadsPayload?.items ?? [];
  if (!agents.length) throw new Error("The real-data workspace has no agent fixture");
  return {
    agent: agents[0],
    agents,
    thread: threads[0] ?? null,
    threadCount: threads.length,
    workspace,
  };
}

async function exerciseRegistrationUi(page, baseUrl, observations) {
  let registrationRequests = 0;
  const onRequest = (request) => {
    if (request.method() === "POST" && request.url().endsWith("/api/v1/auth/register")) {
      registrationRequests += 1;
    }
  };
  page.on("request", onRequest);
  await page.goto(`${baseUrl}/register`, { waitUntil: "domcontentloaded", timeout: 45_000 });
  const fields = [
    page.locator('input[name="email"]'),
    page.locator('input[name="password"]'),
    page.locator('input[name="confirmPassword"]'),
  ];
  for (const field of fields) await field.waitFor({ state: "visible", timeout: 45_000 });
  const fieldsVisible = (await Promise.all(fields.map((field) => field.isVisible()))).every(Boolean);
  await page.locator('button[type="submit"]').click();
  const invalidVisible = await page.locator('.inline-error[role="alert"]').isVisible();
  await sleep(350);
  page.off("request", onRequest);
  observations.push({
    name: "register-ui-validation-without-account-creation",
    pass: fieldsVisible && invalidVisible && registrationRequests === 0,
    detail: {
      fields_visible: fieldsVisible,
      required_validation_visible: invalidVisible,
      registration_requests: registrationRequests,
    },
  });
}

async function loginThroughUi(page, baseUrl, account, observations) {
  await page.goto(`${baseUrl}/login`, { waitUntil: "domcontentloaded", timeout: 45_000 });
  const emailField = page.locator('input[name="email"]');
  const passwordField = page.locator('input[name="password"]');
  const submit = page.locator('button[type="submit"]');
  for (const control of [emailField, passwordField, submit]) {
    await control.waitFor({ state: "visible", timeout: 45_000 });
  }
  observations.push({
    name: "login-ui-fields",
    pass: (await Promise.all([emailField.isVisible(), passwordField.isVisible(), submit.isVisible()])).every(Boolean),
    detail: "email, password, and submit controls are visible",
  });
  await emailField.fill(account.email);
  await passwordField.fill(account.password);
  const responsePromise = page.waitForResponse(
    (response) => response.url().endsWith("/api/v1/auth/login") && response.request().method() === "POST",
    { timeout: 30_000 },
  );
  await submit.click();
  const response = await responsePromise;
  observations.push({
    name: "login-ui",
    pass: response.status() === 200,
    detail: `POST /api/v1/auth/login -> ${response.status()}`,
  });
  if (response.status() !== 200) throw new Error("Browser login failed");
  await page.locator(".app-shell").waitFor({ state: "visible", timeout: 45_000 });
  await page.locator(".ws-selector-trigger").waitFor({ state: "visible", timeout: 30_000 });
  const workspaceLabel = await page.locator(".ws-selector-trigger").getAttribute("title");
  observations.push({
    name: "target-workspace-visible",
    pass: Boolean(workspaceLabel?.includes("Research")),
    detail: workspaceLabel || "workspace selector title missing",
  });
}

async function agentTabState(page) {
  return page.evaluate(() => {
    const tablist = document.querySelector('.tab-strip[role="tablist"]');
    const tabs = tablist ? Array.from(tablist.querySelectorAll('[role="tab"]')) : [];
    const selected = tabs.filter((tab) => tab.getAttribute("aria-selected") === "true");
    const panel = document.querySelector('#agent-tab-panel');
    return {
      tabCount: tabs.length,
      selectedCount: selected.length,
      selectedId: selected[0]?.id ?? null,
      focusedId: document.activeElement?.id ?? null,
      panelRole: panel?.getAttribute("role") ?? null,
      panelLabelledBy: panel?.getAttribute("aria-labelledby") ?? null,
      panelTabIndex: panel instanceof HTMLElement ? panel.tabIndex : null,
      selectedControls: selected[0]?.getAttribute("aria-controls") ?? null,
    };
  });
}

async function exerciseAgentDetailTabs(page, baseUrl, fixtures, screenshotRoot, observations) {
  await page.setViewportSize({ width: 1440, height: 920 });
  await page.goto(`${baseUrl}/agents/${encodeURIComponent(fixtures.agent.id)}`, {
    waitUntil: "domcontentloaded",
    timeout: 60_000,
  });
  await page.locator("main h1").first().waitFor({ state: "visible", timeout: 60_000 });
  const tablist = page.locator('.tab-strip[role="tablist"]');
  await tablist.waitFor({ state: "visible", timeout: 60_000 });
  const tabs = tablist.locator('[role="tab"]');
  const firstId = await tabs.first().getAttribute("id");
  await tabs.first().focus();
  const initial = await agentTabState(page);
  observations.push({
    name: "agent-detail-tabpanel-semantics",
    pass:
      initial.tabCount >= 2 &&
      initial.selectedCount === 1 &&
      initial.panelRole === "tabpanel" &&
      initial.panelLabelledBy === initial.selectedId &&
      initial.panelTabIndex === 0 &&
      initial.selectedControls === "agent-tab-panel",
    detail: initial,
  });

  const transitions = [];
  const sequence = [
    ["ArrowRight", "agent-tab-model"],
    ["ArrowLeft", firstId],
    ["End", "agent-tab-versions"],
    ["Home", firstId],
  ];
  for (const [key, expectedId] of sequence) {
    await page.keyboard.press(key);
    await page.waitForFunction(
      (id) => {
        const panel = document.querySelector('#agent-tab-panel');
        return (
          document.activeElement?.id === id &&
          document.querySelector('[role="tab"][aria-selected="true"]')?.id === id &&
          panel?.getAttribute("aria-labelledby") === id
        );
      },
      expectedId,
      { timeout: 10_000 },
    );
    const state = await agentTabState(page);
    transitions.push({
      key,
      expected_id: expectedId,
      pass:
        state.selectedId === expectedId &&
        state.focusedId === expectedId &&
        state.panelLabelledBy === expectedId,
      state,
    });
  }
  const interactionScreenshot = path.join(screenshotRoot, "interactions", "agent-detail-tabs-keyboard.png");
  fs.mkdirSync(path.dirname(interactionScreenshot), { recursive: true });
  await page.screenshot({ path: interactionScreenshot, fullPage: false, animations: "disabled" });
  observations.push({
    name: "agent-detail-tab-keyboard",
    pass: transitions.every((item) => item.pass),
    detail: {
      transitions,
      screenshot: interactionScreenshot,
    },
  });
}

async function setPreferences(page, theme, locale, observations) {
  if (await page.locator("html").getAttribute("data-theme") !== theme) {
    await page.locator(".theme-toggle").click();
  }
  if (await page.locator("html").getAttribute("lang") !== locale) {
    await page.locator(".language-toggle").click();
  }
  await page.waitForFunction(
    ({ expectedLocale, expectedTheme }) =>
      document.documentElement.lang === expectedLocale &&
      document.documentElement.dataset.theme === expectedTheme,
    { expectedLocale: locale, expectedTheme: theme },
  );
  observations.push({ name: `preferences-${theme}-${locale}`, pass: true, detail: "UI switches applied" });
}

async function waitForStableVisualState(page, scene) {
  if (scene.name === "home") {
    await page.waitForFunction(
      () => {
        const values = Array.from(document.querySelectorAll(".home-pulse-item strong"));
        return values.length === 4 && values.every((value) => {
          const text = value.textContent?.trim() ?? "";
          return Boolean(text) && text !== "…" && text !== "...";
        });
      },
      { timeout: 15_000 },
    );
  }
  await page.evaluate(async () => {
    await document.fonts?.ready;
    let previous = "";
    let stableFrames = 0;
    for (let frame = 0; frame < 20; frame += 1) {
      await new Promise((resolve) => requestAnimationFrame(resolve));
      const rect = (selector) => {
        const element = document.querySelector(selector);
        if (!element) return null;
        const bounds = element.getBoundingClientRect();
        const style = getComputedStyle(element);
        return [bounds.x, bounds.y, bounds.width, bounds.height, style.visibility, style.transform].join(":");
      };
      const snapshot = [
        innerWidth,
        innerHeight,
        document.documentElement.scrollWidth,
        document.body.scrollWidth,
        rect(".app-shell"),
        rect("#app-sidebar"),
        rect("main"),
        rect("main h1"),
      ].join("|");
      stableFrames = snapshot === previous ? stableFrames + 1 : 0;
      previous = snapshot;
      if (stableFrames >= 3) return;
    }
    throw new Error("Page layout did not stabilize before visual capture");
  });
}

async function waitForSceneReadiness(page, scene) {
  if (scene.name !== "research-workbench" || !scene.expectText) return null;
  await page.waitForFunction(
    (expectedTitle) => {
      const main = document.querySelector("main");
      const text = (main?.textContent || "").replace(/\s+/g, " ").trim();
      const composer = document.querySelector('.conversation-composer textarea[name="message"]');
      const error = main?.querySelector('.state-error[role="alert"]');
      const visible = (element) => {
        if (!element) return false;
        const style = getComputedStyle(element);
        const bounds = element.getBoundingClientRect();
        return style.display !== "none" && style.visibility !== "hidden" && bounds.width > 0 && bounds.height > 0;
      };
      return (
        (text.includes(expectedTitle) && visible(composer)) ||
        Boolean(error && visible(error) && (error.textContent || "").trim())
      );
    },
    scene.expectText,
    { timeout: 45_000 },
  );
  return page.evaluate((expectedTitle) => {
    const main = document.querySelector("main");
    const text = (main?.textContent || "").replace(/\s+/g, " ").trim();
    const composer = document.querySelector('.conversation-composer textarea[name="message"]');
    const error = main?.querySelector('.state-error[role="alert"]');
    const visible = (element) => {
      if (!element) return false;
      const style = getComputedStyle(element);
      const bounds = element.getBoundingClientRect();
      return style.display !== "none" && style.visibility !== "hidden" && bounds.width > 0 && bounds.height > 0;
    };
    return {
      fixture_title_visible: text.includes(expectedTitle),
      composer_visible: visible(composer),
      error_visible: Boolean(error && visible(error)),
      error_text: error?.textContent?.replace(/\s+/g, " ").trim().slice(0, 500) || null,
    };
  }, scene.expectText);
}

async function routeGeometry(page) {
  return page.evaluate(() => {
    const rect = (element) => {
      if (!element) return null;
      const bounds = element.getBoundingClientRect();
      return { x: bounds.x, y: bounds.y, width: bounds.width, height: bounds.height, right: bounds.right, bottom: bounds.bottom };
    };
    const visible = (element) => {
      if (!element) return false;
      const style = getComputedStyle(element);
      const bounds = element.getBoundingClientRect();
      return style.display !== "none" && style.visibility !== "hidden" && bounds.width > 0 && bounds.height > 0;
    };
    const controls = Array.from(document.querySelectorAll(".topbar .shell-control, .topbar .ws-selector-trigger, .topbar .user-menu-trigger"))
      .filter(visible)
      .map((element) => ({ className: element.className, ...rect(element) }));
    const collisions = [];
    for (let left = 0; left < controls.length; left += 1) {
      for (let right = left + 1; right < controls.length; right += 1) {
        const a = controls[left];
        const b = controls[right];
        if (Math.min(a.right, b.right) - Math.max(a.x, b.x) > 2 && Math.min(a.bottom, b.bottom) - Math.max(a.y, b.y) > 2) {
          collisions.push([a.className, b.className]);
        }
      }
    }
    const active = document.querySelector(".nav-link-active[aria-current='page']");
    const heading = document.querySelector("main h1");
    const composer = document.querySelector('.conversation-composer textarea[name="message"]');
    const destination = document.querySelector(".home-task-destination");
    const destinationLabel = destination?.querySelector(":scope > span");
    const destinationSelect = destination?.querySelector("select");
    const bodyText = (document.querySelector("main")?.textContent || "").replace(/\s+/g, " ").trim();
    return {
      pageWidth: document.documentElement.scrollWidth,
      viewportWidth: innerWidth,
      bodyWidth: document.body.scrollWidth,
      heading: heading?.textContent?.trim() || null,
      activeNav: active?.getAttribute("href") || null,
      activeNavText: active?.textContent?.replace(/\s+/g, " ").trim() || null,
      sidebar: rect(document.querySelector("#app-sidebar")),
      sidebarVisible: visible(document.querySelector("#app-sidebar")),
      topbarControls: controls,
      topbarCollisions: collisions,
      language: document.documentElement.lang,
      theme: document.documentElement.dataset.theme,
      bodyText: bodyText.slice(0, 800),
      composer: composer
        ? { ...rect(composer), visible: visible(composer), disabled: composer.disabled }
        : null,
      homeTaskDestination: destination
        ? {
            label: rect(destination),
            fieldLabel: rect(destinationLabel),
            select: rect(destinationSelect),
            selectGap: destinationSelect && destinationLabel
              ? rect(destinationSelect).y - rect(destinationLabel).bottom
              : null,
          }
        : null,
      rowCount: document.querySelectorAll("main tbody tr, main article, main .agent-card, main .catalog-row").length,
    };
  });
}

async function exerciseMobileDrawer(page, width) {
  if (width > 900) return { applicable: false, pass: true };
  const toggle = page.locator(".nav-toggle");
  await toggle.click();
  await page.waitForFunction(
    () => {
      const sidebar = document.querySelector("#app-sidebar");
      if (!document.body.classList.contains("nav-open") || !sidebar) return false;
      const style = getComputedStyle(sidebar);
      const transform = style.transform === "none" ? null : new DOMMatrixReadOnly(style.transform);
      return style.visibility === "visible" && (!transform || Math.abs(transform.m41) < 1);
    },
    { timeout: 5_000 },
  );
  const expanded = await toggle.getAttribute("aria-expanded");
  const drawer = await page.locator("#app-sidebar").boundingBox();
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.body.classList.contains("nav-open"));
  await page.waitForFunction(
    () => {
      const sidebar = document.querySelector("#app-sidebar");
      return sidebar && getComputedStyle(sidebar).visibility === "hidden";
    },
    { timeout: 5_000 },
  );
  return {
    applicable: true,
    pass: expanded === "true" && Boolean(drawer && drawer.width > 0),
    expanded,
    drawer,
  };
}

function routeSlug(route) {
  return route.replace(/^\/+/, "").replace(/[^a-zA-Z0-9_-]+/g, "-") || "home";
}

async function runRouteCase(page, scene, width, theme, locale, baseUrl, screenshotRoot) {
  await page.setViewportSize({ width, height: width === 390 ? 844 : width === 768 ? 1024 : 920 });
  const browserSignals = { consoleErrors: [], pageErrors: [], failedResponses: [], requestFailures: [] };
  const startedHere = new Set();
  let caseNavigationStarted = false;
  const onRequest = (request) => {
    if (!caseNavigationStarted) {
      if (!request.isNavigationRequest() || request.frame() !== page.mainFrame()) return;
      caseNavigationStarted = true;
    }
    startedHere.add(request);
  };
  const onConsole = (message) => {
    if (message.type() === "error") {
      const location = message.location();
      const resourceFailure = message.text().startsWith("Failed to load resource:");
      const belongsToStartedRequest = Array.from(startedHere).some((request) => request.url() === location.url);
      if (resourceFailure && location.url && !belongsToStartedRequest) return;
      browserSignals.consoleErrors.push({
        text: message.text().slice(0, 1000),
        url: location.url || null,
        line: location.lineNumber,
        column: location.columnNumber,
      });
    }
  };
  const onPageError = (error) => browserSignals.pageErrors.push(String(error?.stack || error).slice(0, 2000));
  const onResponse = (response) => {
    if (startedHere.has(response.request()) && response.url().includes("/api/v1/") && response.status() >= 400) {
      browserSignals.failedResponses.push({ status: response.status(), url: response.url() });
    }
  };
  const onRequestFailed = (request) => {
    if (startedHere.has(request) && request.url().includes("/api/v1/")) {
      browserSignals.requestFailures.push({ url: request.url(), error: request.failure()?.errorText || "unknown" });
    }
  };
  page.on("request", onRequest);
  page.on("console", onConsole);
  page.on("pageerror", onPageError);
  page.on("response", onResponse);
  page.on("requestfailed", onRequestFailed);
  const checks = [];
  let geometry = null;
  let status = null;
  try {
    const response = await page.goto(`${baseUrl}${scene.path}`, { waitUntil: "domcontentloaded", timeout: 45_000 });
    status = response?.status() ?? null;
    await page.locator(".app-shell").waitFor({ state: "visible", timeout: 45_000 });
    await page.locator("main h1").first().waitFor({ state: "visible", timeout: 45_000 });
    await sleep(900);
    const readiness = await waitForSceneReadiness(page, scene);
    if (scene.expectText) {
      await page.waitForFunction(
        (expected) => (document.querySelector("main")?.textContent || "").includes(expected),
        scene.expectText,
        { timeout: 15_000 },
      ).catch(() => {});
    }
    if (readiness) {
      checks.push({
        name: "fixture-terminal-state",
        selector: ".conversation-composer textarea[name='message'], main .state-error[role='alert']",
        pass: (readiness.fixture_title_visible && readiness.composer_visible) || readiness.error_visible,
        detail: readiness,
      });
    }
    if (scene.requiresComposer) {
      await page.locator('.conversation-composer textarea[name="message"]')
        .waitFor({ state: "visible", timeout: 15_000 })
        .catch(() => {});
    }
    await waitForStableVisualState(page, scene);
    const drawer = await exerciseMobileDrawer(page, width);
    geometry = await routeGeometry(page);
    checks.push({ name: "http-status", selector: scene.path, pass: status !== null && status < 400, detail: String(status) });
    checks.push({ name: "heading", selector: "main h1", pass: Boolean(geometry.heading), detail: geometry.heading || "missing" });
    checks.push({
      name: "global-horizontal-overflow",
      selector: "document.documentElement.scrollWidth",
      pass: geometry.pageWidth <= geometry.viewportWidth + 2,
      detail: `${geometry.pageWidth} <= ${geometry.viewportWidth + 2}`,
    });
    checks.push({
      name: "active-nav",
      selector: `.nav-link-active[href='${scene.nav}']`,
      pass: geometry.activeNav === scene.nav,
      detail: geometry.activeNav || "none",
    });
    checks.push({ name: "topbar-overlap", selector: ".topbar", pass: geometry.topbarCollisions.length === 0, detail: geometry.topbarCollisions });
    checks.push({ name: "mobile-drawer", selector: ".nav-toggle", pass: drawer.pass, detail: drawer });
    checks.push({ name: "theme", selector: "html[data-theme]", pass: geometry.theme === theme, detail: geometry.theme });
    checks.push({ name: "locale", selector: "html[lang]", pass: geometry.language === locale, detail: geometry.language });
    if (scene.expectText) {
      checks.push({
        name: "real-data-content",
        selector: "main",
        pass: geometry.bodyText.toLowerCase().includes(scene.expectText.toLowerCase()),
        detail: scene.expectText,
      });
    }
    if (scene.requiresComposer) {
      checks.push({
        name: "conversation-composer-visible",
        selector: '.conversation-composer textarea[name="message"]',
        pass: Boolean(geometry.composer?.visible && geometry.composer.width > 40 && geometry.composer.height > 20),
        detail: geometry.composer || "not rendered",
      });
    }
    if (scene.name === "home" && width === 390) {
      const destination = geometry.homeTaskDestination;
      checks.push({
        name: "home-destination-mobile-spacing",
        selector: ".home-task-destination",
        pass: Boolean(
          destination &&
          destination.label.height < 100 &&
          destination.selectGap !== null &&
          destination.selectGap >= -2 &&
          destination.selectGap < 24
        ),
        detail: destination || "not rendered",
      });
    }
    const expectedApiResponses = browserSignals.failedResponses.filter((response) =>
      (scene.allowedApiResponses ?? []).some((allowed) =>
        allowed.status === response.status && response.url.includes(allowed.urlIncludes)));
    const unexpectedApiResponses = browserSignals.failedResponses.filter((response) => !expectedApiResponses.includes(response));
    const consumedAllowedResponses = new Set();
    const unexpectedConsoleErrors = browserSignals.consoleErrors.filter((message) => {
      if (!message.text.startsWith("Failed to load resource:")) return true;
      const matchingIndex = expectedApiResponses.findIndex((response, index) =>
        response.status === 403 &&
        !consumedAllowedResponses.has(index) &&
        message.url === response.url);
      if (matchingIndex < 0) return true;
      consumedAllowedResponses.add(matchingIndex);
      return false;
    });
    if (scene.allowedApiResponses?.length) {
      checks.push({
        name: "expected-api-authorization",
        selector: "page.on('response')",
        pass: expectedApiResponses.length > 0 && unexpectedApiResponses.length === 0,
        detail: { observed: expectedApiResponses, allowed: scene.allowedApiResponses },
      });
    }
    checks.push({ name: "console-errors", selector: "page.on('console')", pass: unexpectedConsoleErrors.length === 0, detail: unexpectedConsoleErrors });
    checks.push({ name: "page-errors", selector: "page.on('pageerror')", pass: browserSignals.pageErrors.length === 0, detail: browserSignals.pageErrors });
    checks.push({ name: "api-errors", selector: "page.on('response')", pass: unexpectedApiResponses.length === 0, detail: unexpectedApiResponses });
    checks.push({ name: "request-failures", selector: "page.on('requestfailed')", pass: browserSignals.requestFailures.length === 0, detail: browserSignals.requestFailures });
  } catch (error) {
    checks.push({ name: "case-execution", selector: scene.path, pass: false, detail: String(error?.stack || error) });
    try {
      geometry = await routeGeometry(page);
    } catch {}
  } finally {
    page.off("request", onRequest);
    page.off("console", onConsole);
    page.off("pageerror", onPageError);
    page.off("response", onResponse);
    page.off("requestfailed", onRequestFailed);
  }

  const relativeScreenshot = path.join(
    path.basename(screenshotRoot),
    theme,
    locale,
    String(width),
    `${routeSlug(scene.name)}.png`,
  );
  const absoluteScreenshot = path.join(screenshotRoot, theme, locale, String(width), `${routeSlug(scene.name)}.png`);
  fs.mkdirSync(path.dirname(absoluteScreenshot), { recursive: true });
  await page.screenshot({ path: absoluteScreenshot, fullPage: false, animations: "disabled" }).catch(() => {});
  return {
    route: scene.name,
    path: scene.path,
    width,
    theme,
    locale,
    status,
    screenshot: relativeScreenshot,
    geometry,
    browserSignals,
    expectedApiResponses: browserSignals.failedResponses.filter((response) =>
      (scene.allowedApiResponses ?? []).some((allowed) =>
        allowed.status === response.status && response.url.includes(allowed.urlIncludes))),
    checks,
    pass: checks.every((check) => check.pass),
  };
}

async function exerciseWorkspaceAndWorkbench(page, baseUrl, fixtures, observations) {
  await page.setViewportSize({ width: 1440, height: 920 });
  await page.goto(`${baseUrl}/dashboard`, { waitUntil: "domcontentloaded", timeout: 45_000 });
  await page.locator(".app-shell").waitFor({ state: "visible" });
  const workspaceButton = page.locator(".ws-selector-trigger");
  await workspaceButton.click();
  const researchMenu = page.locator('.ws-menu-item[role="menuitem"]').filter({ hasText: "Research" });
  const workspaceMenuVisible = await researchMenu.isVisible();
  await page.keyboard.press("Escape");
  const sidebarBefore = await page.locator(".app-shell").getAttribute("data-sidebar");
  await page.locator(".sidebar-toggle").click();
  const sidebarAfter = await page.locator(".app-shell").getAttribute("data-sidebar");
  await page.locator(".sidebar-toggle").click();
  observations.push({
    name: "workspace-selector",
    pass: workspaceMenuVisible,
    detail: `Research menu visible=${workspaceMenuVisible}`,
  });
  observations.push({
    name: "desktop-sidebar-toggle",
    pass: sidebarBefore !== sidebarAfter && sidebarAfter === "collapsed",
    detail: `${sidebarBefore} -> ${sidebarAfter}`,
  });

  await page.keyboard.press("Control+k");
  const finder = page.locator(".page-finder[open]");
  await finder.locator("input").fill("/knowledge");
  await finder.locator("input").press("ArrowDown");
  const focusedHref = await finder.locator("a:focus").getAttribute("href");
  await page.keyboard.press("Escape");
  observations.push({
    name: "keyboard-page-finder",
    pass: focusedHref === "/knowledge" && !(await finder.isVisible()),
    detail: focusedHref,
  });

  await page.goto(baseUrl);
  const task = page.locator(".home-task-field textarea");
  await task.fill("Check the order\nReview the evidence before submitting.");
  await page.locator(".home-workflow-options label").filter({ has: page.locator('input[value="/support"]') }).click();
  await task.press("Control+Enter");
  await page.waitForURL((url) => url.pathname === "/support");
  const conversationTitle = page.locator('input[name="conversation-title"]');
  await conversationTitle.waitFor();
  observations.push({
    name: "home-task-private-handoff",
    pass: (await conversationTitle.inputValue()) === "Check the order" && !new URL(page.url()).search,
    detail: "Home to support draft",
  });

  if (!fixtures.thread) return;
  await page.goto(`${baseUrl}/research/${encodeURIComponent(fixtures.thread.id)}`, {
    waitUntil: "domcontentloaded",
    timeout: 45_000,
  });
  await page.locator("main h1").first().waitFor({ state: "visible", timeout: 45_000 });
  const railToggle = page.locator('.thread-topbar button[aria-controls="conversation-history"]');
  const before = await railToggle.getAttribute("aria-expanded");
  await railToggle.click();
  const after = await railToggle.getAttribute("aria-expanded");
  observations.push({
    name: "workbench-thread-rail-toggle",
    pass: before !== after,
    detail: `${before} -> ${after}`,
  });
  const sideToggle = page.locator(".thread-side-toggle");
  if (await sideToggle.isVisible()) {
    const frame = page.locator(".thread-frame");
    const sideBefore = await frame.getAttribute("data-side");
    await sideToggle.click();
    const sideAfter = await frame.getAttribute("data-side");
    observations.push({
      name: "workbench-side-pane-toggle",
      pass: sideBefore !== sideAfter,
      detail: `${sideBefore} -> ${sideAfter}`,
    });
  }
}

async function main() {
  if (process.argv.includes("--help") || process.argv.includes("-h")) {
    process.stdout.write([
      "Usage: node scripts/frontend_product_qa.cjs [options]",
      "",
      "  --phase NAME              Evidence phase (default: final)",
      "  --scenes A,B              Route scenes to exercise",
      "  --widths 1440,768,390     Viewport widths",
      "  --themes light,dark       Theme matrix",
      "  --locales en-US,zh-CN     Locale matrix",
      "  --skip-registration true  Skip the non-submitting registration UI check",
      "  --skip-interactions true  Skip the final workspace/sidebar interaction probes",
      "  --account-file PATH       QA fixture with credentials (kept outside the repository)",
      "  --base-url URL            Web origin (default: http://127.0.0.1:3000)",
      "  --api-url URL             API v1 URL (default: http://127.0.0.1:8000/api/v1)",
      "",
    ].join("\n"));
    return;
  }
  const phase = argument("phase", "final");
  const baseUrl = argument("base-url", process.env.AGENTHUB_QA_BASE_URL || DEFAULT_BASE_URL).replace(/\/$/, "");
  const apiBaseUrl = argument("api-url", process.env.AGENTHUB_QA_API_URL || DEFAULT_API_URL).replace(/\/$/, "");
  const accountPath = path.resolve(argument("account-file", process.env.AGENTHUB_QA_ACCOUNT_FILE || DEFAULT_ACCOUNT));
  const outputRoot = path.resolve(argument(
    "output-root",
    path.join(ROOT, "output", "playwright", "frontend-product-20261111"),
  ));
  const screenshotRoot = path.join(outputRoot, phase);
  const account = JSON.parse(fs.readFileSync(accountPath, "utf8"));
  const fixtures = await discoverFixtures(apiBaseUrl, account);
  const allScenes = [
    { name: "home", path: "/", nav: "/" },
    { name: "dashboard", path: "/dashboard", nav: "/dashboard" },
    { name: "incidents-welcome", path: "/incidents", nav: "/incidents" },
    { name: "analytics-welcome", path: "/analytics", nav: "/analytics" },
    { name: "support-welcome", path: "/support", nav: "/support" },
    { name: "agents", path: "/agents", nav: "/agents", expectText: fixtures.agent.name },
    { name: "agent-detail", path: `/agents/${encodeURIComponent(fixtures.agent.id)}`, nav: "/agents", expectText: fixtures.agent.name },
    { name: "knowledge", path: "/knowledge", nav: "/knowledge" },
    { name: "tools", path: "/tools", nav: "/tools" },
    { name: "runs", path: "/runs", nav: "/runs" },
    { name: "approvals", path: "/approvals", nav: "/approvals" },
    { name: "evaluations", path: "/evaluations", nav: "/evaluations" },
    { name: "settings", path: "/settings", nav: "/settings" },
    {
      name: "settings-members",
      path: "/settings/members",
      nav: "/settings",
      allowedApiResponses: [{ status: 403, urlIncludes: "/organizations/" }],
    },
    {
      name: "research-workbench",
      path: fixtures.thread ? `/research/${encodeURIComponent(fixtures.thread.id)}` : "/research",
      nav: "/research",
      requiresComposer: Boolean(fixtures.thread),
      expectText: fixtures.thread?.title,
    },
    {
      name: "knowledge-wiki",
      path: "/knowledge/wiki",
      nav: "/knowledge",
      allowedApiResponses: [{ status: 403, urlIncludes: "/wiki/compilations" }],
    },
  ];
  const selectedSceneNames = listArgument("scenes");
  const scenes = selectedSceneNames
    ? allScenes.filter((scene) => selectedSceneNames.includes(scene.name))
    : allScenes;
  if (!scenes.length) throw new Error(`No scenes matched --scenes (${selectedSceneNames?.join(",") || "empty"})`);
  const themes = listArgument("themes") ?? ["light", "dark"];
  const locales = listArgument("locales") ?? ["en-US", "zh-CN"];
  const widths = (listArgument("widths") ?? ["1440", "768", "390"]).map(Number);
  if (widths.some((width) => ![1440, 768, 390].includes(width))) {
    throw new Error(`Unsupported --widths: ${widths.join(",")}`);
  }
  const { chromium } = findPlaywright();
  const browser = await chromium.launch({ channel: "msedge", headless: true });
  const observations = [];
  const cases = [];
  try {
    const registrationContext = await browser.newContext({ viewport: { width: 1440, height: 920 }, locale: "en-US" });
    const registrationPage = await registrationContext.newPage();
    if (argument("skip-registration", "false") !== "true") {
      await exerciseRegistrationUi(registrationPage, baseUrl, observations);
    }
    await registrationContext.close();

    const context = await browser.newContext({ viewport: { width: 1440, height: 920 }, locale: "en-US" });
    const page = await context.newPage();
    page.setDefaultTimeout(60_000);
    await loginThroughUi(page, baseUrl, account, observations);
    const needsAgentDetailInteractions = scenes.some((scene) => scene.name === "agent-detail");
    if (!selectedSceneNames || needsAgentDetailInteractions) {
      await setPreferences(page, "light", "en-US", observations);
      await exerciseAgentDetailTabs(page, baseUrl, fixtures, screenshotRoot, observations);
    }
    for (const theme of themes) {
      for (const locale of locales) {
        await page.setViewportSize({ width: 1440, height: 920 });
        await setPreferences(page, theme, locale, observations);
        for (const width of widths) {
          for (const scene of scenes) {
            const casePage = await context.newPage();
            casePage.setDefaultTimeout(60_000);
            try {
              const result = await runRouteCase(casePage, scene, width, theme, locale, baseUrl, screenshotRoot);
              cases.push(result);
              process.stdout.write(`${result.pass ? "PASS" : "FAIL"} ${theme} ${locale} ${width} ${scene.path}\n`);
            } finally {
              await casePage.close();
            }
          }
        }
      }
    }
    if (
      argument("skip-interactions", "false") !== "true" &&
      (!selectedSceneNames || scenes.some((scene) => scene.name === "research-workbench"))
    ) {
      await exerciseWorkspaceAndWorkbench(page, baseUrl, fixtures, observations);
    }
    await context.close();
  } finally {
    await browser.close();
  }

  const failedCases = cases.filter((item) => !item.pass);
  const failedChecks = cases.flatMap((item) => item.checks
    .filter((check) => !check.pass)
    .map((check) => ({
      path: item.path,
      viewport: item.width,
      theme: item.theme,
      locale: item.locale,
      check: check.name,
      selector: check.selector,
      detail: check.detail,
      screenshot: item.screenshot,
    })));
  const report = {
    phase,
    started_or_completed_at: new Date().toISOString(),
    base_url: baseUrl,
    api_base_url: apiBaseUrl,
    matrix: { scenes: scenes.map((scene) => scene.name), widths, themes, locales },
    workspace: "Verification Org / Research",
    authorization_fixture: {
      organization_role: "MEMBER",
      workspace_role: "VIEWER",
      workspace_permissions: ["workspace_read", "evaluation_read"],
    },
    fixture_summary: {
      agent_id: fixtures.agent.id,
      agent_name: fixtures.agent.name,
      research_thread_id: fixtures.thread?.id ?? null,
      research_thread_title: fixtures.thread?.title ?? null,
      discovered_thread_count: fixtures.threadCount,
    },
    totals: {
      cases: cases.length,
      passed: cases.length - failedCases.length,
      failed: failedCases.length,
      interaction_observations: observations.length,
      interaction_failures: observations.filter((item) => !item.pass).length,
    },
    observations,
    expected_api_responses: cases.flatMap((item) => item.expectedApiResponses.map((response) => ({
      path: item.path,
      viewport: item.width,
      theme: item.theme,
      locale: item.locale,
      ...response,
    }))),
    failed_checks: failedChecks,
    cases,
  };
  fs.mkdirSync(outputRoot, { recursive: true });
  const reportPath = path.join(outputRoot, `${phase}-report.json`);
  fs.writeFileSync(reportPath, `${JSON.stringify(report, null, 2)}\n`, "utf8");
  const summaryPath = path.join(outputRoot, `${phase}-summary.md`);
  const lines = [
    `# Frontend product QA — ${phase}`,
    "",
    `- Completed: ${report.started_or_completed_at}`,
    `- Web: ${baseUrl}`,
    `- Workspace: Verification Org / Research`,
    `- Cases: ${report.totals.passed}/${report.totals.cases} passed`,
    `- Interaction observations: ${observations.filter((item) => item.pass).length}/${observations.length} passed`,
    `- Report: ${reportPath}`,
    "",
    "## Failed checks",
    "",
    ...(failedChecks.length
      ? failedChecks.map((item) => `- \`${item.path}\` @ ${item.viewport}px, ${item.theme}, ${item.locale}: **${item.check}** \`${item.selector}\` — ${JSON.stringify(item.detail)} (${item.screenshot})`)
      : ["- None."]),
    "",
  ];
  fs.writeFileSync(summaryPath, lines.join("\n"), "utf8");
  process.stdout.write(`${JSON.stringify({
    report: reportPath,
    summary: summaryPath,
    screenshots: screenshotRoot,
    totals: report.totals,
    interaction_failures: observations.filter((item) => !item.pass),
  }, null, 2)}\n`);
  process.exitCode = failedCases.length || observations.some((item) => !item.pass) ? 1 : 0;
}

main().catch((error) => {
  process.stderr.write(`${String(error?.stack || error)}\n`);
  process.exitCode = 1;
});
