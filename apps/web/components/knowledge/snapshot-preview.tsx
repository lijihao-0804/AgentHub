"use client";

import { useState } from "react";
import { useFrontendSession } from "@/components/providers/session-provider";
import { useWorkspaceData } from "@/hooks/use-workspace-data";
import { useI18n } from "@/i18n/provider";
import { previewSnapshotChunks } from "@/lib/api/knowledge";

export default function SnapshotPreview(props: { knowledgeBaseId: string; snapshotId: string }) {
  const { sessionId } = useFrontendSession();
  return <SnapshotChunkPreview key={`${sessionId}:${props.knowledgeBaseId}:${props.snapshotId}`} {...props} />;
}

export function SnapshotChunkPreview({ knowledgeBaseId, snapshotId }: { knowledgeBaseId: string; snapshotId: string }) {
  const { t, formatNumber } = useI18n();
  const { permissions } = useFrontendSession();
  const allowed = permissions?.includes("knowledge_run") === true;
  const [open, setOpen] = useState(false);
  const [offset, setOffset] = useState(0);
  const page = useWorkspaceData((auth) => previewSnapshotChunks(auth, knowledgeBaseId, snapshotId, offset),
    `snapshot-preview:${knowledgeBaseId}:${snapshotId}:${offset}`, { enabled: open && allowed });
  return <div>
    <button type="button" className="button button-ghost" disabled={!allowed} onClick={() => setOpen(!open)} aria-expanded={open}>
      {t("snapshotPreview.open")}
    </button>
    {!allowed && <p className="state-hint">{t("snapshotPreview.permission")}</p>}
    {open && allowed && <section aria-label={t("snapshotPreview.open")}>
      {page.loading && <p role="status">{t("common.loading")}</p>}
      {page.error && <div role="alert"><p>{t("snapshotPreview.error")}</p><button type="button" onClick={page.reload}>{t("snapshotPreview.retry")}</button></div>}
      {page.loaded && page.data && page.data.offset === offset && <>
        <p>{t("snapshotPreview.total")}: {formatNumber(page.data.total)}</p>
        {page.data.items.length === 0 && <p>{t("snapshotPreview.empty")}</p>}
        {page.data.items.map((item) => <article key={item.chunk_id} style={{ minWidth: 0, overflowWrap: "anywhere" }}>
          <p><code>{item.chunk_id}</code> · <code>{item.document_revision_id}</code></p>
          <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", maxHeight: "20rem", overflow: "auto" }}>{item.text}</pre>
          {item.truncated && <p>{t("snapshotPreview.truncated")}</p>}
        </article>)}
        <button type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 10))}>{t("snapshotPreview.previous")}</button>
        <button type="button" disabled={offset + page.data.items.length >= page.data.total} onClick={() => setOffset(offset + 10)}>{t("snapshotPreview.next")}</button>
      </>}
    </section>}
  </div>;
}
