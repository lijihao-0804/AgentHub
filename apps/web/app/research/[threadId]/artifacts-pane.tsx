"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import PaperCard from "@/app/research/[threadId]/paper-card";
import { InlineConfirm } from "@/components/evaluation/hash-value";
import { EmptyState, ErrorState, InlineError, LoadingState } from "@/components/ui/states";
import { useWorkspaceMutation, type WorkspaceQuery } from "@/hooks/use-workspace-data";
import { errorHintKey } from "@/lib/api/client";
import {
  artifactPapers,
  createThreadArtifact,
  deleteArtifact,
  paperPayload,
  patchArtifact,
  readPaperSearchContent,
  readPaperShortlistContent,
  PAPER_SEARCH,
  PAPER_SHORTLIST,
  type Artifact,
  type PaperRecord,
} from "@/lib/api/artifacts";
import { useI18n } from "@/i18n/provider";
import type { MessageKey } from "@/i18n/messages";

const EMPTY_FILTERS = { titleContains: "", yearFrom: "", yearTo: "" };

type Filters = typeof EMPTY_FILTERS;

const TYPE_LABEL_KEY: Record<string, MessageKey> = {
  [PAPER_SEARCH]: "research.artifacts.types.paperSearch",
  [PAPER_SHORTLIST]: "research.artifacts.types.paperShortlist",
};

function yearBound(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const parsed = Number(trimmed);
  return Number.isInteger(parsed) ? parsed : null;
}

/** Client-side only: the filter narrows what is on screen, never the stored artifact. */
function matchesFilters(paper: PaperRecord, filters: Filters): boolean {
  const needle = filters.titleContains.trim().toLowerCase();
  if (needle && !paper.title.toLowerCase().includes(needle)) return false;
  const from = yearBound(filters.yearFrom);
  const to = yearBound(filters.yearTo);
  if (from !== null && (paper.year === null || paper.year < from)) return false;
  if (to !== null && (paper.year === null || paper.year > to)) return false;
  return true;
}

