/**
 * The icon set.
 *
 * Hand-drawn rather than pulled from a package: the whole app is written
 * without a component library, and an icon dependency would be the largest
 * thing in the bundle for the twenty glyphs actually used. Every icon is one
 * `<path>` group on a 24x24 grid, stroked in `currentColor`, so an icon
 * inherits the colour of whatever it sits in — a muted nav link, an active
 * one, an accent tile — without a single colour being named here.
 *
 * Icons are decorative by default (`aria-hidden`): they sit beside a text
 * label everywhere they are used, and announcing them would just make a
 * screen reader read every nav entry twice. Pass `title` for the rare icon
 * that carries meaning on its own.
 */
export type IconName =
  | "dashboard"
  | "runs"
  | "compare"
  | "approvals"
  | "research"
  | "incidents"
  | "analytics"
  | "support"
  | "agents"
  | "knowledge"
  | "search"
  | "tools"
  | "evaluations"
  | "settings"
  | "models"
  | "workspace"
  | "arrowRight";

const PATHS: Record<IconName, React.ReactNode> = {
  // A gauge: the dashboard is a set of readings, not a list.
  dashboard: (
    <>
      <path d="M4 17.5a8 8 0 1 1 16 0" />
      <path d="M12 17.5l3.6-4.8" />
    </>
  ),
  // An activity trace — a run is something that happened over time.
  runs: <path d="M3 12h3.8l2.4-6.2 4 12.4 2.4-6.2H21" />,
  compare: (
    <>
      <rect x="3.5" y="4.5" width="7" height="15" rx="1.6" />
      <rect x="13.5" y="4.5" width="7" height="15" rx="1.6" />
    </>
  ),
  approvals: (
    <>
      <path d="M12 3.2l7.2 2.8v5.4c0 4.3-3 7.7-7.2 8.6-4.2-.9-7.2-4.3-7.2-8.6V6z" />
      <path d="M9 11.9l2.2 2.2 4-4.2" />
    </>
  ),
  // A flask: research is the one application that only reads and records.
  research: (
    <>
      <path d="M9.5 3v5.6L4.9 17a2 2 0 0 0 1.7 3h10.8a2 2 0 0 0 1.7-3L14.5 8.6V3" />
      <path d="M8 3h8" />
      <path d="M7.4 14.2h9.2" />
    </>
  ),
  incidents: (
    <>
      <path d="M12 4.4 2.9 19.6h18.2z" />
      <path d="M12 10v4.1" />
      <path d="M12 17.2h.01" />
    </>
  ),
  analytics: (
    <>
      <path d="M4.5 4v15.5H20" />
      <path d="M8.6 16.6v-4.2M13 16.6v-8.1M17.4 16.6v-5.6" />
    </>
  ),
  // A headset: support is the application with a person on the other end.
  support: (
    <>
      <path d="M5 14.2v-2.4a7 7 0 0 1 14 0v2.4" />
      <rect x="2.8" y="13.4" width="4.4" height="6.2" rx="1.7" />
      <rect x="16.8" y="13.4" width="4.4" height="6.2" rx="1.7" />
    </>
  ),
  agents: (
    <>
      <rect x="3.8" y="8" width="16.4" height="11.4" rx="3.2" />
      <path d="M12 8V5.2" />
      <path d="M8.8 12.8h.01M15.2 12.8h.01" />
      <path d="M1.8 13.4v2.6M22.2 13.4v2.6" />
    </>
  ),
  knowledge: (
    <>
      <path d="M12 6.6C10.4 5 8.4 4.4 4.8 4.4v13.2c3.6 0 5.6.6 7.2 2.2 1.6-1.6 3.6-2.2 7.2-2.2V4.4c-3.6 0-5.6.6-7.2 2.2z" />
      <path d="M12 6.6v13.2" />
    </>
  ),
  search: (
    <>
      <circle cx="10.8" cy="10.8" r="6.2" />
      <path d="M15.4 15.4 20.4 20.4" />
    </>
  ),
  tools: (
    <path d="M15.6 3.3a5 5 0 0 0-5.9 6.4l-6.1 6.1a2.2 2.2 0 1 0 3.1 3.1l6.1-6.1a5 5 0 0 0 6.4-5.9l-2.9 2.9-2.6-.7-.7-2.6z" />
  ),
  evaluations: (
    <>
      <circle cx="12" cy="12" r="7.6" />
      <circle cx="12" cy="12" r="3.4" />
      <circle cx="12" cy="12" r="0.7" fill="currentColor" stroke="none" />
    </>
  ),
  settings: (
    <>
      <path d="M4 6.4h16M4 12h16M4 17.6h16" />
      <circle cx="9.2" cy="6.4" r="2" />
      <circle cx="15" cy="12" r="2" />
      <circle cx="8" cy="17.6" r="2" />
    </>
  ),
  models: (
    <>
      <rect x="7.2" y="7.2" width="9.6" height="9.6" rx="2.2" />
      <path d="M10.2 3.8v3.4M13.8 3.8v3.4M10.2 16.8v3.4M13.8 16.8v3.4" />
      <path d="M3.8 10.2h3.4M3.8 13.8h3.4M16.8 10.2h3.4M16.8 13.8h3.4" />
    </>
  ),
  workspace: (
    <>
      <path d="M10.6 13.4a4.1 4.1 0 0 0 5.9 0l2.4-2.4a4.1 4.1 0 0 0-5.8-5.8l-1.3 1.3" />
      <path d="M13.4 10.6a4.1 4.1 0 0 0-5.9 0l-2.4 2.4a4.1 4.1 0 0 0 5.8 5.8l1.3-1.3" />
    </>
  ),
  arrowRight: (
    <>
      <path d="M4.5 12h14" />
      <path d="M13 6.4 18.6 12 13 17.6" />
    </>
  ),
};

export default function Icon({
  name,
  size = 18,
  className,
  title,
}: {
  name: IconName;
  /** Rendered square, in px. The stroke is scaled so 14px and 26px read alike. */
  size?: number;
  className?: string;
  /** Supply only when the icon is the sole label for its control. */
  title?: string;
}) {
  return (
    <svg
      className={className ? `icon ${className}` : "icon"}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      // Thin strokes disappear at 14px and look heavy at 28px; scale with size.
      strokeWidth={size >= 24 ? 1.5 : 1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      role={title ? "img" : undefined}
      aria-hidden={title ? undefined : true}
      aria-label={title}
      focusable="false"
    >
      {PATHS[name]}
    </svg>
  );
}
