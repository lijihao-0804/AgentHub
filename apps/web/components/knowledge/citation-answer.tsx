"use client";

import { useState } from "react";

import Select from "@/components/ui/select";
import { InlineError } from "@/components/ui/states";
import { useWorkspaceData, useWorkspaceMutation } from "@/hooks/use-workspace-data";
import { useI18n } from "@/i18n/provider";
import { listModelProfiles } from "@/lib/api/models";
import { askSnapshot, type SnapshotAnswer } from "@/lib/api/retrieval-playground";
import { formatLocator } from "@/lib/format/locator";

export default function CitationAnswer({ knowledgeBaseId, snapshotId, query }: {
  knowledgeBaseId: string;
  snapshotId: string;
  query: string;
}) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [profile, setProfile] = useState("");
  const [response, setResponse] = useState<{ question: string; result: SnapshotAnswer } | null>(null);
  const profiles = useWorkspaceData(listModelProfiles, "citation-qa-profiles", { enabled: open });
  const submission = useWorkspaceMutation(`citation-qa:${knowledgeBaseId}:${snapshotId}`);
  const models = (profiles.data ?? []).filter((item) => item.enabled);
  const activeProfile = models.some((item) => item.id === profile) ? profile : models[0]?.id;

  async function ask() {
    if (!activeProfile || !query || submission.pending) return;
    setResponse(null);
    await submission.run(
      (auth) => askSnapshot(auth, knowledgeBaseId, snapshotId, activeProfile, query),
      (result) => setResponse({ question: query, result }),
    );
  }

  return (
    <details className="panel" onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary>{t("playground.citedAnswer")}</summary>
      {open && (
        <div className="stack">
          <label>
            {t("playground.answerModel")}
            <Select ariaLabel={t("playground.answerModel")} value={activeProfile ?? ""} onChange={setProfile} disabled={!models.length || submission.pending}
              options={models.map((item) => ({ value: item.id, label: item.model }))} />
          </label>
          <InlineError error={profiles.error ?? submission.error} fallback={t("errors.requestFailed")} />
          {profiles.loaded && !models.length && <p>{t("playground.noAnswerModels")}</p>}
          <button type="button" className="button button-primary" onClick={() => void ask()}
            disabled={!activeProfile || !query || submission.pending}>
            {submission.pending ? t("playground.answering") : t("playground.ask")}
          </button>
          {response && (
            <article aria-live="polite" style={{ overflowWrap: "anywhere" }}>
              <h3>{response.question}</h3>
              <p style={{ whiteSpace: "pre-wrap" }}>{response.result.answer}</p>
              {response.result.citations.map((citation) => (
                <details key={citation.id} className="advanced-options">
                  <summary>[{citation.id}] {citation.source} · {formatLocator(citation.locator, t)}</summary>
                  <p style={{ whiteSpace: "pre-wrap" }}>{citation.excerpt}</p>
                  <p className="state-hint">{t("playground.sourceRevision")} <code>{citation.document_revision_id}</code></p>
                  {citation.components?.length ? (
                    <ol>
                      {citation.components.map((component) => (
                        <li key={component.chunk_id}>
                          <details>
                            <summary>{formatLocator(component.locator, t)} · <code>{component.chunk_id}</code></summary>
                            <p style={{ whiteSpace: "pre-wrap" }}>{component.excerpt}</p>
                          </details>
                        </li>
                      ))}
                    </ol>
                  ) : <code>{citation.chunk_id}</code>}
                </details>
              ))}
            </article>
          )}
        </div>
      )}
    </details>
  );
}
