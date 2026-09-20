/**
 * Structural diff between two immutable AgentVersion resolved specs.
 *
 * This is deliberately a small pure helper rather than a JSON diff
 * dependency: the only thing the compare page needs is a flat list of
 * `path / before / after / change_type` rows.
 *
 * It reports structure, never quality. There is no notion of a better or
 * worse version here — judging output quality is what Evaluation is for.
 */

export type ChangeType = "ADDED" | "REMOVED" | "CHANGED" | "UNCHANGED";

export type DiffEntry = {
  /** Dotted path into the resolved spec, e.g. `runtime.max_steps`. */
  path: string;
  before: unknown;
  after: unknown;
  changeType: ChangeType;
};

/** Stable group identity; the UI maps this to a translated label. */
export type DiffGroupKey = "model" | "prompt" | "knowledge" | "tools" | "runtime" | "other";

export type DiffGroup = {
  key: DiffGroupKey;
  entries: DiffEntry[];
};

export type DiffSummary = {
  added: number;
  removed: number;
  changed: number;
  unchanged: number;
  /** Added + removed + changed, i.e. everything that is not identical. */
  changedFields: number;
};

export type VersionDiff = {
  groups: DiffGroup[];
  summary: DiffSummary;
};

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Key-sorted JSON so that two structurally equal values always produce the
 * same string. Arrays keep their own order: order is part of their meaning
 * for a tool list or a fallback chain.
 */
export function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) {
    return `[${value.map((item) => canonicalJson(item)).join(",")}]`;
  }
  if (isPlainObject(value)) {
    const keys = Object.keys(value).sort();
    return `{${keys.map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value) ?? "null";
}

/**
 * Flattens nested objects into dotted leaf paths. Arrays are treated as
 * leaves and compared as whole canonical values: an element-level edit
 * distance would add a lot of machinery without making the change any
 * easier to read.
 */
export function flattenSpec(value: unknown, prefix = ""): Map<string, unknown> {
  const flat = new Map<string, unknown>();
  if (isPlainObject(value)) {
    // An empty object contributes no leaves: emitting the container
    // itself would show up as an unrelated ADDED row next to the
    // REMOVED rows for the children it lost.
    for (const key of Object.keys(value)) {
      const path = prefix === "" ? key : `${prefix}.${key}`;
      for (const [childPath, childValue] of flattenSpec(value[key], path)) {
        flat.set(childPath, childValue);
      }
    }
    return flat;
  }
  if (prefix !== "") flat.set(prefix, value);
  return flat;
}

const GROUP_BY_SEGMENT: Record<string, DiffGroupKey> = {
  model: "model",
  prompt: "prompt",
  retrieval: "knowledge",
  tools: "tools",
  runtime: "runtime",
};

/**
 * Groups by the first path segment only, so any field a future spec
 * schema adds under a known root still lands in the right section instead
 * of disappearing behind a hard-coded field list.
 */
export function groupKeyForPath(path: string): DiffGroupKey {
  const segment = path.split(".")[0] ?? "";
  return GROUP_BY_SEGMENT[segment] ?? "other";
}

const GROUP_ORDER: DiffGroupKey[] = ["prompt", "model", "knowledge", "tools", "runtime", "other"];

function classify(hasBefore: boolean, hasAfter: boolean, before: unknown, after: unknown): ChangeType {
  if (!hasBefore) return "ADDED";
  if (!hasAfter) return "REMOVED";
  return canonicalJson(before) === canonicalJson(after) ? "UNCHANGED" : "CHANGED";
}

/**
 * Diffs two resolved specs. Every leaf path present on either side
 * produces exactly one entry, including unchanged ones — the page decides
 * whether to show them.
 */
export function diffResolvedSpecs(
  left: Record<string, unknown>,
  right: Record<string, unknown>,
): VersionDiff {
  const leftFlat = flattenSpec(left);
  const rightFlat = flattenSpec(right);
  const paths = Array.from(new Set([...leftFlat.keys(), ...rightFlat.keys()])).sort();

  const summary: DiffSummary = { added: 0, removed: 0, changed: 0, unchanged: 0, changedFields: 0 };
  const byGroup = new Map<DiffGroupKey, DiffEntry[]>();

  for (const path of paths) {
    const hasBefore = leftFlat.has(path);
    const hasAfter = rightFlat.has(path);
    const before = hasBefore ? leftFlat.get(path) : undefined;
    const after = hasAfter ? rightFlat.get(path) : undefined;
    const changeType = classify(hasBefore, hasAfter, before, after);

    if (changeType === "ADDED") summary.added += 1;
    else if (changeType === "REMOVED") summary.removed += 1;
    else if (changeType === "CHANGED") summary.changed += 1;
    else summary.unchanged += 1;

    const key = groupKeyForPath(path);
    const entries = byGroup.get(key) ?? [];
    entries.push({ path, before, after, changeType });
    byGroup.set(key, entries);
  }
  summary.changedFields = summary.added + summary.removed + summary.changed;

  const groups: DiffGroup[] = [];
  for (const key of GROUP_ORDER) {
    const entries = byGroup.get(key);
    if (entries && entries.length > 0) groups.push({ key, entries });
  }
  return { groups, summary };
}

/**
 * True when a value needs a collapsible block rather than a single inline
 * line: objects, arrays and long strings.
 */
export function isComplexValue(value: unknown): boolean {
  if (Array.isArray(value) || isPlainObject(value)) return true;
  return typeof value === "string" && (value.length > 80 || value.includes("\n"));
}

/** Short single-line rendering for scalar leaves. */
export function formatScalar(value: unknown): string {
  if (value === undefined) return "—";
  if (value === null) return "null";
  if (typeof value === "string") return value;
  return canonicalJson(value);
}
