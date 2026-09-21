"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";

import {
  EmptyState,
  ErrorState,
  InlineError,
  LoadingState,
  Panel,
} from "@/components/ui/states";
import TechnicalDetails from "@/components/ui/technical-details";
import StatusBadge from "@/components/ui/status-badge";
import { useFrontendSession } from "@/components/providers/session-provider";
import { useWorkspaceMutation } from "@/hooks/use-workspace-data";
import { getAgentRun, type AgentRun } from "@/lib/api/agent-runtime";
import { ApiError, errorHintKey, toApiError } from "@/lib/api/client";
import {
  createDatasetVersionFromRun,
  listDatasetVersions,
  listDatasets,
  type EvaluationDataset,
  type EvaluationDatasetVersion,
} from "@/lib/api/evaluation";
import { useI18n } from "@/i18n/provider";

const CATEGORIES = [
  "RETRIEVAL",
  "KNOWLEDGE_QA",
  "TOOL",
  "NO_ANSWER",
  "APPROVAL",
  "MULTI_STEP",
  "FAILURE",
] as const;

type Category = (typeof CATEGORIES)[number];

type ExpectedDraft = {
  relevantChunkIds: string;
  answer: string;
  citations: string;
  toolIdentity: string;
  toolArguments: string;
  toolSequence: string;
  steps: string;
  terminalStatus: string;
  failureStatus: string;
  failureCode: string;
  approvalDecision: "" | "APPROVED" | "DENIED";
};

function shortId(value: string): string {
  return value.slice(0, 8);
}

function defaultExpected(): ExpectedDraft {
  return {
    relevantChunkIds: "",
    answer: "",
    citations: "",
    toolIdentity: "",
    toolArguments: "{}",
    toolSequence: "",
    steps: "",
    terminalStatus: "",
    failureStatus: "",
    failureCode: "",
    approvalDecision: "",
  };
}

function lines(value: string): string[] {
  return value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
}

function parseTags(value: string): string[] {
  return Array.from(new Set(value.split(",").map((tag) => tag.trim()).filter(Boolean)));
}

type Translator = (key: string, values?: Record<string, string | number>) => string;

function categoryLabel(t: Translator, category: Category): string {
  switch (category) {
    case "RETRIEVAL": return t("evaluation.addToEvaluation.categoryLabels.RETRIEVAL");
    case "KNOWLEDGE_QA": return t("evaluation.addToEvaluation.categoryLabels.KNOWLEDGE_QA");
    case "TOOL": return t("evaluation.addToEvaluation.categoryLabels.TOOL");
    case "NO_ANSWER": return t("evaluation.addToEvaluation.categoryLabels.NO_ANSWER");
    case "APPROVAL": return t("evaluation.addToEvaluation.categoryLabels.APPROVAL");
    case "MULTI_STEP": return t("evaluation.addToEvaluation.categoryLabels.MULTI_STEP");
    case "FAILURE": return t("evaluation.addToEvaluation.categoryLabels.FAILURE");
  }
}

function categoryDescription(t: Translator, category: Category): string {
  switch (category) {
    case "RETRIEVAL": return t("evaluation.addToEvaluation.categories.RETRIEVAL");
    case "KNOWLEDGE_QA": return t("evaluation.addToEvaluation.categories.KNOWLEDGE_QA");
    case "TOOL": return t("evaluation.addToEvaluation.categories.TOOL");
    case "NO_ANSWER": return t("evaluation.addToEvaluation.categories.NO_ANSWER");
    case "APPROVAL": return t("evaluation.addToEvaluation.categories.APPROVAL");
    case "MULTI_STEP": return t("evaluation.addToEvaluation.categories.MULTI_STEP");
    case "FAILURE": return t("evaluation.addToEvaluation.categories.FAILURE");
  }
}

