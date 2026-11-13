import assert from "node:assert/strict";
import test from "node:test";

import {
  evidenceLevelGuidance,
  kOneLimitation,
  literalConsistencyLimitation,
  orderedFailureCounts,
  orderedStatisticsVariants,
  orderedTaxonomyVariants,
  passHatKUnavailableReason,
  runExecutionSucceededWithoutTaskSuccess,
  taskOutcomeCounts,
  taskOutcomeUnavailableReason,
  usableInterval,
} from "./evaluation-results.ts";
import type { EvaluationStatisticsVariant } from "./api/evaluation.ts";

function variant(
  overrides: Partial<EvaluationStatisticsVariant> = {},
): EvaluationStatisticsVariant {
  return {
    k: 2,
    case_count: 4,
    complete_case_count: 4,
    incomplete_case_count: 0,
    trial_count: 8,
    known_trial_count: 8,
    unknown_trial_count: 0,
    success_count: 0,
    success_rate: 0,
    success_coverage_rate: 1,
    complete_case_rate: 1,
    pass_hat_k: 0,
    pass_hat_k_all_cases: 0,
    trial_variance: 0,
    consistency_at_k: 0,
    literal_consistency_at_k: 0,
    success_rate_interval: { method: "jeffreys", lower: 0, upper: 0.3 },
    reliability_note: "fixture",
    mde: null,
    variant_id: "variant-1",
    variant_label: "Candidate",
    ...overrides,
  };
}

test("task outcome counts preserve zero while separating known and unknown trials", () => {
  const summary = variant({
    trial_count: 8,
    known_trial_count: 6,
    unknown_trial_count: 2,
    success_count: 0,
  });

  assert.deepEqual(taskOutcomeCounts(summary), {
    trialCount: 8,
    knownTrialCount: 6,
    successCount: 0,
    failureCount: 6,
    unknownTrialCount: 2,
  });
});

test("inconsistent backend counts are unavailable instead of recomputed", () => {
  const summary = variant({
    trial_count: 8,
    known_trial_count: 6,
    unknown_trial_count: 1,
  });

  assert.equal(taskOutcomeCounts(summary), null);
});

test("a successful execution with a zero task score remains distinguishable", () => {
  assert.equal(runExecutionSucceededWithoutTaskSuccess("SUCCEEDED", variant()), true);
  assert.equal(
    runExecutionSucceededWithoutTaskSuccess(
      "SUCCEEDED",
      variant({ known_trial_count: 0, unknown_trial_count: 8, success_rate: null }),
    ),
    false,
  );
});

test("backend null rates retain an explicit null reason", () => {
  const summary = variant({ success_rate: null });
  assert.match(taskOutcomeUnavailableReason(summary, "zh-CN") ?? "", /null/);
});

test("an absent or malformed interval stays unavailable", () => {
  assert.equal(usableInterval(null), null);
  assert.equal(usableInterval({ method: "jeffreys", lower: 0.8, upper: 0.2 }), null);
  assert.deepEqual(usableInterval({ method: "jeffreys", lower: 0, upper: 0 }), {
    method: "jeffreys",
    lower: 0,
    upper: 0,
  });
});

test("evidence guidance preserves controlled and deterministic restrictions", () => {
  assert.match(evidenceLevelGuidance("CONTROLLED_SIMULATION", "en-US").guidance, /open production/);
  assert.match(
    evidenceLevelGuidance("DETERMINISTIC_CONFORMANCE", "zh-CN").guidance,
    /不代表模型准确率/,
  );
  assert.equal(evidenceLevelGuidance(null, "en-US").level, null);
});

test("K=1 and literal consistency both state their limits", () => {
  assert.match(kOneLimitation("en-US"), /one trial/);
  assert.match(literalConsistencyLimitation("zh-CN"), /语义正确/);
  assert.match(literalConsistencyLimitation("zh-CN"), /全部 K 次 trial 都成功/);
  assert.match(literalConsistencyLimitation("en-US"), /K trials succeeded/);
  assert.match(literalConsistencyLimitation("en-US"), /hashes are identical/);
  assert.match(
    passHatKUnavailableReason(
      variant({ complete_case_count: 0, pass_hat_k: null }),
      "en-US",
    ) ?? "",
    /K=2/,
  );
});

test("old or malformed variant maps degrade without inventing rows", () => {
  assert.deepEqual(orderedStatisticsVariants({}), []);
  assert.deepEqual(orderedStatisticsVariants({ variants: null }), []);
  assert.deepEqual(orderedStatisticsVariants({ variants: { broken: null } }), []);

  const [legacy] = orderedStatisticsVariants({
    variants: {
      "variant-id": {
        ...variant(),
        variant_label: null,
      },
    },
  });
  assert.equal(legacy.id, "variant-id");
  assert.equal(legacy.variant.variant_label, "variant-id");

  assert.deepEqual(orderedTaxonomyVariants({}), []);
  assert.deepEqual(orderedTaxonomyVariants({ variants: [] }), []);
  const [taxonomy] = orderedTaxonomyVariants({
    variants: {
      "taxonomy-id": {
        case_count: 1,
        labeled_case_count: 0,
        counts: null,
        variant_label: null,
      },
    },
  });
  assert.equal(taxonomy.id, "taxonomy-id");
  assert.equal(taxonomy.variant.variant_label, "taxonomy-id");
});

test("malformed failure counts are omitted rather than coerced", () => {
  assert.deepEqual(orderedFailureCounts(undefined), []);
  assert.deepEqual(
    orderedFailureCounts({
      zero: 0,
      valid: 2,
      negative: -1,
      fraction: 1.5,
      string: "3",
    }),
    [
      ["valid", 2],
      ["zero", 0],
    ],
  );
});
