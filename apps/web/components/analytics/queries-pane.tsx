"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { EmptyState, ErrorState, LoadingState } from "@/components/ui/states";
import type { WorkspaceQuery } from "@/hooks/use-workspace-data";
import { errorHintKey } from "@/lib/api/client";
import {
  readAnalysisQueryResultContent,
  ANALYSIS_QUERY_RESULT,
  type AnalysisQueryResultContent,
  type Artifact,
  type QueryCell,
} from "@/lib/api/artifacts";
import { useI18n } from "@/i18n/provider";

/**
 * Every `analysis.query_result` on the thread, oldest first.
 *
 * The order is the order the runs recorded them in, because a drill-down is
 * a sequence: the third query only makes sense after the two that narrowed it.
 */
export function queryResults(artifacts: Artifact[]): { artifact: Artifact; query: AnalysisQueryResultContent }[] {
  return artifacts
    .filter((artifact) => artifact.type === ANALYSIS_QUERY_RESULT)
    .slice()
    .sort((left, right) => (left.created_at < right.created_at ? -1 : left.created_at > right.created_at ? 1 : 0))
    .map((artifact) => ({ artifact, query: readAnalysisQueryResultContent(artifact.content) }));
}

export default function QueriesPane({
  threadId,
  artifacts,
}: {
  threadId: string;
  artifacts: WorkspaceQuery<Artifact[]>;
}) {
  const { t, formatDateTime, formatNumber } = useI18n();

  /** Which SQL blocks the reader opened. The rows lead; the SQL is the audit. */
  const [openSql, setOpenSql] = useState<string[]>([]);

  useEffect(() => {
    setOpenSql([]);
  }, [threadId]);

  const results = queryResults(artifacts.data ?? []);

  /**
   * A cell is rendered as the tool returned it. Numbers are not grouped and
   * booleans are not re-worded: these are query results, and a reader
   * comparing them against the database must see the same characters.
   */
  function cellText(cell: QueryCell): string {
    return cell === null ? t("common.none") : String(cell);
  }

  return (
    <section className="research-pane" aria-label={t("analytics.queries.paneTitle")}>
      <div className="research-pane-head">
        <h2>{t("analytics.queries.paneTitle")}</h2>
        <button
          type="button"
          className="button button-ghost"
          onClick={artifacts.reload}
          disabled={artifacts.loading}
        >
          {t("common.refresh")}
        </button>
      </div>

      <div className="research-pane-body">
        {artifacts.error && (
          <ErrorState
            code={artifacts.error.code}
            message={artifacts.error.message || t("errors.loadArtifacts")}
            hint={errorHintKey(artifacts.error) ? t(errorHintKey(artifacts.error)!) : undefined}
            onRetry={artifacts.reload}
          />
        )}
        {artifacts.loading && results.length === 0 && !artifacts.error && <LoadingState />}
        {artifacts.loaded && !artifacts.error && results.length === 0 && (
          <EmptyState title={t("analytics.queries.empty")} hint={t("analytics.queries.emptyHint")} />
        )}

        <div className="artifact-list">
          {results.map(({ artifact, query }) => {
            const sqlOpen = openSql.includes(artifact.id);
            const rowCount = query.row_count ?? query.rows.length;

            return (
              <article className="artifact-card" key={artifact.id}>
                <header className="artifact-card-head">
                  <div>
                    <h3>{query.question ?? artifact.title}</h3>
                    <p className="artifact-card-meta">
                      <span className="artifact-readonly">{t("appThread.artifacts.readOnly")}</span>
                      <span>{t("analytics.queries.rowCount", { count: formatNumber(rowCount) })}</span>
                      <span>{formatDateTime(artifact.created_at)}</span>
                    </p>
                    {/*
                      A recorded query names the call it came from: nothing in
                      this card was written by a model, and the run is where
                      that can be checked.
                    */}
                    {artifact.run_id && (
                      <p className="artifact-card-meta">
                        <span>
                          {t("appThread.artifacts.producedBy")}{" "}
                          <Link href={`/runs/${encodeURIComponent(artifact.run_id)}`} title={artifact.run_id}>
                            {t("appThread.conversation.runLabel", { id: artifact.run_id.slice(0, 8) })}
                          </Link>
                        </span>
                      </p>
                    )}
                  </div>
                  <div className="artifact-card-actions">
                    <button
                      type="button"
                      className="button button-ghost"
                      onClick={() =>
                        setOpenSql((open) =>
                          open.includes(artifact.id)
                            ? open.filter((id) => id !== artifact.id)
                            : [...open, artifact.id],
                        )
                      }
                      aria-expanded={sqlOpen}
                    >
                      {sqlOpen ? t("analytics.queries.hideSql") : t("analytics.queries.showSql")}
                    </button>
                  </div>
                </header>

                {sqlOpen && (
                  <div className="query-sql">
                    <p className="artifact-card-meta">{t("analytics.queries.sql")}</p>
                    <pre>
                      <code>{query.sql}</code>
                    </pre>
                  </div>
                )}

                {query.truncated && <p className="state-hint">{t("analytics.queries.truncated")}</p>}

                {query.rows.length === 0 ? (
                  <p className="state-hint">{t("analytics.queries.noRows")}</p>
                ) : (
                  <div className="data-table query-table">
                    <table>
                      <thead>
                        <tr>
                          {query.columns.map((column) => (
                            <th key={column} scope="col">
                              {column}
                            </th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {query.rows.map((row, rowIndex) => (
                          // Rows carry no identity of their own — two identical
                          // rows are two facts, not one — so the position is
                          // the key, and the list never reorders.
                          <tr key={`${artifact.id}:${rowIndex}`}>
                            {row.map((cell, cellIndex) => (
                              <td key={`${artifact.id}:${rowIndex}:${cellIndex}`}>{cellText(cell)}</td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </article>
            );
          })}
        </div>
      </div>
    </section>
  );
}
