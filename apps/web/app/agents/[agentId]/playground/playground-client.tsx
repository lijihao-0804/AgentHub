"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import StatusBadge from "../../../../components/status-badge";
import { EmptyState, ErrorState, LoadingState, Panel, SessionRequired } from "../../../../components/states";
import TechnicalDetails, { KeyValues } from "../../../../components/technical-details";
import { useFrontendSession } from "../../../../components/session-provider";
import { useWorkspaceData } from "../../../../components/use-workspace-data";
import { ApiError, errorHintKey, toApiError, type AuthInput } from "../../../../lib/api-client";
import { getAgentVersion, listAgentVersions, type AgentVersion } from "../../../../lib/agents";
import {
  cancelAgentRun,
  getAgentRun,
  payloadNumber,
  payloadString,
  streamAgentRun,
  type AgentEvent,
  type AgentRun,
} from "../../../../lib/agent-runtime";
import { decideApproval, listRunApprovals, type Approval } from "../../../../lib/approvals";
import { useI18n } from "../../../../i18n/provider";

/**
 * Agent Playground.
 *
 * One submit is exactly one AgentRun — there is no chat session, thread
 * or message history here. Live execution is driven by the runtime
 * streaming contract; after any pause or terminal event the persisted
 * Run read back from `/agent-runs/{id}` is the authority. Full history
 * stays where it belongs, on the observability Run Detail page.
 */

type ActivityEntry = {
  key: string;
  label: string;
  status?: string;
  meta: string[];
  details?: unknown;
};

type UsageTotals = {
  input: number | null;
  output: number | null;
  total: number | null;
  cached: number | null;
};

/** Mirrors the Approvals inbox: scalars inline, structures stringified. */
function argumentEntries(approval: Approval): Array<[string, unknown]> {
  return Object.entries(approval.canonical_arguments ?? {}).map(([key, value]) => {
    if (
      value === null ||
      value === undefined ||
      typeof value === "string" ||
      typeof value === "number" ||
      typeof value === "boolean"
    ) {
      return [key, value] as [string, unknown];
    }
    return [key, JSON.stringify(value)] as [string, unknown];
  });
}

function latestPublished(versions: AgentVersion[]): AgentVersion | null {
  return versions.reduce<AgentVersion | null>(
    (best, version) => (best === null || version.version_number > best.version_number ? version : best),
    null,
  );
}

