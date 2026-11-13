/** Shared product mark, paired with the visible AgentHub name. */
export default function BrandMark({ size = 32, className }: { size?: number; className?: string }) {
  return (
    <svg viewBox="0 0 32 32" width={size} height={size} className={className} fill="none" aria-hidden="true">
      <path d="M5 23 13 7h6l8 16h-7l-4-8-4 8H5Z" fill="currentColor" />
      <path d="M11 27h10" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
    </svg>
  );
}
