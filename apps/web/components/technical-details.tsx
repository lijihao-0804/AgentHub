function renderValue(value: unknown): string {
  if (value === null || value === undefined) return "null";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  try {
    return JSON.stringify(value);
  } catch {
    return "[unserializable]";
  }
}

/** Structured key/value summary used before revealing raw technical data. */
export function KeyValues({ entries }: { entries: Array<[string, unknown]> }) {
  if (entries.length === 0) return null;
  return (
    <dl className="key-values">
      {entries.map(([key, value]) => (
        <div className="key-value-row" key={key}>
          <dt>{key}</dt>
          <dd>
            <code>{renderValue(value)}</code>
          </dd>
        </div>
      ))}
    </dl>
  );
}

/**
 * Progressive disclosure for raw technical payloads. Collapsed by
 * default so safe summaries stay the primary presentation.
 */
export default function TechnicalDetails({
  summary = "Technical details",
  value,
}: {
  summary?: string;
  value: unknown;
}) {
  if (value === null || value === undefined) return null;
  let text: string;
  try {
    text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  } catch {
    text = "[unserializable]";
  }
  return (
    <details className="technical-details">
      <summary>{summary}</summary>
      <pre>{text}</pre>
    </details>
  );
}