export default function AgentPlaygroundClient({ agentId }: { agentId: string }) {
  const { t, formatNumber } = useI18n();
  const { connected, workspaceId, accessToken, sessionId } = useFrontendSession();

  const loadVersions = useCallback((auth: AuthInput) => listAgentVersions(auth, agentId), [agentId]);
  const versions = useWorkspaceData<AgentVersion[]>(
    loadVersions,
    `agent-versions:${workspaceId}:${agentId}`,
  );
  const versionList = versions.data ?? [];

  // Query parameters are read after mount so server and client render the
  // same markup; `?run=` is a recovery pointer, not persisted state.
  const [urlParams, setUrlParams] = useState<{ version: string | null; run: string | null } | null>(null);

  const [selectedVersionId, setSelectedVersionId] = useState("");
  const [inputText, setInputText] = useState("");
  const [runId, setRunId] = useState<string | null>(null);
  const [runStatus, setRunStatus] = useState<string | null>(null);
  const [assistantOutput, setAssistantOutput] = useState("");
  const [finalOutput, setFinalOutput] = useState<string | null>(null);
  const [failureCode, setFailureCode] = useState<string | null>(null);
  const [activity, setActivity] = useState<ActivityEntry[]>([]);
  const [usage, setUsage] = useState<UsageTotals | null>(null);
  const [approval, setApproval] = useState<Approval | null>(null);
  const [streaming, setStreaming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [deciding, setDeciding] = useState<"approve" | "deny" | null>(null);
  const [recovered, setRecovered] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // Single liveness token for every async writer on this page. It is
  // bumped on session/workspace change, agent change, unmount, version
  // switch and re-run, so no earlier response can write back.
  const generationRef = useRef(0);
  const streamRef = useRef<AbortController | null>(null);
  const recoveryRef = useRef<string | null>(null);

  const abortStream = useCallback(() => {
    streamRef.current?.abort();
    streamRef.current = null;
  }, []);

  const resetRun = useCallback(() => {
    setRunId(null);
    setRunStatus(null);
    setAssistantOutput("");
    setFinalOutput(null);
    setFailureCode(null);
    setActivity([]);
    setUsage(null);
    setApproval(null);
    setStreaming(false);
    setBusy(false);
    setDeciding(null);
    setRecovered(false);
    setError(null);
    setNotice(null);
  }, []);

  useEffect(() => {
    generationRef.current += 1;
    abortStream();
    recoveryRef.current = null;
    resetRun();
    setSelectedVersionId("");
    setInputText("");
    setUrlParams(
      typeof window === "undefined"
        ? { version: null, run: null }
        : {
            version: new URLSearchParams(window.location.search).get("version"),
            run: new URLSearchParams(window.location.search).get("run"),
          },
    );
    return () => {
      // Unmount only stops reading the stream in this browser. It never
      // cancels the run on the server.
      generationRef.current += 1;
      abortStream();
    };
  }, [sessionId, agentId, abortStream, resetRun]);

  const syncUrl = useCallback((versionId: string, currentRunId: string | null) => {
    if (typeof window === "undefined") return;
    const url = new URL(window.location.href);
    if (versionId) url.searchParams.set("version", versionId);
    else url.searchParams.delete("version");
    if (currentRunId) url.searchParams.set("run", currentRunId);
    else url.searchParams.delete("run");
    window.history.replaceState(null, "", `${url.pathname}${url.search}`);
  }, []);

  // Default version: the URL pointer when it is valid, otherwise the
  // latest published version. Drafts are never runnable here.
  useEffect(() => {
    if (!versions.loaded || urlParams === null || urlParams.run || selectedVersionId) return;
    const fromUrl =
      urlParams.version && versionList.some((version) => version.id === urlParams.version)
        ? urlParams.version
        : null;
    const fallback = latestPublished(versionList);
    setSelectedVersionId(fromUrl ?? fallback?.id ?? "");
  }, [versions.loaded, versionList, urlParams, selectedVersionId]);

  const applyRun = useCallback((run: AgentRun) => {
    setRunId(run.id);
    setRunStatus(run.status);
    setFailureCode(run.failure_code);
    setFinalOutput(run.final_output);
    setUsage({
      input: run.total_input_tokens,
      output: run.total_output_tokens,
      total: run.total_tokens,
      cached: run.total_cached_tokens,
    });
  }, []);

  /**
   * Loads the action the run is currently waiting on. The backend orders
   * approvals by (created_at, id), so the newest PENDING entry is the
   * current one — which is what sequential approvals need.
   */
  const loadPendingApproval = useCallback(
    async (currentRunId: string, generation: number) => {
      const list = await listRunApprovals(workspaceId, currentRunId, accessToken);
      if (generationRef.current !== generation) return;
      const pending = [...list].reverse().find((item) => item.decision_status === "PENDING") ?? null;
      if (pending) setApproval(pending);
    },
    [workspaceId, accessToken],
  );

  /** Reads the persisted Run back and lets it overrule the stream. */
  const reconcile = useCallback(
    async (currentRunId: string, generation: number) => {
      const run = await getAgentRun({ workspaceId, accessToken }, currentRunId);
      if (generationRef.current !== generation) return;
      applyRun(run);
      if (run.status === "WAITING_APPROVAL") await loadPendingApproval(currentRunId, generation);
    },
    [workspaceId, accessToken, applyRun, loadPendingApproval],
  );

  const pushActivity = useCallback((entry: ActivityEntry) => {
    setActivity((current) => [...current, entry]);
  }, []);

  /**
   * Translates one runtime event into product-visible state. Only events
   * that mean something to the person running the agent are surfaced;
   * model internals are not.
   */
  const handleEvent = useCallback(
    (event: AgentEvent, versionId: string) => {
      const key = `${event.sequence}-${event.type}`;
      const payload = event.payload;
      const durationMeta = () => {
        const ms = payloadNumber(payload, "duration_ms");
        return ms === null ? [] : [t("agents.playground.durationMs", { ms: formatNumber(Math.round(ms)) })];
      };
      const countMeta = () => {
        const count = payloadNumber(payload, "result_count");
        return count === null ? [] : [t("agents.playground.resultCount", { count })];
      };
      const toolMeta = () => {
        const identity = payloadString(payload, "tool_identity");
        return identity ? [identity] : [];
      };

      switch (event.type) {
        case "run.started":
          setRunId(event.run_id);
          setRunStatus("RUNNING");
          syncUrl(versionId, event.run_id);
          break;
        case "message.delta": {
          const delta = payloadString(payload, "delta");
          if (delta !== null) setAssistantOutput((current) => current + delta);
          break;
        }
        case "context.budget":
          pushActivity({
            key,
            label: t("agents.playground.contextAdjusted"),
            meta: [],
            details: payload,
          });
          break;
        case "retrieval.started":
          pushActivity({ key, label: t("agents.playground.retrievalStarted"), meta: [] });
          break;
        case "retrieval.completed":
          pushActivity({
            key,
            label: t("agents.playground.retrievalCompleted"),
            meta: [...countMeta(), ...durationMeta()],
          });
          break;
        case "rerank.completed":
          pushActivity({
            key,
            label: t("agents.playground.rerankCompleted"),
            meta: [...countMeta(), ...durationMeta()],
          });
          break;
        case "tool.requested":
          pushActivity({ key, label: t("agents.playground.toolRequested"), meta: toolMeta() });
          break;
        case "tool.started":
          pushActivity({ key, label: t("agents.playground.toolStarted"), meta: toolMeta() });
          break;
        case "tool.completed":
          pushActivity({
            key,
            label: t("agents.playground.toolCompleted"),
            status: payloadString(payload, "status") ?? undefined,
            meta: [...toolMeta(), ...durationMeta()],
          });
          break;
        case "tool.failed": {
          const code = payloadString(payload, "error_code");
          pushActivity({
            key,
            label: t("agents.playground.toolFailed"),
            status: "FAILED",
            meta: [
              ...toolMeta(),
              ...durationMeta(),
              ...(code ? [t("agents.playground.errorCode", { code })] : []),
            ],
          });
          break;
        }
        case "approval.required":
          setRunStatus("WAITING_APPROVAL");
          pushActivity({
            key,
            label: t("agents.playground.approvalRequired"),
            status: "PENDING",
            meta: toolMeta(),
          });
          break;
        case "approval.resolved":
          pushActivity({
            key,
            label: t("agents.playground.approvalResolved"),
            status: payloadString(payload, "decision_status") ?? undefined,
            meta: [],
          });
          break;
        case "usage":
          setUsage({
            input: payloadNumber(payload, "input_tokens"),
            output: payloadNumber(payload, "output_tokens"),
            total: payloadNumber(payload, "total_tokens"),
            cached: payloadNumber(payload, "cached_tokens"),
          });
          break;
        case "run.cancel_requested":
          setRunStatus("CANCEL_REQUESTED");
          break;
        case "run.completed": {
          setRunStatus("SUCCEEDED");
          const output = payloadString(payload, "output");
          if (output !== null) setFinalOutput(output);
          break;
        }
        case "run.failed":
          setRunStatus("FAILED");
          setFailureCode(payloadString(payload, "failure_code"));
          break;
        case "run.cancelled":
          setRunStatus("CANCELLED");
          break;
        default:
          break;
      }
    },
    [t, formatNumber, pushActivity, syncUrl],
  );

  async function startRun() {
    const text = inputText.trim();
    if (!text || !selectedVersionId || streaming) return;
    abortStream();
    const controller = new AbortController();
    streamRef.current = controller;
    const generation = (generationRef.current += 1);
    const isCurrent = () => generationRef.current === generation;

    const versionId = selectedVersionId;
    resetRun();
    setRunStatus("RUNNING");
    setStreaming(true);
    setRecovered(false);
    syncUrl(versionId, null);

    const observed: { id: string | null } = { id: null };
    try {
      await streamAgentRun(
        { workspaceId, accessToken },
        {
          agentVersionId: versionId,
          inputText: text,
          signal: controller.signal,
          onEvent: (event) => {
            if (!isCurrent()) return;
            observed.id = event.run_id;
            handleEvent(event, versionId);
          },
        },
      );
    } catch (caught) {
      if (!isCurrent() || controller.signal.aborted) return;
      setStreaming(false);
      setError(toApiError(caught, t("errors.requestFailed")));
      return;
    }
    if (!isCurrent()) return;
    setStreaming(false);
    // Reaching end-of-stream is not a failure: the backend closes the
    // stream when a run pauses for approval as well as when it ends.
    const settledRunId = observed.id;
    if (settledRunId) {
      try {
        await reconcile(settledRunId, generation);
      } catch (caught) {
        if (isCurrent()) setError(toApiError(caught, t("errors.requestFailed")));
      }
    }
  }

  async function cancelRun() {
    if (!runId) return;
    const generation = generationRef.current;
    setBusy(true);
    setError(null);
    try {
      // Cancelling is a server decision; the local stream keeps reading
      // so the run's own cancellation events still arrive.
      const run = await cancelAgentRun({ workspaceId, accessToken }, runId);
      if (generationRef.current !== generation) return;
      applyRun(run);
    } catch (caught) {
      if (generationRef.current === generation) setError(toApiError(caught, t("errors.requestFailed")));
    } finally {
      if (generationRef.current === generation) setBusy(false);
    }
  }

  async function refreshStatus() {
    if (!runId) return;
    const generation = generationRef.current;
    setBusy(true);
    setError(null);
    try {
      await reconcile(runId, generation);
    } catch (caught) {
      if (generationRef.current === generation) setError(toApiError(caught, t("errors.requestFailed")));
    } finally {
      if (generationRef.current === generation) setBusy(false);
    }
  }

  async function decide(decision: "approve" | "deny") {
    if (!approval) return;
    const generation = generationRef.current;
    setDeciding(decision);
    setError(null);
    try {
      // The backend resumes the run durably and answers over HTTP; there
      // is no resume stream to reconnect to.
      const result = await decideApproval(workspaceId, approval.id, decision, accessToken);
      if (generationRef.current !== generation) return;
      setApproval(result.approval);
      setRunStatus(result.run_status);
      await reconcile(result.run_id, generation);
    } catch (caught) {
      if (generationRef.current === generation) setError(toApiError(caught, t("errors.requestFailed")));
    } finally {
      if (generationRef.current === generation) setDeciding(null);
    }
  }

  // Refresh recovery: a `?run=` pointer reads the persisted run instead
  // of starting a new one. There is no event replay.
  useEffect(() => {
    if (!connected || urlParams === null || !urlParams.run) return;
    const targetRunId = urlParams.run;
    const recoveryKey = `${sessionId}:${agentId}:${targetRunId}`;
    if (recoveryRef.current === recoveryKey) return;
    recoveryRef.current = recoveryKey;

    const generation = generationRef.current;
    setBusy(true);
    void (async () => {
      try {
        const run = await getAgentRun({ workspaceId, accessToken }, targetRunId);
        if (generationRef.current !== generation) return;
        // Ownership: the run's version must belong to this agent, so a
        // run from another agent can never be adopted by this page.
        const version = await getAgentVersion({ workspaceId, accessToken }, agentId, run.agent_version_id);
        if (generationRef.current !== generation) return;
        if (version.agent_id !== agentId) {
          setError(new ApiError("AGENT_RUN_NOT_FOUND", t("agents.playground.runNotOwned"), 404));
          return;
        }
        applyRun(run);
        setSelectedVersionId(run.agent_version_id);
        setInputText(run.input_text);
        setAssistantOutput(run.final_output ?? "");
        setRecovered(true);
        if (run.status === "WAITING_APPROVAL") await loadPendingApproval(targetRunId, generation);
      } catch (caught) {
        if (generationRef.current === generation) setError(toApiError(caught, t("errors.requestFailed")));
      } finally {
        if (generationRef.current === generation) setBusy(false);
      }
    })();
  }, [
    connected,
    urlParams,
    sessionId,
    agentId,
    workspaceId,
    accessToken,
    applyRun,
    loadPendingApproval,
    t,
  ]);

  function switchVersion(versionId: string) {
    // A different version is a different run surface: stop reading the
    // old stream and clear anything that belonged to it.
    generationRef.current += 1;
    abortStream();
    recoveryRef.current = null;
    resetRun();
    setSelectedVersionId(versionId);
    syncUrl(versionId, null);
  }

  if (!connected) {
    return (
      <div className="page">
        <header className="page-header">
          <p className="eyebrow">{t("agents.eyebrow")}</p>
          <h1>{t("agents.playground.title")}</h1>
        </header>
        <SessionRequired contextKey="session.context.agents" />
      </div>
    );
  }

  const selectedVersion = versionList.find((version) => version.id === selectedVersionId) ?? null;
  const displayedOutput = finalOutput ?? assistantOutput;
  const canRun = Boolean(selectedVersionId) && inputText.trim().length > 0 && !streaming && !busy;
  const cancellable = runId !== null && (runStatus === "RUNNING" || streaming);
  const hintKey = error ? errorHintKey(error) : null;
  // A malformed stream frame is surfaced, never swallowed.
  const errorHint = hintKey
    ? t(hintKey)
    : error?.code === "STREAM_PROTOCOL_ERROR"
      ? t("agents.playground.streamProtocolError")
      : undefined;
  const usageEntries: Array<[string, unknown]> = usage
    ? ([
        [t("agents.playground.inputTokens"), usage.input],
        [t("agents.playground.outputTokens"), usage.output],
        [t("agents.playground.totalTokens"), usage.total],
        [t("agents.playground.cachedTokens"), usage.cached],
      ].filter(([, value]) => value !== null && value !== undefined) as Array<[string, unknown]>)
    : [];

  return (
    <div className="page">
      <header className="page-header">
        <p className="eyebrow">{t("agents.eyebrow")}</p>
        <h1>{t("agents.playground.title")}</h1>
        <p className="page-lede">{t("agents.playground.lede")}</p>
      </header>

      <div className="page-toolbar">
        <Link className="button button-ghost" href={`/agents/${agentId}`}>
          {t("agents.backToAgent")}
        </Link>
        {runId && (
          <Link className="button button-ghost" href={`/runs/${encodeURIComponent(runId)}`}>
            {t("agents.playground.openRunDetail")}
          </Link>
        )}
      </div>

      {error && (
        <ErrorState
          code={error.code}
          message={error.message || t("errors.requestFailed")}
          hint={errorHint}
        />
      )}
      {notice && <p className="inline-notice">{notice}</p>}

      <Panel
        ariaLabel={t("agents.playground.title")}
        eyebrow={t("agents.playground.publishedVersion")}
        title={t("agents.playground.inputTitle")}
        actions={
          runStatus ? (
            <>
              <StatusBadge status={runStatus} />
              {recovered && <span className="state-hint">{t("agents.playground.recoveredRun")}</span>}
            </>
          ) : undefined
        }
      >
        {versions.error && (
          <ErrorState
            code={versions.error.code}
            message={versions.error.message || t("errors.loadVersions")}
            hint={errorHintKey(versions.error) ? t(errorHintKey(versions.error)!) : undefined}
            onRetry={versions.reload}
          />
        )}
        {versions.loading && !versions.error && <LoadingState />}
        {versions.loaded && !versions.error && versionList.length === 0 && (
          <EmptyState
            title={t("agents.playground.noPublishedVersion")}
            hint={t("agents.playground.publishFirst")}
          />
        )}

        {versionList.length > 0 && (
          <>
            <div className="form-grid">
              <label>
                {t("agents.version")}
                <select
                  value={selectedVersionId}
                  onChange={(event) => switchVersion(event.target.value)}
                  disabled={streaming || busy}
                >
                  {versionList.map((version) => (
                    <option key={version.id} value={version.id}>
                      v{version.version_number}
                    </option>
                  ))}
                </select>
              </label>
              <label className="query-field">
                {t("agents.playground.input")}
                <textarea
                  value={inputText}
                  onChange={(event) => setInputText(event.target.value)}
                  placeholder={t("agents.playground.inputPlaceholder")}
                  rows={4}
                  disabled={streaming}
                />
              </label>
            </div>

            {selectedVersion && (
              <p className="state-hint">
                {t("agents.playground.runningVersion", { version: selectedVersion.version_number })}{" "}
                <code className="hash-value">{selectedVersion.resolved_spec_hash}</code>
              </p>
            )}
            <p className="state-hint">{t("agents.playground.immutableHint")}</p>

            <div className="approval-actions">
              <button
                type="button"
                className="button button-primary"
                onClick={() => void startRun()}
                disabled={!canRun}
              >
                {streaming ? t("agents.playground.running") : t("agents.playground.run")}
              </button>
              {cancellable && (
                <button
                  type="button"
                  className="button button-ghost"
                  onClick={() => void cancelRun()}
                  disabled={busy}
                >
                  {t("agents.playground.cancel")}
                </button>
              )}
              {runStatus === "RUNNING" && !streaming && (
                <button
                  type="button"
                  className="button button-ghost"
                  onClick={() => void refreshStatus()}
                  disabled={busy}
                >
                  {t("agents.playground.refreshStatus")}
                </button>
              )}
            </div>

            {runStatus === "CANCEL_REQUESTED" && (
              <p className="state-hint">{t("agents.playground.cancelRequestedHint")}</p>
            )}
            {runStatus === "RUNNING" && !streaming && (
              <p className="state-hint">{t("agents.playground.stillRunning")}</p>
            )}
            {failureCode && (
              <p className="state-hint">
                {t("agents.playground.failureCode")} <code>{failureCode}</code>
              </p>
            )}
          </>
        )}
      </Panel>

      {(displayedOutput || streaming) && (
        <Panel ariaLabel={t("agents.playground.assistant")} title={t("agents.playground.assistant")}>
          <p className="playground-output">{displayedOutput}</p>
          {usageEntries.length > 0 && (
            <>
              <p className="eyebrow">{t("agents.playground.usage")}</p>
              <KeyValues entries={usageEntries} />
            </>
          )}
        </Panel>
      )}

      {approval && (
        <Panel ariaLabel={t("agents.playground.approvalRequired")} title={t("agents.playground.approvalRequired")}>
          <article className="approval-card">
            <div className="approval-header">
              <div>
                <p className="eyebrow">{t("approvals.card.toolIdentity")}</p>
                <code>{approval.tool_identity}</code>
              </div>
              <Link href={`/runs/${encodeURIComponent(approval.run_id)}`}>
                {t("approvals.card.runLink", { id: approval.run_id.slice(0, 8) + "…" })}
              </Link>
            </div>

            <div className="approval-split">
              <div className="approval-state-block">
                <span className="approval-state-label">{t("approvals.card.decision")}</span>
                <StatusBadge status={approval.decision_status} />
              </div>
              <div className="approval-state-block">
                <span className="approval-state-label">{t("approvals.card.execution")}</span>
                <StatusBadge status={approval.execution_status} />
              </div>
            </div>

            {approval.failure_code && (
              <p className="state-hint">
                {t("approvals.card.failureCodeOnly", { code: approval.failure_code })}
              </p>
            )}

            {argumentEntries(approval).length > 0 && (
              <>
                <KeyValues entries={argumentEntries(approval)} />
                <TechnicalDetails
                  summary={t("approvals.card.technicalArguments")}
                  value={approval.canonical_arguments}
                />
              </>
            )}

            {approval.decision_status === "PENDING" && (
              <div className="approval-actions">
                <button
                  type="button"
                  className="button button-ghost"
                  onClick={() => void decide("deny")}
                  disabled={deciding !== null}
                >
                  {deciding === "deny" ? t("approvals.card.denying") : t("approvals.card.deny")}
                </button>
                <button
                  type="button"
                  className="button button-primary"
                  onClick={() => void decide("approve")}
                  disabled={deciding !== null}
                >
                  {deciding === "approve" ? t("approvals.card.approving") : t("approvals.card.approve")}
                </button>
                {deciding !== null && <p className="state-hint">{t("agents.playground.resuming")}</p>}
              </div>
            )}
          </article>
        </Panel>
      )}

      {activity.length > 0 && (
        <Panel ariaLabel={t("agents.playground.activity")} title={t("agents.playground.activity")}>
          <div className="timeline">
            {activity.map((entry) => (
              <article className="timeline-entry" key={entry.key}>
                <span className="timeline-dot tone-info" aria-hidden="true" />
                <div className="timeline-body">
                  <div className="timeline-title">
                    <strong>{entry.label}</strong>
                    {entry.status && <StatusBadge status={entry.status} />}
                    {entry.meta.map((meta) => (
                      <span className="timeline-meta" key={meta}>
                        {meta}
                      </span>
                    ))}
                  </div>
                  {entry.details !== undefined && (
                    <TechnicalDetails summary={t("common.technicalDetails")} value={entry.details} />
                  )}
                </div>
              </article>
            ))}
          </div>
        </Panel>
      )}
    </div>
  );
}
