"use client";

import Link from "next/link";
import { useCallback, useDeferredValue, useEffect, useState, type FormEvent } from "react";

import { EmptyState, ErrorState, InlineError, LoadingState, Panel, SessionRequired } from "@/components/ui/states";
import {
  CatalogEmptyNotice,
  CatalogPageHeader,
  CatalogStatStrip,
  CatalogToolbar,
} from "@/components/catalog/catalog-layout";
import StatusBadge from "@/components/ui/status-badge";
import { riskTone } from "@/components/ui/badge-tones";
import { useFrontendSession } from "@/components/providers/session-provider";
import { useWorkspaceData, useWorkspaceMutation } from "@/hooks/use-workspace-data";
import { errorHintKey, type AuthInput } from "@/lib/api/client";
import {
  createTool,
  listToolCatalog,
  listTools,
  type Tool,
  type ToolCatalogItem,
} from "@/lib/api/tools";
import { useI18n } from "@/i18n/provider";
import type { MessageKey } from "@/i18n/messages";

/**
 * Catalog descriptions arrive as server-authored English text. The closed
 * builtin catalog has reader-facing translations; anything else (future
 * identities, MCP imports) keeps the server's own description.
 */
const CATALOG_DESCRIPTION_KEYS: Record<string, MessageKey> = {
  calculator: "tools.catalogDescriptions.calculator",
  query_customer: "tools.catalogDescriptions.query_customer",
  search_knowledge: "tools.catalogDescriptions.search_knowledge",
  query_wiki: "tools.catalogDescriptions.query_wiki",
  create_ticket: "tools.catalogDescriptions.create_ticket",
};

const EXECUTION_KIND_KEYS: Record<string, MessageKey> = {
  builtin: "tools.executionKind.builtin",
  action: "tools.executionKind.action",
  mcp: "tools.executionKind.mcp",
};

const APPROVAL_POLICY_KEYS: Record<string, MessageKey> = {
  NEVER: "tools.approvalPolicy.NEVER",
  ALWAYS: "tools.approvalPolicy.ALWAYS",
};

