"use client";

import Link from "next/link";
import { useCallback, useDeferredValue, useEffect, useState, type FormEvent } from "react";

import { EmptyState, ErrorState, InlineError, LoadingState, Panel, SessionRequired } from "@/components/ui/states";
import { CatalogEmptyNotice, CatalogPageHeader, CatalogToolbar } from "@/components/catalog/catalog-layout";
import HashValue from "@/components/evaluation/hash-value";
import { useFrontendSession } from "@/components/providers/session-provider";
import { useWorkspaceData, useWorkspaceMutation } from "@/hooks/use-workspace-data";
import { errorHintKey, type AuthInput } from "@/lib/api/client";
import { createKnowledgeBase, listKnowledgeBases, type KnowledgeBase } from "@/lib/api/knowledge";
import { useI18n } from "@/i18n/provider";

export default function KnowledgeBasesPage() {
  const { t, locale, formatDateTime } = useI18n();
  const { connected, sessionId, workspaceId, permissions } = useFrontendSession();
  const canCreate = permissions?.includes("knowledge_create") === true;

  const load = useCallback((auth: AuthInput) => listKnowledgeBases(auth), []);
  const bases = useWorkspaceData<KnowledgeBase[]>(load, `knowledge-bases:${workspaceId}`);
  const mutation = useWorkspaceMutation(`knowledge-bases:${workspaceId}`);

  const [name, setName] = useState("");
  const [showForm, setShowForm] = useState(false);
  const [search, setSearch] = useState("");
  const deferredSearch = useDeferredValue(search);

  useEffect(() => {
    setName("");
    setShowForm(false);
    setSearch("");
  }, [sessionId]);

  const copy = locale === "zh-CN"
    ? {
        eyebrow: "知识资源",
        description: "整理智能体可以查阅的资料。按知识库组织文档，并随时查看上传进度与可用内容。",
        searchLabel: "搜索知识库",
        searchPlaceholder: "按知识库名称或 ID 搜索",
        clearSearch: "清空搜索",
        result: (visible: number, total: number) => `显示 ${visible} / ${total}`,
        noMatch: "没有匹配的知识库",
        noMatchHint: "尝试更短的关键词，或清空搜索查看全部知识库。",
      }
    : {
        eyebrow: "Knowledge resources",
        description: "Keep the material your agents can draw on. Organize documents by knowledge base and follow upload progress from one place.",
        searchLabel: "Search knowledge bases",
        searchPlaceholder: "Search by knowledge base name or ID",
        clearSearch: "Clear search",
        result: (visible: number, total: number) => `${visible} of ${total} shown`,
        noMatch: "No matching knowledge bases",
        noMatchHint: "Try a shorter term or clear the search to see every knowledge base.",
      };

  if (!connected) {
    return (
      <div className="page">
        <header className="page-header">
          <h1>{t("knowledge.title")}</h1>
        </header>
        <SessionRequired contextKey="session.context.knowledge" />
      </div>
    );
  }

  const list = bases.data ?? [];
  const normalizedSearch = deferredSearch.trim().toLocaleLowerCase(locale);
  const filteredBases = normalizedSearch
    ? list.filter((base) =>
        [base.name, base.id].some((value) => value.toLocaleLowerCase(locale).includes(normalizedSearch)),
      )
    : list;

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canCreate) return;
    const result = await mutation.run((auth) => createKnowledgeBase(auth, { name: name.trim() }));
    if (result) {
      setName("");
      setShowForm(false);
      bases.reload();
    }
  }

  return (
    <div className="page">
      <CatalogPageHeader
        eyebrow={copy.eyebrow}
        title={t("knowledge.title")}
        description={copy.description}
        actions={
          <>
            <Link className="button button-ghost" href="/knowledge/playground">
              {t("knowledge.openPlayground")}
            </Link>
            <Link className="button button-ghost" href="/knowledge/wiki">
              {locale === "zh-CN" ? "Wiki 工作台" : "Wiki workbench"}
            </Link>
            <button
              type="button"
              className="button button-primary"
              disabled={!canCreate}
              title={!canCreate ? t("agents.noPermissionHint") : undefined}
              onClick={() => {
                setShowForm((value) => !value);
                setName("");
              }}
            >
              {t("knowledge.createBase")}
            </button>
          </>
        }
      />

      <Panel
        ariaLabel={t("knowledge.bases")}
      >
        {/* Documents live inside a knowledge base; say so where the list is. */}
        <p className="state-hint">{t("knowledge.uploadLocationHint")}</p>

        {showForm && (
          <form className="eval-form" onSubmit={submit} noValidate>
            <p className="eval-form-title">{t("knowledge.createBase")}</p>
            <label>
              {t("knowledge.baseName")}
              <input value={name} onChange={(event) => setName(event.target.value)} maxLength={200} />
            </label>
            <InlineError error={mutation.error} fallback={t("errors.requestFailed")} />
            <div className="form-actions">
              <button
                type="submit"
                className="button button-primary"
                disabled={!canCreate || mutation.pending || !name.trim()}
              >
                {t("knowledge.createBase")}
              </button>
              <button type="button" className="button button-ghost" onClick={() => setShowForm(false)}>
                {t("session.cancel")}
              </button>
            </div>
          </form>
        )}

        {bases.error && (
          <ErrorState
            code={bases.error.code}
            message={bases.error.message || t("errors.loadKnowledgeBases")}
            hint={errorHintKey(bases.error) ? t(errorHintKey(bases.error)!) : undefined}
            onRetry={bases.reload}
          />
        )}
        {bases.loading && !bases.error && <LoadingState />}
        {bases.loaded && !bases.error && list.length > 0 && (
          <CatalogToolbar
            searchLabel={copy.searchLabel}
            searchPlaceholder={copy.searchPlaceholder}
            searchValue={search}
            onSearchChange={setSearch}
            resultLabel={copy.result(filteredBases.length, list.length)}
            clearLabel={copy.clearSearch}
          >
            <button type="button" className="button button-ghost" onClick={bases.reload} disabled={bases.loading}>
              {t("common.refresh")}
            </button>
          </CatalogToolbar>
        )}
        {bases.loaded && !bases.error && list.length === 0 && (
          <EmptyState
            title={t("knowledge.noBases")}
            hint={t("knowledge.noBasesHint")}
            actions={
              <button type="button" className="button button-primary" onClick={() => setShowForm(true)}>
                {t("knowledge.createBase")}
              </button>
            }
          />
        )}

        {bases.loaded && !bases.error && list.length > 0 && filteredBases.length === 0 && (
          <CatalogEmptyNotice title={copy.noMatch} hint={copy.noMatchHint} />
        )}

        {filteredBases.length > 0 && (
          <div className="data-table">
            <table>
              <thead>
                <tr>
                  <th scope="col">{t("knowledge.baseName")}</th>
                  <th scope="col">{t("common.id")}</th>
                  <th scope="col">{t("common.created")}</th>
                  <th scope="col">{t("common.actions")}</th>
                </tr>
              </thead>
              <tbody>
                {filteredBases.map((base) => (
                  <tr key={base.id}>
                    <td data-label={t("knowledge.baseName")}>
                      <Link href={`/knowledge/${base.id}`}>{base.name}</Link>
                    </td>
                    <td data-label={t("common.id")}>
                      <HashValue value={base.id} label={t("common.id")} />
                    </td>
                    <td data-label={t("common.created")}>
                      {base.created_at ? formatDateTime(base.created_at) : t("common.none")}
                    </td>
                    <td data-label={t("common.actions")}>
                      <Link className="button button-ghost" href={`/knowledge/${base.id}`}>
                        {t("knowledge.manageDocuments")}
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>
    </div>
  );
}
