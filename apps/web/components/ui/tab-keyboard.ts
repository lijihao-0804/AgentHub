import type { KeyboardEvent } from "react";

/** Arrow navigation for a tablist; activation uses the tab's existing click handler. */
export function handleTabListKeyDown(event: KeyboardEvent<HTMLElement>) {
  if (event.altKey || event.ctrlKey || event.metaKey) return;
  const forward = event.key === "ArrowRight";
  const backward = event.key === "ArrowLeft";
  if (!forward && !backward && event.key !== "Home" && event.key !== "End") return;
  const tabs = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('button[role="tab"]:not(:disabled)'));
  const index = tabs.findIndex((tab) => tab === event.target);
  if (index < 0 || tabs.length === 0) return;
  event.preventDefault();
  const nextIndex = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : (index + (forward ? 1 : -1) + tabs.length) % tabs.length;
  tabs[nextIndex].focus();
  tabs[nextIndex].click();
}