function expectedFromDraft(category: Category, draft: ExpectedDraft): Record<string, unknown> | null {
  switch (category) {
    case "RETRIEVAL": {
      const relevantChunkIds = lines(draft.relevantChunkIds);
      return relevantChunkIds.length > 0 ? { relevant_chunk_ids: relevantChunkIds } : null;
    }
    case "KNOWLEDGE_QA": {
      const citations = lines(draft.citations);
      return draft.answer.trim() && citations.length > 0
        ? { answer: draft.answer.trim(), citations }
        : null;
    }
    case "TOOL": {
      if (!draft.toolIdentity.trim()) return null;
      let argumentsValue: unknown;
      try {
        argumentsValue = JSON.parse(draft.toolArguments);
      } catch {
        return null;
      }
      if (typeof argumentsValue !== "object" || argumentsValue === null || Array.isArray(argumentsValue)) return null;
      const expected: Record<string, unknown> = {
        tool_identity: draft.toolIdentity.trim(),
        arguments: argumentsValue,
      };
      const toolSequence = lines(draft.toolSequence);
      if (toolSequence.length > 0) expected.tool_sequence = toolSequence;
      return expected;
    }
    case "NO_ANSWER":
      return draft.answer.trim() ? { answer: draft.answer.trim() } : null;
    case "APPROVAL":
      return draft.approvalDecision ? { decision: draft.approvalDecision } : null;
    case "MULTI_STEP": {
      const steps = lines(draft.steps);
      if (steps.length === 0) return null;
      const expected: Record<string, unknown> = { steps };
      if (draft.terminalStatus.trim()) expected.terminal_status = draft.terminalStatus.trim();
      return expected;
    }
    case "FAILURE":
      if (!draft.failureStatus.trim()) return null;
      return {
        status: draft.failureStatus.trim(),
        failure_code: draft.failureCode.trim() || null,
      };
  }
}

