"use client";

import { EmptyState, ErrorState, LoadingState } from "@/components/ui/states";
import type { WorkspaceQuery } from "@/hooks/use-workspace-data";
import { errorHintKey } from "@/lib/api/client";
import { readSupportHandoffContent, SUPPORT_HANDOFF, type Artifact } from "@/lib/api/artifacts";
import { useI18n } from "@/i18n/provider";

/**
 * The most recent handoff on the case, or null while there is none.
 *
 * Later is better here: a case that was escalated twice was escalated the
 * second time with the references that turned out to be right.
 */
export function latestHandoff(artifacts: Artifact[]): Artifact | null {
  let latest: Artifact | null = null;
  for (const artifact of artifacts) {
    if (artifact.type !== SUPPORT_HANDOFF) continue;
    if (latest === null || artifact.created_at > latest.created_at) latest = artifact;
  }
  return latest;
}

/**
 * Who the case is about, read back from the handoff that carries it.
 *
 * The pane is deliberately read-only. The customer and case references are
 * not fetched from anywhere — they are what a person wrote on the escalation
 * — so showing them anywhere else would be inventing a second source for a
 * fact that has exactly one.
 */
export default function ContextPane({ artifacts }: { artifacts: WorkspaceQuery<Artifact[]> }) {
  const { t, formatDateTime } = useI18n();

  const artifactList = artifacts.data ?? [];
  const latest = latestHandoff(artifactList);
  const content = latest ? readSupportHandoffContent(latest.content) : null;
  const known = content !== null && (content.customer_ref !== null || content.case_ref !== null);

  return (
    <section className="research-pane" aria-label={t("support.context.paneTitle")}>
      <div className="research-pane-head">
        <h2>{t("support.context.paneTitle")}</h2>
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
        {artifacts.loading && artifactList.length === 0 && !artifacts.error && <LoadingState />}

        {known && content && latest ? (
          <dl className="artifact-facts">
            <div>
              <dt>{t("support.context.customerRef")}</dt>
              <dd>{content.customer_ref ?? t("common.none")}</dd>
            </div>
            <div>
              <dt>{t("support.context.caseRef")}</dt>
              <dd>{content.case_ref ?? t("common.none")}</dd>
            </div>
            <div>
              <dt>{t("common.updated")}</dt>
              <dd>{formatDateTime(latest.created_at)}</dd>
            </div>
          </dl>
        ) : (
          artifacts.loaded &&
          !artifacts.error && (
            <EmptyState title={t("support.context.empty")} hint={t("support.context.emptyHint")} />
          )
        )}
      </div>
    </section>
  );
}
