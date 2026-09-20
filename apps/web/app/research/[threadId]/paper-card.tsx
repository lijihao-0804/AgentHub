"use client";

import Link from "next/link";
import type { ReactNode } from "react";

import type { PaperRecord } from "@/lib/api/artifacts";
import { useI18n } from "@/i18n/provider";

/**
 * One paper, rendered as a record rather than as a line of prose.
 *
 * A numbered list of titles hides exactly the fields a reader uses to judge a
 * paper — who wrote it, when, where, how often it is cited, and which tool
 * call produced it — so each of those gets its own place here. The abstract is
 * collapsed because it is the one field that would otherwise bury the rest.
 */
export default function PaperCard({
  paper,
  actions,
  showProvenance,
}: {
  paper: PaperRecord;
  actions?: ReactNode;
  showProvenance?: boolean;
}) {
  const { t, formatNumber } = useI18n();
  const link = paper.url ?? (paper.doi ? `https://doi.org/${encodeURIComponent(paper.doi)}` : null);

  return (
    <article className="paper-card">
      <div className="paper-card-head">
        <h4 className="paper-card-title">
          {link ? (
            <Link href={link} target="_blank" rel="noreferrer noopener">
              {paper.title}
            </Link>
          ) : (
            paper.title
          )}
        </h4>
        {actions && <div className="paper-card-actions">{actions}</div>}
      </div>

      <p className="paper-card-authors">
        {paper.authors.length > 0 ? paper.authors.join(", ") : t("research.artifacts.unknownAuthors")}
      </p>

      <dl className="paper-card-facts">
        <div>
          <dt>{t("research.artifacts.year")}</dt>
          {/* A year is a label, not a quantity: grouping separators turn 2023
              into "2,023". Citation counts below are quantities and do want them. */}
          <dd>{paper.year === null ? t("common.none") : String(paper.year)}</dd>
        </div>
        <div>
          <dt>{t("research.artifacts.venue")}</dt>
          <dd>{paper.venue ?? t("common.none")}</dd>
        </div>
        <div>
          <dt>{t("research.artifacts.citations")}</dt>
          <dd>{paper.citation_count === null ? t("common.none") : formatNumber(paper.citation_count)}</dd>
        </div>
        {paper.doi && (
          <div>
            <dt>{t("research.artifacts.doi")}</dt>
            <dd>
              <code>{paper.doi}</code>
            </dd>
          </div>
        )}
      </dl>

      <details className="paper-card-abstract">
        <summary>{t("research.artifacts.abstract")}</summary>
        <p>{paper.abstract ?? t("research.artifacts.noAbstract")}</p>
      </details>

      {showProvenance && paper.provenance && (
        <p className="paper-card-provenance">
          <span>{t("research.artifacts.provenance")}</span>
          <span>
            {t("research.artifacts.provenanceTool")}: <code>{paper.provenance.tool_identity}</code>
          </span>
          {paper.provenance.step_sequence !== null && (
            <span>
              {t("research.artifacts.provenanceStep")}: {formatNumber(paper.provenance.step_sequence)}
            </span>
          )}
          <Link href={`/runs/${encodeURIComponent(paper.provenance.run_id)}`}>
            {t("research.artifacts.provenanceRun")}
          </Link>
        </p>
      )}
    </article>
  );
}
