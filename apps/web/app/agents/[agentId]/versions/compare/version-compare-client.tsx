"use client";

import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";

import StatusBadge from "@/components/ui/status-badge";
import Breadcrumbs from "@/components/layout/breadcrumbs";
import { EmptyState, ErrorState, LoadingState, Panel, SessionRequired } from "@/components/ui/states";
import TechnicalDetails from "@/components/ui/technical-details";
import { useFrontendSession } from "@/components/providers/session-provider";
import { useWorkspaceData } from "@/hooks/use-workspace-data";
import { errorHintKey, type ApiError, type AuthInput } from "@/lib/api/client";
import { getAgentVersion, listAgentVersions, type AgentVersion } from "@/lib/api/agents";
import {
  diffResolvedSpecs,
  formatScalar,
  isComplexValue,
  type ChangeType,
  type DiffEntry,
  type DiffGroupKey,
} from "@/lib/version-diff";
import { useI18n } from "@/i18n/provider";
import type { MessageKey } from "@/i18n/messages";

const CHANGE_LABEL: Record<ChangeType, MessageKey> = {
  ADDED: "agents.versionDiff.changeType.ADDED",
  REMOVED: "agents.versionDiff.changeType.REMOVED",
  CHANGED: "agents.versionDiff.changeType.CHANGED",
  UNCHANGED: "agents.versionDiff.changeType.UNCHANGED",
};

const GROUP_LABEL: Record<DiffGroupKey, MessageKey> = {
  model: "agents.versionDiff.group.model",
  prompt: "agents.versionDiff.group.prompt",
  knowledge: "agents.versionDiff.group.knowledge",
  tools: "agents.versionDiff.group.tools",
  runtime: "agents.versionDiff.group.runtime",
  other: "agents.versionDiff.group.other",
};

/**
 * Structural comparison of two published versions of one agent.
 *
 * Both sides are read from the immutable version endpoint, so the draft is
 * never part of a comparison. The page reports differences only: there is
 * deliberately no winner, score or recommendation here.
 */
