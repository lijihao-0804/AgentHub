"use client";

import { useEffect, useRef } from "react";

/**
 * ARIA menu keyboard support for the two dropdown panels: focus lands on the
 * first item when the menu opens, ArrowUp/Down/Home/End move between items,
 * and the ref stays attached to the panel container. Escape and outside
 * clicks remain each component's own concern.
 */
export function useMenuKeyboardNav(open: boolean) {
  const containerRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const container = containerRef.current;
    if (!container) return;
    const items = () => [
      ...container.querySelectorAll<HTMLElement>('[role="menuitem"]:not([disabled])'),
    ];
    const focusAt = (index: number) => {
      const list = items();
      if (list.length > 0) list[Math.max(0, Math.min(index, list.length - 1))].focus();
    };
    items()[0]?.focus();
    function onKeyDown(event: KeyboardEvent) {
      const list = items();
      const current = list.indexOf(document.activeElement as HTMLElement);
      if (event.key === "ArrowDown") {
        event.preventDefault();
        focusAt(current < 0 ? 0 : current + 1);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        focusAt(current < 0 ? list.length - 1 : current - 1);
      } else if (event.key === "Home") {
        event.preventDefault();
        focusAt(0);
      } else if (event.key === "End") {
        event.preventDefault();
        focusAt(list.length - 1);
      }
    }
    container.addEventListener("keydown", onKeyDown);
    return () => container.removeEventListener("keydown", onKeyDown);
  }, [open]);
  return containerRef;
}