export default function ToolsPage() {
  const { t, locale, formatCount } = useI18n();
  const { connected, sessionId, workspaceId, permissions } = useFrontendSession();
  const canCreate = permissions?.includes("tool_create") === true;

  function catalogDescription(identity: string, fallback: string | null): string {
    const key = CATALOG_DESCRIPTION_KEYS[identity];
    return key ? t(key) : fallback ?? "";
  }
  function executionKindLabel(kind: string | null | undefined): string {
    if (!kind) return "—";
    const key = EXECUTION_KIND_KEYS[kind.toLowerCase()];
    return key ? t(key) : kind;
  }
  function approvalPolicyLabel(policy: string | null | undefined): string {
    if (!policy) return "—";
    const key = APPROVAL_POLICY_KEYS[policy];
    return key ? t(key) : policy;
  }

  const loadTools = useCallback((auth: AuthInput) => listTools(auth), []);
  const loadCatalog = useCallback((auth: AuthInput) => listToolCatalog(auth), []);

  const tools = useWorkspaceData<Tool[]>(loadTools, `tools:${workspaceId}`);
  const catalog = useWorkspaceData<ToolCatalogItem[]>(loadCatalog, `tool-catalog:${workspaceId}`);
  const mutation = useWorkspaceMutation(`tools:${workspaceId}`);

  const [selectedIdentity, setSelectedIdentity] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const deferredSearch = useDeferredValue(search);

  useEffect(() => {
    setSelectedIdentity(null);
    setName("");
    setDescription("");
    setNotice(null);
    setSearch("");
  }, [sessionId]);

  const copy = locale === "zh-CN"
    ? {
        eyebrow: "工具治理",
        description: "看看智能体能够做什么，并决定哪些工具可以投入使用。每项工具都会说明读取或写入范围、风险以及是否需要审批。",
        searchLabel: "搜索工具",
        searchPlaceholder: "按名称、标识、效果或风险搜索",
        clearSearch: "清空搜索",
        result: (visible: number, total: number) => `显示 ${visible} / ${total}`,
        noMatch: "没有匹配的工具",
        noMatchHint: "尝试更短的关键词，或清空搜索查看全部工具。",
        workspaceLabel: "工作区工具",
        catalogLabel: "内置目录",
        approvalGated: "需审批项",
        loadingValue: "加载中",
        unavailableValue: "不可用",
      }
    : {
        eyebrow: "Tool governance",
        description: "See what your agents can do and choose which tools they may use. Each tool shows whether it reads or changes data, its risk, and whether someone must approve it.",
        searchLabel: "Search tools",
        searchPlaceholder: "Search by name, identity, effect, or risk",
        clearSearch: "Clear search",
        result: (visible: number, total: number) => `${visible} of ${total} shown`,
        noMatch: "No matching tools",
        noMatchHint: "Try a shorter term or clear the search to see every tool.",
        workspaceLabel: "Workspace tools",
        catalogLabel: "Built-in catalog",
        approvalGated: "Approval gated",
        loadingValue: "Loading",
        unavailableValue: "Unavailable",
      };

  if (!connected) {
    return (
      <div className="page">
        <header className="page-header">
          <h1>{t("tools.title")}</h1>
        </header>
        <SessionRequired contextKey="session.context.tools" />
      </div>
    );
  }

  const toolList = tools.data ?? [];
  const catalogList = catalog.data ?? [];
  const normalizedSearch = deferredSearch.trim().toLocaleLowerCase(locale);
  const filteredTools = normalizedSearch
    ? toolList.filter((tool) =>
        [
          tool.name,
          tool.identity ?? "",
          tool.effect ?? "",
          tool.risk_level ?? "",
          executionKindLabel(tool.execution_kind),
        ].some((value) => value.toLocaleLowerCase(locale).includes(normalizedSearch)),
      )
    : toolList;
  const filteredCatalog = normalizedSearch
    ? catalogList.filter((item) =>
        [
          item.identity,
          item.description ?? "",
          catalogDescription(item.identity, item.description ?? null),
          item.effect,
          item.risk_level,
          executionKindLabel(item.execution_kind),
        ].some((value) => value.toLocaleLowerCase(locale).includes(normalizedSearch)),
      )
    : catalogList;
  const searchableTotal = toolList.length + catalogList.length;
  const searchableVisible = filteredTools.length + filteredCatalog.length;
  const approvalGated = [...toolList, ...catalogList].filter((item) => item.approval_policy === "ALWAYS").length;

  async function submitAdd(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canCreate || !selectedIdentity) return;
    const identity = selectedIdentity;
    setNotice(null);
    const result = await mutation.run((auth) =>
      createTool(auth, {
        identity,
        name: name.trim(),
        description: description.trim() || null,
      }),
    );
    if (result) {
      setSelectedIdentity(null);
      setName("");
      setDescription("");
      setNotice(t("tools.toolAdded"));
      tools.reload();
    }
  }

  return (
    <div className="page">
      <CatalogPageHeader
        eyebrow={copy.eyebrow}
        title={t("tools.title")}
        description={copy.description}
        actions={
          <Link className="button button-ghost" href="/tools/mcp">
            {t("tools.mcp.entry")}
          </Link>
        }
      />

      {notice && <p className="inline-notice">{notice}</p>}

      <CatalogStatStrip
        items={[
          {
            label: copy.workspaceLabel,
            value: tools.loaded ? formatCount(toolList.length) : tools.loading ? copy.loadingValue : copy.unavailableValue,
            href: "#workspace-tools",
          },
          {
            label: copy.catalogLabel,
            value: catalog.loaded ? formatCount(catalogList.length) : catalog.loading ? copy.loadingValue : copy.unavailableValue,
            href: "#tool-catalog",
          },
          {
            label: copy.approvalGated,
            value: tools.loaded && catalog.loaded ? formatCount(approvalGated) : copy.unavailableValue,
            tone: approvalGated > 0 ? "attention" : undefined,
          },
        ]}
      />

      {(tools.loaded || catalog.loaded) && !tools.error && !catalog.error && searchableTotal > 0 && (
        <CatalogToolbar
          searchLabel={copy.searchLabel}
          searchPlaceholder={copy.searchPlaceholder}
          searchValue={search}
          onSearchChange={setSearch}
          resultLabel={copy.result(searchableVisible, searchableTotal)}
          clearLabel={copy.clearSearch}
        >
          <button
            type="button"
            className="button button-ghost"
            onClick={() => {
              tools.reload();
              catalog.reload();
            }}
            disabled={tools.loading || catalog.loading}
          >
            {t("common.refresh")}
          </button>
        </CatalogToolbar>
      )}

      <div id="workspace-tools">
      <Panel ariaLabel={t("tools.workspaceTools")} title={t("tools.workspaceTools")}>
        {tools.error && (
          <ErrorState
            code={tools.error.code}
            message={tools.error.message || t("errors.loadTools")}
            hint={errorHintKey(tools.error) ? t(errorHintKey(tools.error)!) : undefined}
            onRetry={tools.reload}
          />
        )}
        {tools.loading && !tools.error && <LoadingState />}
        {tools.loaded && !tools.error && toolList.length === 0 && (
          <EmptyState title={t("tools.noTools")} hint={t("tools.noToolsHint")} />
        )}

        {tools.loaded && !tools.error && toolList.length > 0 && filteredTools.length === 0 && (
          <CatalogEmptyNotice title={copy.noMatch} hint={copy.noMatchHint} />
        )}

        {filteredTools.length > 0 && (
          <div className="data-table">
            <table>
              <thead>
                <tr>
                  <th scope="col">{t("tools.name")}</th>
                  <th scope="col">{t("tools.identity")}</th>
                  <th scope="col">{t("tools.effect")}</th>
                  <th scope="col">{t("tools.risk")}</th>
                  <th scope="col">{t("tools.approval")}</th>
                  <th scope="col">{t("tools.revision")}</th>
                  <th scope="col">{t("settings.models.status")}</th>
                </tr>
              </thead>
              <tbody>
                {filteredTools.map((tool) => (
                  <tr key={tool.id}>
                    <td data-label={t("tools.name")}>
                      <Link href={`/tools/${tool.id}`}>{tool.name}</Link>
                    </td>
                    <td data-label={t("tools.identity")}>
                      <code>{tool.identity ?? "—"}</code>
                    </td>
                    <td data-label={t("tools.effect")}>
                      {tool.effect ? <StatusBadge status={tool.effect} /> : "—"}
                    </td>
                    <td data-label={t("tools.risk")}>
                      {tool.risk_level ? (
                        <StatusBadge status={tool.risk_level} tone={riskTone(tool.risk_level)} />
                      ) : (
                        "—"
                      )}
                    </td>
                    <td data-label={t("tools.approval")}>
                      {tool.approval_policy ? approvalPolicyLabel(tool.approval_policy) : "—"}
                    </td>
                    <td data-label={t("tools.revision")}>
                      {tool.current_revision_number != null ? `#${tool.current_revision_number}` : "—"}
                    </td>
                    <td data-label={t("settings.models.status")}>
                      <StatusBadge
                        status={tool.enabled ? "ENABLED" : "DISABLED"}
                        tone={tool.enabled ? "success" : "neutral"}
                        label={tool.enabled ? t("common.enabled") : t("common.disabled")}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
      </div>

      <div id="tool-catalog">
      <Panel
        ariaLabel={t("tools.catalog")}
        title={t("tools.catalog")}
        eyebrow={t("tools.catalogEyebrow")}
      >
        <p className="state-hint">{t("tools.catalogNote")}</p>

        {catalog.error && (
          <ErrorState
            code={catalog.error.code}
            message={catalog.error.message || t("errors.loadCatalog")}
            hint={errorHintKey(catalog.error) ? t(errorHintKey(catalog.error)!) : undefined}
            onRetry={catalog.reload}
          />
        )}
        {catalog.loading && !catalog.error && <LoadingState />}
        {catalog.loaded && !catalog.error && catalogList.length === 0 && (
          <EmptyState title={t("tools.noCatalog")} />
        )}

        {catalog.loaded && !catalog.error && catalogList.length > 0 && filteredCatalog.length === 0 && (
          <CatalogEmptyNotice title={copy.noMatch} hint={copy.noMatchHint} />
        )}

        {filteredCatalog.length > 0 && (
          <div className="data-table">
            <table>
              <thead>
                <tr>
                  <th scope="col">{t("tools.identity")}</th>
                  <th scope="col">{t("tools.kind")}</th>
                  <th scope="col">{t("tools.effect")}</th>
                  <th scope="col">{t("tools.risk")}</th>
                  <th scope="col">{t("tools.approval")}</th>
                  <th scope="col">{t("settings.models.actions")}</th>
                </tr>
              </thead>
              <tbody>
                {filteredCatalog.map((item) => (
                  <tr key={item.identity}>
                    <td data-label={t("tools.identity")}>
                      <code>{item.identity}</code>
                      <span className="state-hint">{catalogDescription(item.identity, item.description ?? null)}</span>
                    </td>
                    <td data-label={t("tools.kind")}>
                      {executionKindLabel(item.execution_kind)}
                    </td>
                    <td data-label={t("tools.effect")}>
                      <StatusBadge status={item.effect} />
                    </td>
                    <td data-label={t("tools.risk")}>
                      <StatusBadge status={item.risk_level} tone={riskTone(item.risk_level)} />
                    </td>
                    <td data-label={t("tools.approval")}>
                      {approvalPolicyLabel(item.approval_policy)}
                    </td>
                    <td data-label={t("settings.models.actions")}>
                      <button
                        type="button"
                        className="button button-ghost"
                        onClick={() => {
                          setSelectedIdentity(item.identity);
                          setName(item.identity);
                          setDescription(catalogDescription(item.identity, item.description ?? null));
                        }}
                        disabled={!canCreate}
                        title={!canCreate ? t("agents.noPermissionHint") : undefined}
                      >
                        {t("tools.addToWorkspace")}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {selectedIdentity && (
          <form className="eval-form" onSubmit={submitAdd} noValidate>
            <p className="eval-form-title">
              {t("tools.addToWorkspace")} · <code>{selectedIdentity}</code>
            </p>
            <div className="form-grid">
              <label>
                {t("tools.name")}
                <input value={name} onChange={(event) => setName(event.target.value)} maxLength={200} />
              </label>
              <label>
                {t("tools.description")}
                <input
                  value={description}
                  onChange={(event) => setDescription(event.target.value)}
                  maxLength={500}
                />
              </label>
            </div>
            <InlineError error={mutation.error} fallback={t("errors.requestFailed")} />
            <div className="form-actions">
              <button
                type="submit"
                className="button button-primary"
                disabled={!canCreate || mutation.pending || !name.trim()}
              >
                {t("tools.addToWorkspace")}
              </button>
              <button
                type="button"
                className="button button-ghost"
                onClick={() => setSelectedIdentity(null)}
              >
                {t("session.cancel")}
              </button>
            </div>
          </form>
        )}
      </Panel>
      </div>
    </div>
  );
}
