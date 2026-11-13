import assert from "node:assert/strict";
import test from "node:test";

import {
  COST_RESERVATION_UNAVAILABLE,
  caseDisplayIdentity,
  classifyEvidenceStep,
  describeCost,
  describeTaskSuccess,
  evidenceHasCostReservationUnavailable,
  hasUnknownOutcomeEvidence,
  normalizeCaseEvidence,
  normalizeCaseEvidenceList,
  type CaseEvidenceListItem,
} from "./case-evidence-view.ts";

const listItem: CaseEvidenceListItem = {
  id: "case-result-1",
  status: "SUCCEEDED",
  observed_agent_status: "SUCCEEDED",
  case_execution_key: "fallback-case-key",
  experiment_variant_id: "variant-1",
  repetition_index: 0,
  agent_run_id: "run-1",
  cost_amount: null,
  cost_currency: null,
  failure_code: null,
  observed_agent_failure_code: null,
  safe_failure_message: null,
};

test("evidence normalization keeps only the documented safe projection", () => {
  const evidence = normalizeCaseEvidence({
    schema_version: 1,
    case_id: "case-result-1",
    case_key: "case-a",
    category: "TOOLS",
    variant_label: "candidate",
    task_success: { status: "AVAILABLE", value: 0, reason: null },
    evidence_level: "CONTROLLED",
    execution_cleanup_status: "TEARDOWN_COMPLETE",
    termination_reason: "COMPLETED",
    episode: {
      episode_id: "episode-1",
      execution_status: "UNKNOWN_OUTCOME",
      cleanup_status: "CLEANUP_BLOCKED",
      cleanup_error: "INTERACTIVE_CLEANUP_UNKNOWN_OUTCOME",
      turns: [{ turn_index: 0, agent_run_id: "run-1", status: "NEEDS_ATTENTION" }],
      observation: "private observation",
    },
    steps: [
      {
        step_order: 2,
        step_type: "ACTION",
        agent_run_id: "run-1",
        logical_action_id: "action-1",
        arguments_hash: "a".repeat(64),
        status: "UNKNOWN_OUTCOME",
        error_code: "ACTION_OUTCOME_UNKNOWN",
        tool_name: "write_ticket",
        policy_decision: "APPROVED",
        arguments: { private: true },
      },
    ],
    failure_labels: [{ code: "UNKNOWN_OUTCOME", step_orders: [2] }],
    prompt: "private prompt",
    private_facts: ["private fact"],
  });

  assert.equal(evidence.case_key, "case-a");
  assert.equal(evidence.episode?.cleanup_status, "CLEANUP_BLOCKED");
  assert.equal(evidence.steps[0]?.logical_action_id, "action-1");
  assert.equal("observation" in (evidence.episode ?? {}), false);
  assert.equal("arguments" in (evidence.steps[0] ?? {}), false);
  assert.equal("prompt" in evidence, false);
  assert.equal("private_facts" in evidence, false);
});

test("missing evidence fields become null or empty arrays", () => {
  const evidence = normalizeCaseEvidence({ case_id: "case-result-2" });
  assert.equal(evidence.schema_version, null);
  assert.equal(evidence.case_key, null);
  assert.deepEqual(evidence.task_success, {
    status: "NOT_AVAILABLE",
    value: null,
    reason: null,
  });
  assert.equal(evidence.episode, null);
  assert.deepEqual(evidence.steps, []);
  assert.deepEqual(evidence.failure_labels, []);
});

test("list normalization strips legacy raw observation data", () => {
  const list = normalizeCaseEvidenceList({
    total: 1,
    items: [
      {
        ...listItem,
        observation: { prompt: "private prompt", private_facts: ["secret"] },
      },
    ],
  });
  assert.equal(list.items.length, 1);
  assert.equal("observation" in list.items[0], false);
  assert.equal(list.items[0]?.status, "SUCCEEDED");
});

test("list summaries retain safe identity fields and evidence takes precedence", () => {
  const list = normalizeCaseEvidenceList({
    total: 1,
    items: [
      {
        ...listItem,
        case_key: "safe-summary-key",
        category: "SAFE_CATEGORY",
        variant_label: "Safe summary variant",
        task_success: { status: "AVAILABLE", value: 0, reason: "assertion_failed" },
        observation: {
          case_key: "private-observation-key",
          category: "PRIVATE_OBSERVATION_CATEGORY",
          variant_label: "Private observation variant",
          task_success: { status: "AVAILABLE", value: 1, reason: null },
        },
      },
    ],
  });
  const item = list.items[0];
  assert.ok(item);
  assert.deepEqual(
    {
      caseKey: item.case_key,
      category: item.category,
      variantLabel: item.variant_label,
      taskSuccess: item.task_success,
    },
    {
      caseKey: "safe-summary-key",
      category: "SAFE_CATEGORY",
      variantLabel: "Safe summary variant",
      taskSuccess: {
        status: "AVAILABLE",
        value: 0,
        reason: "assertion_failed",
      },
    },
  );
  assert.equal("observation" in item, false);
  assert.deepEqual(caseDisplayIdentity(item), {
    caseKey: "safe-summary-key",
    category: "SAFE_CATEGORY",
    variantLabel: "Safe summary variant",
    taskSuccess: {
      status: "AVAILABLE",
      value: 0,
      reason: "assertion_failed",
    },
  });

  const authoritative = normalizeCaseEvidence({
    case_id: "case-result-1",
    case_key: "evidence-key",
    category: "EVIDENCE_CATEGORY",
    variant_label: "Evidence variant",
    task_success: { status: "AVAILABLE", value: 1, reason: null },
  });
  assert.deepEqual(caseDisplayIdentity(item, authoritative), {
    caseKey: "evidence-key",
    category: "EVIDENCE_CATEGORY",
    variantLabel: "Evidence variant",
    taskSuccess: {
      status: "AVAILABLE",
      value: 1,
      reason: null,
    },
  });

  const olderEvidence = normalizeCaseEvidence({
    case_id: "case-result-1",
    task_success: { status: "NOT_AVAILABLE", value: null, reason: "older_protocol" },
  });
  assert.deepEqual(caseDisplayIdentity(item, olderEvidence), {
    caseKey: "safe-summary-key",
    category: "SAFE_CATEGORY",
    variantLabel: "Safe summary variant",
    taskSuccess: {
      status: "AVAILABLE",
      value: 0,
      reason: "assertion_failed",
    },
  });
});