export default function AddRunToDatasetPanel({
  runId,
  open,
  onClose,
}: {
  runId: string;
  open: boolean;
  onClose: () => void;
}) {
  const { t: translate, statusLabel, formatCount } = useI18n();
  const t = useCallback(
    (key: string, values?: Record<string, string | number>) =>
      translate(
        (key.startsWith("evaluation.addToEvaluation.")
          ? key.replace("evaluation.addToEvaluation", "run.addToEvaluation")
          : key) as Parameters<typeof translate>[0],
        values,
      ),
    [translate],
  );
  const { workspaceId, accessToken, connected, sessionId } = useFrontendSession();
  const [sourceRun, setSourceRun] = useState<AgentRun | null>(null);
  const [datasets, setDatasets] = useState<EvaluationDataset[]>([]);
  const [datasetId, setDatasetId] = useState("");
  const [versions, setVersions] = useState<EvaluationDatasetVersion[]>([]);
  const [baseVersionId, setBaseVersionId] = useState("");
  const [category, setCategory] = useState<Category>("FAILURE");
  const [draft, setDraft] = useState<ExpectedDraft>(defaultExpected);
  const [caseKey, setCaseKey] = useState(`run-${shortId(runId)}`);
  const [split, setSplit] = useState<"DEV" | "HOLDOUT">("DEV");
  const [tags, setTags] = useState("production-regression");
  const [loading, setLoading] = useState(false);
  const [versionsLoading, setVersionsLoading] = useState(false);
  const [loadError, setLoadError] = useState<ApiError | null>(null);
  const [versionsError, setVersionsError] = useState<ApiError | null>(null);
  const [validationError, setValidationError] = useState<string | null>(null);
  const [createdVersion, setCreatedVersion] = useState<EvaluationDatasetVersion | null>(null);
  const activeSessionRef = useRef(sessionId);
  const versionsGenerationRef = useRef(0);
  activeSessionRef.current = sessionId;

  const mutation = useWorkspaceMutation(`evaluation-run-to-dataset:${workspaceId}:${runId}`);

  const loadPanel = useCallback(async () => {
    if (!connected || !open) return;
    const requestSessionId = sessionId;
    setLoading(true);
    setLoadError(null);
    setCreatedVersion(null);
    try {
      const [nextRun, nextDatasets] = await Promise.all([
        getAgentRun({ workspaceId, accessToken }, runId),
        listDatasets({ workspaceId, accessToken }),
      ]);
      if (activeSessionRef.current !== requestSessionId) return;
      setSourceRun(nextRun);
      setDatasets(nextDatasets);
      setDatasetId((current) => (current && nextDatasets.some((dataset) => dataset.id === current) ? current : nextDatasets[0]?.id ?? ""));
    } catch (caught) {
      if (activeSessionRef.current === requestSessionId) setLoadError(toApiError(caught, ""));
    } finally {
      if (activeSessionRef.current === requestSessionId) setLoading(false);
    }
  }, [accessToken, connected, open, runId, sessionId, workspaceId]);

  useEffect(() => {
    setSourceRun(null);
    setDatasets([]);
    setDatasetId("");
    setVersions([]);
    setBaseVersionId("");
    setVersionsLoading(false);
    setLoadError(null);
    setVersionsError(null);
    setValidationError(null);
    setCreatedVersion(null);
    setCategory("FAILURE");
    setDraft(defaultExpected());
    setCaseKey(`run-${shortId(runId)}`);
    setSplit("DEV");
    setTags("production-regression");
  }, [runId, sessionId]);

  useEffect(() => {
    if (open) void loadPanel();
  }, [loadPanel, open]);

  const loadVersions = useCallback(async () => {
    const generation = versionsGenerationRef.current + 1;
    versionsGenerationRef.current = generation;
    if (!open || !connected || !datasetId) {
      setVersions([]);
      setBaseVersionId("");
      setVersionsLoading(false);
      return;
    }
    const requestSessionId = sessionId;
    setVersionsLoading(true);
    setVersionsError(null);
    setBaseVersionId("");
    try {
      const nextVersions = await listDatasetVersions({ workspaceId, accessToken }, datasetId);
      if (activeSessionRef.current !== requestSessionId || versionsGenerationRef.current !== generation) return;
      const usable = nextVersions
        .filter((version) => version.status === "DRAFT" || version.status === "PUBLISHED")
        .sort((left, right) => right.version_number - left.version_number);
      setVersions(usable);
      setBaseVersionId(usable[0]?.id ?? "");
    } catch (caught) {
      if (activeSessionRef.current === requestSessionId && versionsGenerationRef.current === generation) {
        setVersionsError(toApiError(caught, ""));
      }
    } finally {
      if (activeSessionRef.current === requestSessionId && versionsGenerationRef.current === generation) {
        setVersionsLoading(false);
      }
    }
  }, [accessToken, connected, datasetId, open, sessionId, workspaceId]);

  useEffect(() => {
    void loadVersions();
  }, [loadVersions]);

  function updateDraft<K extends keyof ExpectedDraft>(key: K, value: ExpectedDraft[K]) {
    setDraft((current) => ({ ...current, [key]: value }));
  }

  function changeCategory(next: Category) {
    setCategory(next);
    setDraft(defaultExpected());
    setValidationError(null);
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setValidationError(null);
    const expected = expectedFromDraft(category, draft);
    if (!datasetId) return setValidationError(t("evaluation.addToEvaluation.validation.dataset"));
    if (!baseVersionId) return setValidationError(t("evaluation.addToEvaluation.validation.baseVersion"));
    if (!caseKey.trim()) return setValidationError(t("evaluation.addToEvaluation.validation.caseKey"));
    if (!expected) {
      if (category === "TOOL") {
        try {
          const parsed = JSON.parse(draft.toolArguments);
          if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
            return setValidationError(t("evaluation.addToEvaluation.validation.jsonObject"));
          }
        } catch {
          return setValidationError(t("evaluation.addToEvaluation.validation.jsonObject"));
        }
      }
      return setValidationError(t("evaluation.addToEvaluation.validation.expected"));
    }
    void mutation.run(
      (auth) =>
        createDatasetVersionFromRun(auth, datasetId, {
          run_id: sourceRun?.id ?? runId,
          base_version_id: baseVersionId,
          case_key: caseKey.trim(),
          split,
          category,
          expected,
          tags: parseTags(tags),
        }),
      (created) => setCreatedVersion(created),
    );
  }

  if (!open) return null;

  const loadHint = loadError ? errorHintKey(loadError) : null;

  return (
    <Panel
      title={t("evaluation.addToEvaluation.title")}
      eyebrow={t("evaluation.eyebrow")}
      ariaLabel={t("evaluation.addToEvaluation.title")}
      actions={
        <button type="button" className="button button-ghost" onClick={onClose} disabled={mutation.pending}>
          {t("evaluation.addToEvaluation.close")}
        </button>
      }
    >
      <p className="page-lede">{t("evaluation.addToEvaluation.description")}</p>

      {loadError && (
        <ErrorState
          code={loadError.code}
          message={loadError.message || t("evaluation.addToEvaluation.errors.loadRun")}
          hint={loadHint ? t(loadHint) : undefined}
          onRetry={() => void loadPanel()}
        />
      )}
      {loading && !loadError && <LoadingState />}

      {sourceRun && !loadError && (
        <>
          <section className="eval-form" aria-labelledby="observed-run-title">
            <div className="eval-form-title" id="observed-run-title">{t("evaluation.addToEvaluation.observed")}</div>
            <div className="run-facts">
              <span>{t("evaluation.addToEvaluation.observedStatus")}<strong><StatusBadge status={sourceRun.status} /></strong></span>
              <span>{t("evaluation.addToEvaluation.observedFailure")}<strong>{sourceRun.failure_code ?? "—"}</strong></span>
              <span>{t("evaluation.addToEvaluation.observedResolvedHash")}<strong>{sourceRun.resolved_spec_hash ? shortId(sourceRun.resolved_spec_hash) : "—"}</strong></span>
            </div>
            <TechnicalDetails summary={t("evaluation.addToEvaluation.observedInput")} value={sourceRun.input_text} />
          </section>

          {datasets.length === 0 ? (
            <div>
              <EmptyState title={t("evaluation.addToEvaluation.createDatasetFirst")} />
              <Link className="button button-primary" href="/evaluations/datasets">
                {t("evaluation.addToEvaluation.createDatasetLink")}
              </Link>
            </div>
          ) : createdVersion ? (
            <div className="inline-notice" role="status">
              <p><strong>{t("evaluation.addToEvaluation.success")}</strong></p>
              <p className="state-hint">
                {t("evaluation.addToEvaluation.successDetail", { version: createdVersion.version_number })}
                {createdVersion.item_count !== null && (
                  <> · {t("evaluation.addToEvaluation.itemCount", { count: createdVersion.item_count })}</>
                )}
              </p>
              <div className="form-actions">
                <Link
                  className="button button-primary"
                  href={`/evaluations/datasets/${encodeURIComponent(createdVersion.dataset_id)}/versions/${encodeURIComponent(createdVersion.id)}`}
                >
                  {t("evaluation.addToEvaluation.openVersion")}
                </Link>
                <button type="button" className="button button-ghost" onClick={onClose}>
                  {t("evaluation.addToEvaluation.close")}
                </button>
              </div>
            </div>
          ) : (
            <form className="eval-form" onSubmit={submit} noValidate>
              <div className="eval-form-title">{t("evaluation.addToEvaluation.expected")}</div>
              <div className="form-grid">
                <label>
                  {t("evaluation.addToEvaluation.dataset")}
                  <select value={datasetId} onChange={(event) => setDatasetId(event.target.value)}>
                    <option value="">{t("evaluation.addToEvaluation.datasetPlaceholder")}</option>
                    {datasets.map((dataset) => <option value={dataset.id} key={dataset.id}>{dataset.name}</option>)}
                  </select>
                </label>
                <label>
                  {t("evaluation.addToEvaluation.baseVersion")}
                  {versionsLoading ? (
                    <span className="state-hint">{t("evaluation.addToEvaluation.baseVersionLoading")}</span>
                  ) : versionsError ? (
                    <ErrorState
                      code={versionsError.code}
                      message={versionsError.message || t("evaluation.addToEvaluation.errors.loadVersions")}
                      onRetry={() => void loadVersions()}
                    />
                  ) : (
                    <select value={baseVersionId} onChange={(event) => setBaseVersionId(event.target.value)} disabled={versions.length === 0}>
                      <option value="">{t("evaluation.addToEvaluation.baseVersionPlaceholder")}</option>
                      {versions.map((version) => (
                        <option value={version.id} key={version.id}>
                          {t("evaluation.addToEvaluation.baseVersionOption", {
                            version: version.version_number,
                            status: statusLabel(version.status),
                            // Unknown stays "?" rather than a formatted 0: the
                            // count is missing, not zero.
                            items: version.item_count === null || version.item_count === undefined
                              ? "?"
                              : formatCount(version.item_count),
                          })}
                        </option>
                      ))}
                    </select>
                  )}
                  <span className="state-hint">{versions.length === 0 && !versionsLoading ? t("evaluation.addToEvaluation.baseVersionEmpty") : t("evaluation.addToEvaluation.baseVersionHint")}</span>
                </label>
                <label>
                  {t("evaluation.addToEvaluation.caseKey")}
                  <input value={caseKey} onChange={(event) => setCaseKey(event.target.value)} spellCheck={false} />
                </label>
                <label>
                  {t("evaluation.addToEvaluation.split")}
                  <select value={split} onChange={(event) => setSplit(event.target.value as "DEV" | "HOLDOUT")}>
                    <option value="DEV">{t("evaluation.addToEvaluation.dev")}</option>
                    <option value="HOLDOUT">{t("evaluation.addToEvaluation.holdout")}</option>
                  </select>
                  {split === "HOLDOUT" && <span className="state-hint">{t("evaluation.addToEvaluation.holdoutWarning")}</span>}
                </label>
                <label>
                  {t("evaluation.addToEvaluation.category")}
                  <select value={category} onChange={(event) => changeCategory(event.target.value as Category)}>
                    {CATEGORIES.map((value) => <option value={value} key={value}>{categoryLabel(t, value)}</option>)}
                  </select>
                  <span className="state-hint">{t("evaluation.addToEvaluation.categoryDescription")}: {categoryDescription(t, category)}</span>
                </label>
                <label>
                  {t("evaluation.addToEvaluation.tags")}
                  <input value={tags} onChange={(event) => setTags(event.target.value)} spellCheck={false} />
                </label>
              </div>

              <div className="form-grid">
                {category === "RETRIEVAL" && (
                  <label>
                    {t("evaluation.addToEvaluation.relevantChunkIds")}
                    <textarea className="mono-area" rows={4} value={draft.relevantChunkIds} onChange={(event) => updateDraft("relevantChunkIds", event.target.value)} />
                  </label>
                )}
                {category === "KNOWLEDGE_QA" && (
                  <>
                    <label>
                      {t("evaluation.addToEvaluation.answer")}
                      <textarea rows={4} value={draft.answer} onChange={(event) => updateDraft("answer", event.target.value)} />
                    </label>
                    <label>
                      {t("evaluation.addToEvaluation.citations")}
                      <textarea className="mono-area" rows={4} value={draft.citations} onChange={(event) => updateDraft("citations", event.target.value)} />
                    </label>
                  </>
                )}
                {category === "TOOL" && (
                  <>
                    <label>
                      {t("evaluation.addToEvaluation.toolIdentity")}
                      <input value={draft.toolIdentity} onChange={(event) => updateDraft("toolIdentity", event.target.value)} spellCheck={false} />
                    </label>
                    <label>
                      {t("evaluation.addToEvaluation.toolArguments")}
                      <textarea className="mono-area" rows={4} placeholder={t("evaluation.addToEvaluation.toolArgumentsPlaceholder")} value={draft.toolArguments} onChange={(event) => updateDraft("toolArguments", event.target.value)} />
                    </label>
                    <label>
                      {t("evaluation.addToEvaluation.toolSequence")}
                      <textarea className="mono-area" rows={4} value={draft.toolSequence} onChange={(event) => updateDraft("toolSequence", event.target.value)} />
                    </label>
                  </>
                )}
                {category === "NO_ANSWER" && (
                  <label>
                    {t("evaluation.addToEvaluation.answer")}
                    <textarea rows={4} value={draft.answer} onChange={(event) => updateDraft("answer", event.target.value)} />
                  </label>
                )}
                {category === "APPROVAL" && (
                  <label>
                    {t("evaluation.addToEvaluation.approvalDecision")}
                    <select value={draft.approvalDecision} onChange={(event) => updateDraft("approvalDecision", event.target.value as ExpectedDraft["approvalDecision"])}>
                      <option value="">—</option>
                      <option value="APPROVED">APPROVED</option>
                      <option value="DENIED">DENIED</option>
                    </select>
                  </label>
                )}
                {category === "MULTI_STEP" && (
                  <>
                    <label>
                      {t("evaluation.addToEvaluation.steps")}
                      <textarea className="mono-area" rows={4} value={draft.steps} onChange={(event) => updateDraft("steps", event.target.value)} />
                    </label>
                    <label>
                      {t("evaluation.addToEvaluation.terminalStatus")}
                      <input value={draft.terminalStatus} onChange={(event) => updateDraft("terminalStatus", event.target.value)} spellCheck={false} />
                    </label>
                  </>
                )}
                {category === "FAILURE" && (
                  <>
                    <label>
                      {t("evaluation.addToEvaluation.failureStatus")}
                      <input value={draft.failureStatus} onChange={(event) => updateDraft("failureStatus", event.target.value)} spellCheck={false} />
                    </label>
                    <label>
                      {t("evaluation.addToEvaluation.failureCode")}
                      <input value={draft.failureCode} onChange={(event) => updateDraft("failureCode", event.target.value)} spellCheck={false} />
                    </label>
                  </>
                )}
              </div>

              {validationError && <p className="inline-error" role="alert">{validationError}</p>}
              <InlineError error={mutation.error} fallback={t("errors.requestFailed")} />
              <div className="form-actions">
                <button type="submit" className="button button-primary" disabled={mutation.pending || versions.length === 0}>
                  {mutation.pending ? t("evaluation.addToEvaluation.submitting") : t("evaluation.addToEvaluation.submit")}
                </button>
                <button type="button" className="button button-ghost" onClick={onClose} disabled={mutation.pending}>
                  {t("evaluation.addToEvaluation.close")}
                </button>
              </div>
            </form>
          )}
        </>
      )}
    </Panel>
  );
}