export default function ArtifactsPane({
  threadId,
  artifacts,
}: {
  threadId: string;
  artifacts: WorkspaceQuery<Artifact[]>;
}) {
  const { t, formatDateTime, formatNumber } = useI18n();
  const mutation = useWorkspaceMutation(`research-artifacts:${threadId}`);

  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  /** Empty means "a shortlist that does not exist yet". */
  const [targetShortlistId, setTargetShortlistId] = useState("");
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [titleDraft, setTitleDraft] = useState("");
  const [deletingId, setDeletingId] = useState<string | null>(null);

  useEffect(() => {
    setFilters(EMPTY_FILTERS);
    setTargetShortlistId("");
    setRenamingId(null);
    setDeletingId(null);
  }, [threadId]);

  const artifactList = artifacts.data ?? [];
  // A run-produced artifact is a record, so it is never a save target.
  const shortlists = artifactList.filter(
    (artifact) => artifact.type === PAPER_SHORTLIST && artifact.run_id === null,
  );
  const target = shortlists.find((artifact) => artifact.id === targetShortlistId) ?? null;
  const savedIds = new Set(target ? artifactPapers(target).map((paper) => paper.paper_id) : []);

  async function savePaper(paper: PaperRecord) {
    if (target) {
      if (savedIds.has(paper.paper_id)) return;
      const content = readPaperShortlistContent(target.content);
      const result = await mutation.run((auth) =>
        patchArtifact(auth, target.id, {
          content: {
            note: content.note,
            papers: [...content.papers.map(paperPayload), paperPayload(paper)],
          },
        }),
      );
      if (result) artifacts.reload();
      return;
    }
    const created = await mutation.run((auth) =>
      createThreadArtifact(auth, threadId, {
        type: PAPER_SHORTLIST,
        title: t("research.artifacts.newShortlistTitle"),
        content: { note: null, papers: [paperPayload(paper)] },
      }),
    );
    if (created) {
      setTargetShortlistId(created.id);
      artifacts.reload();
    }
  }

  async function removePaper(artifact: Artifact, paperId: string) {
    const content = readPaperShortlistContent(artifact.content);
    const result = await mutation.run((auth) =>
      patchArtifact(auth, artifact.id, {
        content: {
          note: content.note,
          papers: content.papers.filter((paper) => paper.paper_id !== paperId).map(paperPayload),
        },
      }),
    );
    if (result) artifacts.reload();
  }

  async function renameArtifact(artifact: Artifact) {
    const title = titleDraft.trim();
    if (!title) return;
    const result = await mutation.run((auth) => patchArtifact(auth, artifact.id, { title }));
    if (result) {
      setRenamingId(null);
      artifacts.reload();
    }
  }

  async function removeArtifact(artifact: Artifact) {
    const result = await mutation.run(async (auth) => {
      await deleteArtifact(auth, artifact.id);
      return true;
    });
    if (result) {
      setDeletingId(null);
      if (targetShortlistId === artifact.id) setTargetShortlistId("");
      artifacts.reload();
    }
  }

  return (
    <section className="research-pane research-pane-artifacts" aria-label={t("research.artifacts.paneTitle")}>
      <div className="research-pane-head">
        <h2>{t("research.artifacts.paneTitle")}</h2>
        <button type="button" className="button button-ghost" onClick={artifacts.reload} disabled={artifacts.loading}>
          {t("common.refresh")}
        </button>
      </div>

      <div className="research-pane-body">
        <div className="artifact-toolbar" role="group" aria-label={t("research.artifacts.filtersTitle")}>
          <label>
            {t("research.artifacts.titleContains")}
            <input
              value={filters.titleContains}
              onChange={(event) =>
                setFilters((current) => ({ ...current, titleContains: event.target.value }))
              }
              placeholder={t("research.artifacts.titleContainsPlaceholder")}
            />
          </label>
          <label>
            {t("research.artifacts.yearFrom")}
            <input
              type="number"
              value={filters.yearFrom}
              onChange={(event) => setFilters((current) => ({ ...current, yearFrom: event.target.value }))}
            />
          </label>
          <label>
            {t("research.artifacts.yearTo")}
            <input
              type="number"
              value={filters.yearTo}
              onChange={(event) => setFilters((current) => ({ ...current, yearTo: event.target.value }))}
            />
          </label>
          <label>
            {t("research.artifacts.saveTo")}
            <select value={targetShortlistId} onChange={(event) => setTargetShortlistId(event.target.value)}>
              <option value="">{t("research.artifacts.newShortlist")}</option>
              {shortlists.map((shortlist) => (
                <option value={shortlist.id} key={shortlist.id}>
                  {shortlist.title}
                </option>
              ))}
            </select>
          </label>
          <button type="button" className="button button-ghost" onClick={() => setFilters(EMPTY_FILTERS)}>
            {t("research.artifacts.clearFilters")}
          </button>
        </div>

        <InlineError error={mutation.error} fallback={t("errors.requestFailed")} />

        {artifacts.error && (
          <ErrorState
            code={artifacts.error.code}
            message={artifacts.error.message || t("errors.loadArtifacts")}
            hint={errorHintKey(artifacts.error) ? t(errorHintKey(artifacts.error)!) : undefined}
            onRetry={artifacts.reload}
          />
        )}
        {artifacts.loading && artifactList.length === 0 && !artifacts.error && <LoadingState />}
        {artifacts.loaded && !artifacts.error && artifactList.length === 0 && (
          <EmptyState title={t("research.artifacts.empty")} hint={t("research.artifacts.emptyHint")} />
        )}

        <div className="artifact-list">
          {artifactList.map((artifact) => {
            const editable = artifact.run_id === null;
            const isShortlist = artifact.type === PAPER_SHORTLIST;
            const papers = artifactPapers(artifact);
            const shown = papers.filter((paper) => matchesFilters(paper, filters));
            const search = artifact.type === PAPER_SEARCH ? readPaperSearchContent(artifact.content) : null;
            const shortlist = isShortlist ? readPaperShortlistContent(artifact.content) : null;
            const typeKey = TYPE_LABEL_KEY[artifact.type];

            return (
              <article className="artifact-card" key={artifact.id}>
                <header className="artifact-card-head">
                  <div>
                    {renamingId === artifact.id ? (
                      <div className="artifact-rename">
                        <label>
                          {t("research.artifacts.renameLabel")}
                          <input
                            value={titleDraft}
                            onChange={(event) => setTitleDraft(event.target.value)}
                            maxLength={300}
                          />
                        </label>
                        <button
                          type="button"
                          className="button button-primary"
                          onClick={() => void renameArtifact(artifact)}
                          disabled={mutation.pending || !titleDraft.trim()}
                        >
                          {t("common.save")}
                        </button>
                        <button
                          type="button"
                          className="button button-ghost"
                          onClick={() => setRenamingId(null)}
                          disabled={mutation.pending}
                        >
                          {t("research.cancel")}
                        </button>
                      </div>
                    ) : (
                      <h3>{artifact.title}</h3>
                    )}
                    <p className="artifact-card-meta">
                      <span className="artifact-type">{typeKey ? t(typeKey) : artifact.type}</span>
                      <span>{t("research.artifacts.paperCount", { count: formatNumber(papers.length) })}</span>
                      <span>{formatDateTime(artifact.updated_at)}</span>
                    </p>
                    <p className="artifact-card-meta">
                      {artifact.run_id ? (
                        <>
                          <span className="artifact-readonly">{t("research.artifacts.readOnly")}</span>
                          <span>
                            {t("research.artifacts.producedBy")}{" "}
                            <Link href={`/runs/${encodeURIComponent(artifact.run_id)}`} title={artifact.run_id}>
                              {t("research.conversation.runLabel", { id: artifact.run_id.slice(0, 8) })}
                            </Link>
                          </span>
                        </>
                      ) : (
                        <span>{t("research.artifacts.manual")}</span>
                      )}
                    </p>
                  </div>
                  {editable && renamingId !== artifact.id && (
                    <div className="artifact-card-actions">
                      <button
                        type="button"
                        className="button button-ghost"
                        onClick={() => {
                          setRenamingId(artifact.id);
                          setTitleDraft(artifact.title);
                        }}
                      >
                        {t("research.artifacts.rename")}
                      </button>
                      <button
                        type="button"
                        className="button button-ghost"
                        onClick={() => setDeletingId(artifact.id)}
                      >
                        {t("research.artifacts.delete")}
                      </button>
                    </div>
                  )}
                </header>

                {deletingId === artifact.id && (
                  <InlineConfirm
                    title={t("research.artifacts.delete")}
                    text={t("research.artifacts.confirmDelete")}
                    confirmLabel={t("research.artifacts.delete")}
                    pendingLabel={t("common.loading")}
                    cancelLabel={t("research.cancel")}
                    pending={mutation.pending}
                    onConfirm={() => void removeArtifact(artifact)}
                    onCancel={() => setDeletingId(null)}
                  />
                )}

                {search && (
                  <dl className="artifact-facts">
                    <div>
                      <dt>{t("research.artifacts.query")}</dt>
                      <dd>{search.query}</dd>
                    </div>
                    <div>
                      <dt>{t("research.artifacts.source")}</dt>
                      <dd>
                        <code>{search.source}</code>
                      </dd>
                    </div>
                    <div>
                      <dt>{t("research.artifacts.total")}</dt>
                      <dd>{search.total === null ? t("common.none") : formatNumber(search.total)}</dd>
                    </div>
                  </dl>
                )}
                {shortlist?.note && <p className="artifact-note">{shortlist.note}</p>}

                <p className="artifact-card-meta">
                  {t("research.artifacts.filtered", {
                    shown: formatNumber(shown.length),
                    total: formatNumber(papers.length),
                  })}
                </p>

                {papers.length === 0 ? (
                  <p className="state-hint">{t("research.artifacts.noPapers")}</p>
                ) : (
                  <div className="paper-list">
                    {shown.map((paper) => (
                      <PaperCard
                        key={`${artifact.id}:${paper.paper_id}`}
                        paper={paper}
                        showProvenance
                        actions={
                          isShortlist && editable ? (
                            <button
                              type="button"
                              className="button button-ghost"
                              onClick={() => void removePaper(artifact, paper.paper_id)}
                              disabled={mutation.pending}
                            >
                              {t("research.artifacts.remove")}
                            </button>
                          ) : (
                            <button
                              type="button"
                              className="button button-ghost"
                              onClick={() => void savePaper(paper)}
                              disabled={mutation.pending || savedIds.has(paper.paper_id)}
                            >
                              {savedIds.has(paper.paper_id)
                                ? t("research.artifacts.saved")
                                : t("research.artifacts.save")}
                            </button>
                          )
                        }
                      />
                    ))}
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
