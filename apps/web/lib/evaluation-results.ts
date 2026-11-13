import type {
  EvaluationFailureTaxonomyVariant,
  EvaluationStatisticsInterval,
  EvaluationStatisticsVariant,
} from "./api/evaluation";

export type EvaluationResultLocale = "en-US" | "zh-CN";

export type OrderedStatisticsVariant = {
  id: string;
  variant: EvaluationStatisticsVariant;
};

export type OrderedTaxonomyVariant = {
  id: string;
  variant: EvaluationFailureTaxonomyVariant;
};

export type TaskOutcomeCounts = {
  trialCount: number;
  knownTrialCount: number;
  successCount: number;
  failureCount: number;
  unknownTrialCount: number;
};

export type EvidenceLevelGuidance = {
  level: string | null;
  declared: boolean;
  guidance: string;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function displayVariantLabel(value: unknown, id: string): string {
  return typeof value === "string" && value.trim().length > 0 ? value : id;
}

function statisticsVariant(
  id: string,
  value: unknown,
): EvaluationStatisticsVariant | null {
  if (!isRecord(value)) return null;
  return {
    ...(value as unknown as EvaluationStatisticsVariant),
    variant_label: displayVariantLabel(value.variant_label, id),
  };
}

function taxonomyVariant(
  id: string,
  value: unknown,
): EvaluationFailureTaxonomyVariant | null {
  if (!isRecord(value)) return null;
  return {
    ...(value as unknown as EvaluationFailureTaxonomyVariant),
    variant_label: displayVariantLabel(value.variant_label, id),
  };
}

export function orderedStatisticsVariants(
  statistics: unknown,
): OrderedStatisticsVariant[] {
  if (!isRecord(statistics)) return [];
  const variants = statistics.variants;
  if (!isRecord(variants)) return [];
  return Object.entries(variants)
    .flatMap(([id, value]) => {
      const variant = statisticsVariant(id, value);
      return variant ? [{ id, variant }] : [];
    })
    .sort(
      (left, right) =>
        left.variant.variant_label.localeCompare(right.variant.variant_label) ||
        left.id.localeCompare(right.id),
    );
}

export function orderedTaxonomyVariants(
  taxonomy: unknown,
): OrderedTaxonomyVariant[] {
  if (!isRecord(taxonomy)) return [];
  const variants = taxonomy.variants;
  if (!isRecord(variants)) return [];
  return Object.entries(variants)
    .flatMap(([id, value]) => {
      const variant = taxonomyVariant(id, value);
      return variant ? [{ id, variant }] : [];
    })
    .sort(
      (left, right) =>
        left.variant.variant_label.localeCompare(right.variant.variant_label) ||
        left.id.localeCompare(right.id),
    );
}

export function orderedFailureCounts(counts: unknown): Array<[string, number]> {
  if (!isRecord(counts)) return [];
  return Object.entries(counts)
    .filter(
      (entry): entry is [string, number] =>
        typeof entry[1] === "number" &&
        Number.isInteger(entry[1]) &&
        entry[1] >= 0,
    )
    .sort(([left], [right]) => left.localeCompare(right));
}

/**
 * Preserve the backend's denominators. A malformed count is shown as
 * unavailable rather than guessed from another field.
 */
export function taskOutcomeCounts(
  variant: EvaluationStatisticsVariant,
): TaskOutcomeCounts | null {
  const counts = [
    variant.trial_count,
    variant.known_trial_count,
    variant.unknown_trial_count,
    variant.success_count,
  ];
  if (counts.some((value) => !Number.isInteger(value) || value < 0)) return null;
  if (variant.success_count > variant.known_trial_count) return null;
  if (variant.known_trial_count + variant.unknown_trial_count !== variant.trial_count) {
    return null;
  }
  return {
    trialCount: variant.trial_count,
    knownTrialCount: variant.known_trial_count,
    successCount: variant.success_count,
    failureCount: variant.known_trial_count - variant.success_count,
    unknownTrialCount: variant.unknown_trial_count,
  };
}

export function usableInterval(
  interval: unknown,
): EvaluationStatisticsInterval | null {
  if (!isRecord(interval)) return null;
  const method = interval.method;
  const lower = interval.lower;
  const upper = interval.upper;
  if (typeof method !== "string") return null;
  if (typeof lower !== "number" || typeof upper !== "number") return null;
  if (!Number.isFinite(lower) || !Number.isFinite(upper)) return null;
  if (lower < 0 || upper > 1 || lower > upper) return null;
  return { ...interval, method, lower, upper } as EvaluationStatisticsInterval;
}

export function runExecutionSucceededWithoutTaskSuccess(
  runStatus: string | null | undefined,
  variant: EvaluationStatisticsVariant,
): boolean {
  return (
    runStatus === "SUCCEEDED" &&
    variant.known_trial_count > 0 &&
    variant.success_rate === 0
  );
}

export function evidenceLevelGuidance(
  evidenceLevel: string | null | undefined,
  locale: EvaluationResultLocale,
): EvidenceLevelGuidance {
  const zh = locale === "zh-CN";
  if (!evidenceLevel) {
    return {
      level: null,
      declared: false,
      guidance: zh
        ? "该响应未记录证据等级；页面不会据此推断更高等级。"
        : "This response did not record an evidence level. The UI does not infer a stronger one.",
    };
  }
  switch (evidenceLevel) {
    case "DETERMINISTIC_CONFORMANCE":
      return {
        level: evidenceLevel,
        declared: true,
        guidance: zh
          ? "创建者声明的确定性一致性夹具证据，不代表模型准确率或生产表现。"
          : "Creator-declared deterministic conformance fixture evidence; it is not model accuracy or production performance.",
      };
    case "CONTROLLED_SIMULATION":
      return {
        level: evidenceLevel,
        declared: true,
        guidance: zh
          ? "创建者声明的受控模拟/provider 条件证据，不能据此声明开放生产环境表现。"
          : "Creator-declared evidence from controlled simulation/provider conditions; it does not establish open production behavior.",
      };
    case "REAL_PROVIDER":
      return {
        level: evidenceLevel,
        declared: true,
        guidance: zh
          ? "创建者声明的真实 provider 证据；样本仍不覆盖所有实例或生产分布。"
          : "Creator-declared real-provider evidence; the sample does not cover every provider instance or the production distribution.",
      };
    case "PRODUCTION_SAMPLE":
      return {
        level: evidenceLevel,
        declared: true,
        guidance: zh
          ? "创建者声明的生产样本证据；适用范围仍限于已采样时段与人群。"
          : "Creator-declared production-sample evidence; conclusions remain limited to the sampled period and population.",
      };
    default:
      return {
        level: evidenceLevel,
        declared: false,
        guidance: zh
          ? "后端返回了无法识别的证据等级；页面原样显示，不自动提升。"
          : "The backend returned an unrecognized evidence level. It is shown verbatim and is not upgraded.",
      };
  }
}

export function kOneLimitation(locale: EvaluationResultLocale): string {
  return locale === "zh-CN"
    ? "K=1：pass^k 只反映单次 trial 的完成情况，不能作为稳定性或可重复性证据。"
    : "K=1: pass^k reflects one trial only and is not evidence of stability or repeatability.";
}

export function literalConsistencyLimitation(locale: EvaluationResultLocale): string {
  return locale === "zh-CN"
    ? "字面一致只表示该完整 case 的全部 K 次 trial 都成功且 K 次输出 hash 相同，不等同于语义正确。"
    : "Literal consistency means every one of the case's K trials succeeded and all K output hashes are identical; it does not establish semantic correctness.";
}

export function taskOutcomeUnavailableReason(
  variant: EvaluationStatisticsVariant,
  locale: EvaluationResultLocale,
): string | null {
  const successRate: unknown = variant.success_rate;
  if (typeof successRate === "number" && Number.isFinite(successRate)) return null;
  if (successRate === undefined) {
    return locale === "zh-CN"
      ? "旧版本响应缺少 success_rate；页面未将其按 0 处理。"
      : "This older response did not include success_rate; the UI does not treat it as zero.";
  }
  if (successRate !== null) {
    return locale === "zh-CN"
      ? "success_rate 不是有限数值；页面未据此推导结果。"
      : "success_rate is not a finite number; the UI does not infer a result from it.";
  }
  if (variant.known_trial_count === 0) {
    return locale === "zh-CN"
      ? "known_trial_count 为 0，因此 success_rate 不可用。"
      : "known_trial_count is 0, so success_rate is unavailable.";
  }
  return locale === "zh-CN"
    ? "后端返回 success_rate=null；页面未将其按 0 处理。"
    : "The backend returned success_rate=null; the UI does not treat it as zero.";
}

export function passHatKUnavailableReason(
  variant: EvaluationStatisticsVariant,
  locale: EvaluationResultLocale,
): string | null {
  const passHatK: unknown = variant.pass_hat_k;
  if (typeof passHatK === "number" && Number.isFinite(passHatK)) return null;
  if (passHatK === undefined) {
    return locale === "zh-CN"
      ? "旧版本响应缺少 pass_hat_k；页面未将其按 0 处理。"
      : "This older response did not include pass_hat_k; the UI does not treat it as zero.";
  }
  if (passHatK !== null) {
    return locale === "zh-CN"
      ? "pass_hat_k 不是有限数值；页面未据此推导可靠性。"
      : "pass_hat_k is not a finite number; the UI does not infer reliability from it.";
  }
  if (variant.complete_case_count === 0) {
    return locale === "zh-CN"
      ? `没有 K=${variant.k} 的完整 case，因此 pass_hat_k 不可用。`
      : `No complete case has K=${variant.k} trials, so pass_hat_k is unavailable.`;
  }
  return locale === "zh-CN"
    ? "后端返回 pass_hat_k=null；页面未将其按 0 处理。"
    : "The backend returned pass_hat_k=null; the UI does not treat it as zero.";
}
