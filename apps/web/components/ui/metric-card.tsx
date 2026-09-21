import Link from "next/link";
import type { ReactNode } from "react";

/** KPI / operational metric card. Optionally renders as a drill-down link. */
export default function MetricCard({
  label,
  value,
  unit,
  hint,
  href,
  emphasis,
}: {
  label: string;
  value: ReactNode;
  /**
   * Unit or currency code, set apart from the figure. "0.00055 USD" at the
   * metric type size wraps inside a quarter-width card; the number is the
   * thing being read, so the unit is rendered smaller beside it instead.
   */
  unit?: string | null;
  hint?: ReactNode;
  href?: string;
  emphasis?: boolean;
}) {
  const body = (
    <>
      <span className="metric-label">{label}</span>
      <strong className="metric-value">
        {value}
        {unit ? <span className="metric-unit">{unit}</span> : null}
      </strong>
      {hint && <small className="metric-hint">{hint}</small>}
    </>
  );
  if (href) {
    return (
      <Link className={`metric-card metric-card-link${emphasis ? " metric-card-emphasis" : ""}`} href={href}>
        {body}
      </Link>
    );
  }
  return <article className={`metric-card${emphasis ? " metric-card-emphasis" : ""}`}>{body}</article>;
}
