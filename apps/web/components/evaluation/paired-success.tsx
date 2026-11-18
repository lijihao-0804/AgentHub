"use client";

import { useEffect, useState } from "react";
import { getPairedSuccess, type PairedSuccessReport } from "@/lib/api/evaluation";
import { useI18n } from "@/i18n/provider";

export default function PairedSuccess({ input, runId, comparisonId }: {
  input: { workspaceId: string; accessToken: string };
  runId: string;
  comparisonId: string;
}) {
  const { locale } = useI18n();
  const zh = locale === "zh-CN";
  const [open, setOpen] = useState(false);
  const [report, setReport] = useState<PairedSuccessReport | null>(null);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const { workspaceId, accessToken } = input;

  useEffect(() => {
    if (!open) return;
    let active = true;
    setFailed(false);
    getPairedSuccess({ workspaceId, accessToken }, runId, comparisonId)
      .then(data => { if (active) setReport(data); })
      .catch(() => { if (active) setFailed(true); });
    return () => { active = false; };
  }, [open, workspaceId, accessToken, runId, comparisonId, attempt]);

  const points = (value: number | null) => value === null ? "—" :
    new Intl.NumberFormat(locale, { maximumFractionDigits: 2, signDisplay: "exceptZero" }).format(value * 100);

  return <details onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>{zh ? "逐题配对成功率" : "Paired success by case"}</summary>
    {failed ? <p role="alert">{zh ? "配对结果加载失败。" : "Could not load paired outcomes."}{" "}
      <button type="button" onClick={() => setAttempt(n => n + 1)}>{zh ? "重试" : "Retry"}</button></p>
      : !report ? <p role="status">{zh ? "加载中…" : "Loading…"}</p>
      : <>
        <p>{zh ? "候选减基线（百分点）" : "Candidate minus baseline (percentage points)"} {points(report.delta)}
          {report.interval && <> · 95% CI [{points(report.interval.lower)}, {points(report.interval.upper)}]</>}</p>
        <p>{zh ? "同题同次执行配对，每题等权。" : "Repetitions are matched within each case, with equal weight per case."}{" "}
          {zh ? "可比较题数" : "Comparable cases"} {report.pair_count}/{report.cases.length} ·{" "}
          {zh ? "缺失配对次数" : "Unmatched repetitions"} {report.missing_repetitions}/{report.planned_repetitions}</p>
        {report.interval && (report.pair_count < 10 || report.interval.lower === report.interval.upper) &&
          <p className="state-hint">{zh ? "配对样例少于 10 个或区间上下界重合时，发布门禁的区间规则无法判定。" :
            "Interval-based gate rules are inconclusive with fewer than 10 matched cases or coincident interval bounds."}</p>}
        <div className="data-table"><table><thead><tr>
          {(zh ? ["样例", "变化（百分点）", "配对次数", "基线缺失", "候选缺失"] :
            ["Case", "Change (pp)", "Matched trials", "Baseline missing", "Candidate missing"]).map(h => <th key={h} scope="col">{h}</th>)}
        </tr></thead><tbody>
          {[...report.cases].sort((a, b) => (a.delta ?? Infinity) - (b.delta ?? Infinity)).map(row => <tr key={row.case_key}>
            <th scope="row">{row.case_key}</th><td>{points(row.delta)}</td><td>{row.matched_repetitions}/{row.planned_repetitions}</td>
            <td>{row.baseline_missing}</td><td>{row.candidate_missing}</td>
          </tr>)}
        </tbody></table></div>
      </>}
  </details>;
}