export default function VersionCompareClient({ agentId }: { agentId: string }) {
  const { t, formatNumber } = useI18n();
  const { connected, workspaceId, sessionId } = useFrontendSession();

  const [leftId, setLeftId] = useState("");
  const [rightId, setRightId] = useState("");
  const [showUnchanged, setShowUnchanged] = useState(false);

  const loadVersions = useCallback((auth: AuthInput) => listAgentVersions(auth, agentId), [agentId]);
  const versions = useWorkspaceData<AgentVersion[]>(
    loadVersions,
    `agent-versions:${workspaceId}:${agentId}`,
  );
  const versionList = useMemo(() => versions.data ?? [], [versions.data]);

  // The URL is the only place a selection is stored; there is no
  // comparison record on the server and none is created here.
  useEffect(() => {
    setShowUnchanged(false);
    if (typeof window === "undefined") {
      setLeftId("");
      setRightId("");
      return;
    }
    const params = new URLSearchParams(window.location.search);
    setLeftId(params.get("left") ?? "");
    setRightId(params.get("right") ?? "");
  }, [sessionId, agentId]);

  // A pointer that survived a workspace switch belongs to another
  // workspace's data; it is dropped rather than requested.
  useEffect(() => {
    if (!versions.loaded) return;
    const known = (id: string) => id === "" || versionList.some((version) => version.id === id);
    if (!known(leftId)) setLeftId("");
    if (!known(rightId)) setRightId("");
  }, [versions.loaded, versionList, leftId, rightId]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const url = new URL(window.location.href);
    if (leftId) url.searchParams.set("left", leftId);
    else url.searchParams.delete("left");
    if (rightId) url.searchParams.set("right", rightId);
    else url.searchParams.delete("right");
    window.history.replaceState(null, "", `${url.pathname}${url.search}`);
  }, [leftId, rightId]);

  const comparable = leftId !== "" && rightId !== "" && leftId !== rightId;

  const loadLeft = useCallback(
    (auth: AuthInput) => getAgentVersion(auth, agentId, leftId),
    [agentId, leftId],
  );
  const loadRight = useCallback(
    (auth: AuthInput) => getAgentVersion(auth, agentId, rightId),
    [agentId, rightId],
  );
  const left = useWorkspaceData<AgentVersion>(
    loadLeft,
    comparable ? `agent-version:${workspaceId}:${agentId}:${leftId}` : "",
    { enabled: comparable },
  );
  const right = useWorkspaceData<AgentVersion>(
    loadRight,
    comparable ? `agent-version:${workspaceId}:${agentId}:${rightId}` : "",
    { enabled: comparable },
  );

  const diff = useMemo(() => {
    if (!left.data || !right.data) return null;
    return diffResolvedSpecs(left.data.resolved_spec, right.data.resolved_spec);
  }, [left.data, right.data]);

  if (!connected) {
    return (
      <div className="page">
        <header className="page-header">
          <p className="eyebrow">{t("agents.eyebrow")}</p>
          <h1>{t("agents.versionDiff.title")}</h1>
        </header>
        <SessionRequired contextKey="session.context.agents" />
      </div>
    );
  }

  function renderError(error: ApiError, retry: () => void) {
    const hint = errorHintKey(error);
    return (
      <ErrorState
        code={error.code}
        message={error.message || t("errors.loadVersions")}
        hint={hint ? t(hint) : undefined}
        onRetry={retry}
      />
    );
  }

  function valueBlock(label: string, value: unknown, present: boolean): ReactNode {
    if (!present) return <span className="muted">{t("agents.versionDiff.absent")}</span>;
    if (isComplexValue(value)) return <TechnicalDetails summary={label} value={value} />;
    return <code className="mono-area">{formatScalar(value)}</code>;
  }

  function renderEntry(entry: DiffEntry): ReactNode {
    const hasBefore = entry.changeType !== "ADDED";
    const hasAfter = entry.changeType !== "REMOVED";
    const beforeLabel = t("agents.versionDiff.before");
    const afterLabel = t("agents.versionDiff.after");
    // Two short scalars read better as `8 → 12` than as two blocks.
    const inline =
      entry.changeType === "CHANGED" && !isComplexValue(entry.before) && !isComplexValue(entry.after);

    return (
      <article className="diff-entry" key={entry.path}>
        <div className="diff-entry-head">
          <code>{entry.path}</code>
          <StatusBadge
            status={entry.changeType}
            label={t(CHANGE_LABEL[entry.changeType])}
            tone={entry.changeType === "UNCHANGED" ? "neutral" : "info"}
          />
        </div>
        {inline ? (
          <p className="diff-inline">
            <code className="mono-area">{formatScalar(entry.before)}</code>
            <span aria-hidden="true">→</span>
            <code className="mono-area">{formatScalar(entry.after)}</code>
          </p>
        ) : (
          <div className="approval-split">
            <div className="approval-state-block">
              <span className="approval-state-label">{beforeLabel}</span>
              {valueBlock(beforeLabel, entry.before, hasBefore)}
            </div>
            <div className="approval-state-block">
              <span className="approval-state-label">{afterLabel}</span>
              {valueBlock(afterLabel, entry.after, hasAfter)}
            </div>
          </div>
        )}
      </article>
    );
  }

  const leftVersion = left.data;
  const rightVersion = right.data;
  const loadingSides = comparable && (left.loading || right.loading) && !diff;

  return (
    <div className="page">
      <Breadcrumbs
        items={[
          { label: t("nav.agents"), href: "/agents" },
          { label: t("agents.detail"), href: `/agents/${agentId}` },
          { label: t("agents.versionDiff.title") },
        ]}
      />
      <header className="page-header">
        <p className="eyebrow">{t("agents.eyebrow")}</p>
        <h1>{t("agents.versionDiff.title")}</h1>
        <p className="page-lede">{t("agents.versionDiff.lede")}</p>
      </header>

      <Panel title={t("agents.versionDiff.title")} ariaLabel={t("agents.versionDiff.title")}>
        {versions.error && renderError(versions.error, versions.reload)}
        {versions.loading && !versions.error && <LoadingState />}
        {versions.loaded && !versions.error && versionList.length < 2 && (
          <EmptyState title={t("agents.versionDiff.needTwoVersions")} />
        )}

        {versionList.length >= 2 && (
          <>
            <div className="form-grid compare-selectors">
              <label>
                {t("agents.versionDiff.left")}
                <select value={leftId} onChange={(event) => setLeftId(event.target.value)}>
                  <option value="">{t("agents.versionDiff.selectPlaceholder")}</option>
                  {versionList.map((version) => (
                    <option key={version.id} value={version.id}>
                      v{version.version_number}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                {t("agents.versionDiff.right")}
                <select value={rightId} onChange={(event) => setRightId(event.target.value)}>
                  <option value="">{t("agents.versionDiff.selectPlaceholder")}</option>
                  {versionList.map((version) => (
                    <option key={version.id} value={version.id}>
                      v{version.version_number}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            {leftId !== "" && leftId === rightId && (
              <p className="state-hint">{t("agents.versionDiff.sameVersion")}</p>
            )}
            {(leftId === "" || rightId === "") && (
              <p className="state-hint">{t("agents.versionDiff.selectBoth")}</p>
            )}
          </>
        )}
      </Panel>

      {comparable && left.error && (
        <Panel ariaLabel={t("agents.versionDiff.left")}>{renderError(left.error, left.reload)}</Panel>
      )}
      {comparable && right.error && (
        <Panel ariaLabel={t("agents.versionDiff.right")}>{renderError(right.error, right.reload)}</Panel>
      )}
      {loadingSides && !left.error && !right.error && (
        <Panel ariaLabel={t("agents.versionDiff.title")}>
          <LoadingState />
        </Panel>
      )}

      {diff && leftVersion && rightVersion && (
        <>
          <Panel
            ariaLabel={t("agents.versionDiff.changedFields")}
            eyebrow={t("agents.versionDiff.title")}
            title={t("agents.versionDiff.heading", {
              left: leftVersion.version_number,
              right: rightVersion.version_number,
            })}
          >
            <div className="run-facts">
              <span>
                {t("agents.versionDiff.changedFields")}
                <strong>{formatNumber(diff.summary.changedFields)}</strong>
              </span>
              <span>
                {t("agents.versionDiff.added")}
                <strong>{formatNumber(diff.summary.added)}</strong>
              </span>
              <span>
                {t("agents.versionDiff.removed")}
                <strong>{formatNumber(diff.summary.removed)}</strong>
              </span>
              <span>
                {t("agents.versionDiff.changed")}
                <strong>{formatNumber(diff.summary.changed)}</strong>
              </span>
              <span>
                {t("agents.versionDiff.unchanged")}
                <strong>{formatNumber(diff.summary.unchanged)}</strong>
              </span>
            </div>
            <div className="approval-split">
              <div className="approval-state-block">
                <span className="approval-state-label">{t("agents.versionDiff.left")}</span>
                <span>v{leftVersion.version_number}</span>
                <span className="approval-state-meta">
                  <code className="hash-value">{leftVersion.resolved_spec_hash}</code>
                </span>
              </div>
              <div className="approval-state-block">
                <span className="approval-state-label">{t("agents.versionDiff.right")}</span>
                <span>v{rightVersion.version_number}</span>
                <span className="approval-state-meta">
                  <code className="hash-value">{rightVersion.resolved_spec_hash}</code>
                </span>
              </div>
            </div>
            <label className="checkbox-label compare-toggle">
              <input
                type="checkbox"
                checked={showUnchanged}
                onChange={(event) => setShowUnchanged(event.target.checked)}
              />
              {t("agents.versionDiff.showUnchanged")}
            </label>
          </Panel>

          {diff.summary.changedFields === 0 && !showUnchanged && (
            <Panel ariaLabel={t("agents.versionDiff.noChanges")}>
              <EmptyState title={t("agents.versionDiff.noChanges")} />
            </Panel>
          )}

          {diff.groups.map((group) => {
            const entries = group.entries.filter(
              (entry) => showUnchanged || entry.changeType !== "UNCHANGED",
            );
            if (entries.length === 0) return null;
            return (
              <Panel key={group.key} title={t(GROUP_LABEL[group.key])}>
                <div className="diff-list">{entries.map((entry) => renderEntry(entry))}</div>
              </Panel>
            );
          })}
        </>
      )}
    </div>
  );
}
