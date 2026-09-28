import Link from "next/link";
import type { ReactNode } from "react";

import { useI18n } from "@/i18n/provider";

/**
 * Compare average bucket volume across the two halves of a series. Averaging
 * keeps odd-length series comparable; a zero baseline has no meaningful
 * percentage delta, including the 0-to-0 case.
 */
export function windowDelta(series: number[]): number | null {
  if (series.length < 4) return null;
  const half = Math.floor(series.length / 2);
  const olderValues = series.slice(0, half);
  const recentValues = series.slice(half);
  const older = olderValues.reduce((sum, value) => sum + value, 0) / olderValues.length;
  const recent = recentValues.reduce((sum, value) => sum + value, 0) / recentValues.length;
  if (older === 0) return null;
  return (recent - older) / older;
}

/** A tiny inline bar sparkline; pure SVG, no per-point labels. */
function Sparkline({ series }: { series: number[] }) {
  const width = 72;
  const height = 18;
  const max = Math.max(...series, 1);
  const barWidth = width / series.length;
  return (
    <svg className="metric-sparkline" width={width} height={height} aria-hidden="true">
      {series.map((value, index) => {
        const barHeight = Math.max((value / max) * height, 1);
        return (
          <rect
            key={index}
            x={index * barWidth}
            y={height - barHeight}
            width={Math.max(barWidth - 1, 1)}
            height={barHeight}
            rx="1"
          />
        );
      })}
    </svg>
  );
}

/** KPI / operational metric card. Optionally renders as a drill-down link. */
export default function MetricCard({
  label,
  value,
  unit,
  hint,
  href,
  emphasis,
  delta,
  deltaLabel,
  deltaInverse,
  sparkline,
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
  /** Half-window change as a fraction (0.12 = +12%); rendered with an arrow. */
  delta?: number | null;
  /** Accessible description of what the delta compares. */
  deltaLabel?: string;
  /** Increasing the value is unfavorable, as with failed runs. */
  deltaInverse?: boolean;
  /** Recent per-bucket values, drawn as a small sparkline. */
  sparkline?: number[];
}) {
  const { formatPercent } = useI18n();
  const body = (
    <>
      <span className="metric-label">{label}</span>
      <strong className="metric-value">
        {value}
        {unit ? <span className="metric-unit">{unit}</span> : null}
      </strong>
      {(delta !== undefined && delta !== null) || sparkline ? (
        <span className="metric-trendline">
          {delta !== undefined && delta !== null ? (
            <span
              className={`metric-delta ${delta === 0 ? "metric-delta-neutral" : (delta > 0) !== Boolean(deltaInverse) ? "metric-delta-good" : "metric-delta-bad"}`}
              title={deltaLabel}
            >
              {delta > 0 ? "▲" : delta < 0 ? "▼" : "→"} {formatPercent(Math.abs(delta))}
            </span>
          ) : null}
          {sparkline && sparkline.length > 0 ? <Sparkline series={sparkline} /> : null}
        </span>
      ) : null}
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