test("task success preserves zero and never infers it from execution status", () => {
  assert.equal(
    describeTaskSuccess({ status: "AVAILABLE", value: 0, reason: null }).kind,
    "failure",
  );
  assert.equal(
    describeTaskSuccess({ status: "AVAILABLE", value: 1, reason: null }).kind,
    "success",
  );
  assert.equal(
    describeTaskSuccess({ status: "AVAILABLE", value: null, reason: "missing" }).kind,
    "unknown",
  );
  assert.equal(describeTaskSuccess(null).kind, "not_available");
});

test("unknown cost is not zero and a recorded amount is not called a bill", () => {
  assert.deepEqual(describeCost(null, "CNY"), {
    kind: "unknown",
    value: null,
    currency: "CNY",
    text: "UNKNOWN",
  });
  assert.equal(describeCost(0, "CNY").kind, "recorded");
  assert.equal(describeCost("1.25", null).text, "1.25");
});

test("step classifications keep proposals, rejection, execution, and unknown separate", () => {
  const step = {
    step_order: 1,
    step_type: "TOOL_PROPOSAL",
    agent_run_id: null,
    logical_action_id: null,
    arguments_hash: null,
    status: "SUCCEEDED",
    error_code: null,
    tool_name: "lookup",
    policy_decision: null,
  };
  assert.equal(classifyEvidenceStep(step), "proposed");
  assert.equal(
    classifyEvidenceStep({ ...step, policy_decision: "DENIED" }),
    "rejected",
  );
  assert.equal(
    classifyEvidenceStep({ ...step, step_type: "TOOL_EXECUTE" }),
    "executed",
  );
  assert.equal(
    classifyEvidenceStep({
      ...step,
      step_type: "APPROVAL_EXECUTION",
      step_order: 0,
      status: "SUCCEEDED",
      logical_action_id: null,
    }),
    "executed",
  );
  assert.equal(
    classifyEvidenceStep({ ...step, step_type: "TOOL_EXECUTE", status: "FAILED" }),
    "failed_execution",
  );
  assert.equal(
    classifyEvidenceStep({
      ...step,
      step_type: "APPROVAL_EXECUTION",
      step_order: 0,
      status: "FAILED",
      error_code: "APPROVAL_EXECUTION_FAILED",
      logical_action_id: null,
    }),
    "failed_execution",
  );
  assert.equal(
    classifyEvidenceStep({
      ...step,
      step_type: "ACTION",
      status: null,
      error_code: "TOOL_EXECUTION_FAILED",
    }),
    "failed_execution",
  );
  assert.equal(
    classifyEvidenceStep({
      ...step,
      step_type: "ACTION",
      status: "UNKNOWN_OUTCOME",
    }),
    "unknown_outcome",
  );
  assert.equal(
    classifyEvidenceStep({
      ...step,
      step_type: "APPROVAL_EXECUTION",
      step_order: 0,
      status: "UNKNOWN_OUTCOME",
      error_code: "APPROVAL_EXECUTION_OUTCOME_UNKNOWN",
      logical_action_id: null,
    }),
    "unknown_outcome",
  );
  assert.equal(
    classifyEvidenceStep({
      ...step,
      step_type: "TOOL_EXECUTE",
      status: "DENIED",
      policy_decision: "DENIED",
    }),
    "rejected",
  );
});

test("cost-reservation and unknown-outcome notices retain their source evidence", () => {
  const evidence = normalizeCaseEvidence({
    case_id: "case-result-1",
    failure_labels: [{ code: COST_RESERVATION_UNAVAILABLE, step_orders: [] }],
    steps: [
      {
        step_order: 1,
        step_type: "ACTION",
        status: "UNKNOWN_OUTCOME",
        error_code: "ACTION_OUTCOME_UNKNOWN",
      },
    ],
  });
  assert.equal(evidenceHasCostReservationUnavailable(evidence), true);
  assert.equal(hasUnknownOutcomeEvidence(evidence, listItem), true);
  assert.deepEqual(
    caseDisplayIdentity(listItem, evidence),
    {
      caseKey: "fallback-case-key",
      category: null,
      variantLabel: null,
      taskSuccess: evidence.task_success,
    },
  );
});
