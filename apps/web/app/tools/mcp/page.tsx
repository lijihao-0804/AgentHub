"use client";

import Link from "next/link";
import { useCallback, useEffect, useState, type FormEvent } from "react";

import HashValue from "@/components/evaluation/hash-value";
import StatusBadge from "@/components/ui/status-badge";
import { EmptyState, ErrorState, InlineError, LoadingState, Panel, SessionRequired } from "@/components/ui/states";
import { useFrontendSession } from "@/components/providers/session-provider";
import { useWorkspaceData, useWorkspaceMutation } from "@/hooks/use-workspace-data";
import { errorHintKey, toApiError, type AuthInput, type ApiError } from "@/lib/api/client";
import {
  createMcpConnection,
  discoverMcpTools,
  importMcpTool,
  listMcpConnections,
  testMcpConnection,
  type McpConnection,
  type McpConnectionTest,
  type McpDiscovery,
} from "@/lib/api/mcp";
import { useI18n } from "@/i18n/provider";

type ImportDraft = {
  identity: string;
  name: string;
  effect: "READ" | "WRITE";
  risk_level: "LOW" | "MEDIUM" | "HIGH";
  approval_policy: "NEVER" | "ALWAYS";
  timeout_seconds: string;
};

function slugify(value: string): string {
  const slug = value.toLowerCase().replace(/[^a-z0-9_-]+/g, "_").replace(/^_+|_+$/g, "");
  return slug.slice(0, 64);
}

function defaultDraft(remoteName: string, title: string | null): ImportDraft {
  return {
    identity: slugify(remoteName),
    name: title ?? remoteName,
    effect: "READ",
    risk_level: "LOW",
    approval_policy: "NEVER",
    timeout_seconds: "",
  };
}

