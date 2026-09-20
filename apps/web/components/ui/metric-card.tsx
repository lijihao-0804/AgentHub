import Link from "next/link";
import type { ReactNode } from "react";

/** KPI / operational metric card. Optionally renders as a drill-down link. */
export default function MetricCard({
  label,
  value,
  hint,
  href,
  emphasis,
}: {
  label: string;
  value: ReactNode;
  hint?: ReactNode;
  href?: string;
  emphasis?: boolean;
}) {
  const body = (
    <>
      <span className="metric-label">{label}</span>
      <strong className="metric-value">{value}</strong>
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