export default function McpImportPage() {
  const { t, statusLabel } = useI18n();
  const { workspaceId, accessToken, connected, sessionId } = useFrontendSession();

  const loadConnections = useCallback(
    (auth: AuthInput) => listMcpConnections(auth),
    [],
  );
  const connections = useWorkspaceData<McpConnection[]>(
    loadConnections,
    `mcp-connections:${workspaceId}`,
  );
  const connectionMutation = useWorkspaceMutation(`mcp-connections:${workspaceId}`);
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [name, setName] = useState("");
  const [endpointUrl, setEndpointUrl] = useState("");
  const [authType, setAuthType] = useState<"NONE" | "BEARER">("NONE");
  const [secret, setSecret] = useState("");

  const [selectedConnectionId, setSelectedConnectionId] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<McpConnectionTest | null>(null);
  const [testing, setTesting] = useState(false);
  const [discovery, setDiscovery] = useState<McpDiscovery | null>(null);
  const [discovering, setDiscovering] = useState(false);
  const [actionError, setActionError] = useState<ApiError | null>(null);
  const [importDrafts, setImportDrafts] = useState<Record<string, ImportDraft>>({});
  const [importedIds, setImportedIds] = useState<Record<string, string>>({});
  const importMutation = useWorkspaceMutation(`mcp-import:${workspaceId}`);

  // Another workspace is another set of connections: nothing selected or
  // discovered carries over.
  useEffect(() => {
    setSelectedConnectionId(null);
    setTestResult(null);
    setDiscovery(null);
    setImportDrafts({});
    setImportedIds({});
  }, [sessionId, workspaceId]);

  const connectionList = connections.data ?? [];
  const selectedConnection =
    connectionList.find((item) => item.id === selectedConnectionId) ?? null;

  async function submitConnection(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const result = await connectionMutation.run((auth) =>
      createMcpConnection(auth, {
        name: name.trim(),
        endpoint_url: endpointUrl.trim(),
        auth_type: authType,
        ...(authType === "BEARER" && secret ? { secret } : {}),
      }),
    );
    if (result) {
      setName("");
      setEndpointUrl("");
      setSecret("");
      setAuthType("NONE");
      setShowCreateForm(false);
      connections.reload();
    }
  }

  async function runTest(connectionId: string) {
    setTesting(true);
    setActionError(null);
    setTestResult(null);
    try {
      const result = await testMcpConnection({ workspaceId, accessToken }, connectionId);
      setTestResult(result);
    } catch (caught) {
      setActionError(toApiError(caught, ""));
    } finally {
      setTesting(false);
    }
  }

  async function runDiscovery(connectionId: string) {
    setDiscovering(true);
    setActionError(null);
    setDiscovery(null);
    setImportDrafts({});
    setImportedIds({});
    try {
      const result = await discoverMcpTools({ workspaceId, accessToken }, connectionId);
      setDiscovery(result);
    } catch (caught) {
      setActionError(toApiError(caught, ""));
    } finally {
      setDiscovering(false);
    }
  }

  async function submitImport(remoteToolName: string) {
    const draft = importDrafts[remoteToolName];
    if (!draft || !selectedConnectionId) return;
    const result = await importMutation.run((auth) =>
      importMcpTool(auth, selectedConnectionId, {
        remote_tool_name: remoteToolName,
        identity: draft.identity.trim(),
        name: draft.name.trim() || null,
        effect: draft.effect,
        risk_level: draft.risk_level,
        approval_policy: draft.approval_policy,
        timeout_seconds: draft.timeout_seconds ? Number(draft.timeout_seconds) : null,
      }),
    );
    if (result) {
      setImportedIds((current) => ({ ...current, [remoteToolName]: result.id }));
    }
  }

  if (!connected) {
    return (
      <div className="page">
        <header className="page-header">
          <p className="eyebrow">{t("tools.mcp.eyebrow")}</p>
          <h1>{t("tools.mcp.title")}</h1>
        </header>
        <SessionRequired contextKey="session.context.tools" />
      </div>
    );
  }

  return (
    <div className="page">
      <header className="page-header">
        <p className="eyebrow">{t("tools.mcp.eyebrow")}</p>
        <h1>{t("tools.mcp.title")}</h1>
        <p className="page-lede">{t("tools.mcp.lede")}</p>
      </header>

      <Panel
        title={t("tools.mcp.connections.title")}
        eyebrow={t("tools.mcp.eyebrow")}
        actions={
          <button type="button" className="button button-ghost" onClick={() => setShowCreateForm((v) => !v)}>
            {t("tools.mcp.connections.add")}
          </button>
        }
      >
        {connections.error ? (
          <ErrorState
            code={connections.error.code}
            message={connections.error.message || t("errors.loadTools")}
            onRetry={connections.reload}
          />
        ) : !connections.loaded ? (
          <LoadingState />
        ) : connectionList.length === 0 ? (
          <EmptyState title={t("tools.mcp.connections.empty")} hint={t("tools.mcp.connections.emptyHint")} />
        ) : (
          <div className="data-table">
            <table>
              <thead>
                <tr>
                  <th scope="col">{t("tools.mcp.connections.name")}</th>
                  <th scope="col">{t("tools.mcp.connections.endpoint")}</th>
                  <th scope="col">{t("tools.mcp.connections.auth")}</th>
                  <th scope="col">{t("tools.mcp.connections.enabled")}</th>
                  <th scope="col">{t("settings.models.actions")}</th>
                </tr>
              </thead>
              <tbody>
                {connectionList.map((connection) => (
                  <tr key={connection.id}>
                    <td data-label={t("tools.mcp.connections.name")}>{connection.name}</td>
                    <td data-label={t("tools.mcp.connections.endpoint")}>
                      <code>{connection.endpoint_url}</code>
                    </td>
                    <td data-label={t("tools.mcp.connections.auth")}>
                      {connection.auth_type}
                      {connection.secret_configured ? " · ✓" : ""}
                    </td>
                    <td data-label={t("tools.mcp.connections.enabled")}>
                      <StatusBadge status={connection.enabled ? "AVAILABLE" : "NOT_AVAILABLE"} />
                    </td>
                    <td data-label={t("settings.models.actions")}>
                      <button
                        type="button"
                        className="button button-ghost"
                        onClick={() => {
                          setSelectedConnectionId((current) => (current === connection.id ? null : connection.id));
                          setTestResult(null);
                          setDiscovery(null);
                        }}
                      >
                        {t("tools.mcp.select")}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <InlineError error={connections.error} fallback={t("errors.loadTools")} />

        {showCreateForm && (
          <form className="eval-form" onSubmit={submitConnection} noValidate>
            <p className="eval-form-title">{t("tools.mcp.connections.formTitle")}</p>
            <div className="form-grid">
              <label>
                {t("tools.mcp.connections.name")}
                <input required value={name} onChange={(e) => setName(e.target.value)} maxLength={128} />
              </label>
              <label>
                {t("tools.mcp.connections.endpoint")}
                <input
                  required
                  value={endpointUrl}
                  onChange={(e) => setEndpointUrl(e.target.value)}
                  placeholder="https://"
                  spellCheck={false}
                />
              </label>
              <label>
                {t("tools.mcp.connections.auth")}
                <select value={authType} onChange={(e) => setAuthType(e.target.value as "NONE" | "BEARER")}>
                  <option value="NONE">NONE</option>
                  <option value="BEARER">BEARER</option>
                </select>
              </label>
              {authType === "BEARER" && (
                <label>
                  {t("tools.mcp.connections.secret")}
                  <input
                    type="password"
                    value={secret}
                    onChange={(e) => setSecret(e.target.value)}
                    autoComplete="new-password"
                  />
                </label>
              )}
            </div>
            <InlineError error={connectionMutation.error} fallback={t("errors.requestFailed")} />
            <div className="form-actions">
              <button type="submit" className="button button-primary" disabled={connectionMutation.pending}>
                {connectionMutation.pending ? t("common.loading") : t("common.save")}
              </button>
            </div>
          </form>
        )}
      </Panel>

      {selectedConnection && (
        <Panel title={t("tools.mcp.wizard.title")} eyebrow={selectedConnection.name}>
          <div className="form-actions">
            <button type="button" className="button button-ghost" onClick={() => void runTest(selectedConnection.id)} disabled={testing}>
              {testing ? t("common.loading") : t("tools.mcp.wizard.test")}
            </button>
            <button type="button" className="button button-primary" onClick={() => void runDiscovery(selectedConnection.id)} disabled={discovering}>
              {discovering ? t("common.loading") : t("tools.mcp.wizard.discover")}
            </button>
          </div>
          {actionError && (
            <ErrorState
              code={actionError.code}
              message={actionError.message || t("errors.requestFailed")}
              hint={errorHintKey(actionError) ? t(errorHintKey(actionError)!) : undefined}
            />
          )}
          {testResult && (
            <div className="inline-notice">
              <StatusBadge status={testResult.status === "healthy" ? "SUCCEEDED" : "FAILED"} />
              {" "}
              <span className="state-hint">
                {t("tools.mcp.wizard.testResult", {
                  latency: testResult.latency_ms,
                  protocol: testResult.protocol_version ?? t("common.unknown"),
                  server: testResult.server_name ?? t("common.unknown"),
                })}
              </span>
              {testResult.failure_code && (
                <span className="state-hint">
                  {t("tools.mcp.wizard.testFailure")} <code>{testResult.failure_code}</code>
                </span>
              )}
            </div>
          )}

          {discovering && <LoadingState />}
          {discovery && (
            <>
              <p className="state-hint">
                {t("tools.mcp.wizard.discoverySummary", {
                  count: discovery.tools.length,
                  server: discovery.server_name ?? t("common.unknown"),
                })}
              </p>
              {discovery.tools.length === 0 ? (
                <EmptyState title={t("tools.mcp.wizard.noTools")} />
              ) : (
                <div className="split-list">
                  {discovery.tools.map((tool) => {
                    const draft = importDrafts[tool.name] ?? defaultDraft(tool.name, tool.title);
                    const importedId = importedIds[tool.name];
                    return (
                      <div className="split-row" key={tool.name}>
                        <div>
                          <strong>{tool.title ?? tool.name}</strong>
                          <span className="state-hint"> · <code>{tool.name}</code></span>
                          {tool.description && (
                            <p className="state-hint">
                              {tool.description.length > 160 ? `${tool.description.slice(0, 160)}…` : tool.description}
                            </p>
                          )}
                        </div>
                        {importedId ? (
                          <Link className="button button-primary" href={`/tools/${encodeURIComponent(importedId)}`}>
                            {t("tools.mcp.wizard.openTool")}
                          </Link>
                        ) : (
                          <details>
                            <summary className="button button-ghost">{t("tools.mcp.wizard.configure")}</summary>
                            <form
                              className="eval-form"
                              onSubmit={(event) => {
                                event.preventDefault();
                                void submitImport(tool.name);
                              }}
                              noValidate
                            >
                              <div className="form-grid">
                                <label>
                                  {t("tools.mcp.import.identity")}
                                  <input
                                    required
                                    value={draft.identity}
                                    onChange={(e) =>
                                      setImportDrafts((current) => ({
                                        ...current,
                                        [tool.name]: { ...draft, identity: e.target.value },
                                      }))
                                    }
                                    spellCheck={false}
                                  />
                                </label>
                                <label>
                                  {t("tools.mcp.import.displayName")}
                                  <input
                                    value={draft.name}
                                    onChange={(e) =>
                                      setImportDrafts((current) => ({
                                        ...current,
                                        [tool.name]: { ...draft, name: e.target.value },
                                      }))
                                    }
                                  />
                                </label>
                                <label>
                                  {t("tools.mcp.import.effect")}
                                  <select
                                    value={draft.effect}
                                    onChange={(e) =>
                                      setImportDrafts((current) => ({
                                        ...current,
                                        [tool.name]: { ...draft, effect: e.target.value as "READ" | "WRITE" },
                                      }))
                                    }
                                  >
                                    <option value="READ">{statusLabel("READ")}</option>
                                    <option value="WRITE">{statusLabel("WRITE")}</option>
                                  </select>
                                </label>
                                <label>
                                  {t("tools.mcp.import.risk")}
                                  <select
                                    value={draft.risk_level}
                                    onChange={(e) =>
                                      setImportDrafts((current) => ({
                                        ...current,
                                        [tool.name]: { ...draft, risk_level: e.target.value as ImportDraft["risk_level"] },
                                      }))
                                    }
                                  >
                                    <option value="LOW">{statusLabel("LOW")}</option>
                                    <option value="MEDIUM">{statusLabel("MEDIUM")}</option>
                                    <option value="HIGH">{statusLabel("HIGH")}</option>
                                  </select>
                                </label>
                                <label>
                                  {t("tools.mcp.import.approval")}
                                  <select
                                    value={draft.approval_policy}
                                    onChange={(e) =>
                                      setImportDrafts((current) => ({
                                        ...current,
                                        [tool.name]: { ...draft, approval_policy: e.target.value as "NEVER" | "ALWAYS" },
                                      }))
                                    }
                                  >
                                    <option value="NEVER">{t("tools.mcp.import.approvalNever")}</option>
                                    <option value="ALWAYS">{t("tools.mcp.import.approvalAlways")}</option>
                                  </select>
                                </label>
                                <label>
                                  {t("tools.mcp.import.timeout")}
                                  <input
                                    type="number"
                                    min={1}
                                    max={120}
                                    value={draft.timeout_seconds}
                                    onChange={(e) =>
                                      setImportDrafts((current) => ({
                                        ...current,
                                        [tool.name]: { ...draft, timeout_seconds: e.target.value },
                                      }))
                                    }
                                  />
                                </label>
                              </div>
                              <p className="state-hint">{t("tools.mcp.import.governanceHint")}</p>
                              <InlineError error={importMutation.error} fallback={t("errors.requestFailed")} />
                              <div className="form-actions">
                                <button type="submit" className="button button-primary" disabled={importMutation.pending}>
                                  {importMutation.pending ? t("common.loading") : t("tools.mcp.wizard.import")}
                                </button>
                              </div>
                            </form>
                          </details>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
              <p className="state-hint">{t("tools.mcp.wizard.importedNote")} <HashValue value={selectedConnection.id} label={t("common.id")} /></p>
            </>
          )}
        </Panel>
      )}
    </div>
  );
}
