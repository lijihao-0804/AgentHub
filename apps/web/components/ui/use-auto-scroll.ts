"use client";

import { useEffect, useRef, useState } from "react";

/**
 * Follow-the-output scrolling: the list sticks to the bottom while the user
 * is at (or near) the bottom, and stops following the moment they scroll up,
 * with a manual way back. `deps` are whatever changes as content grows.
 */
export function useAutoScroll(deps: ReadonlyArray<unknown>) {
  const containerRef = useRef<HTMLDivElement>(null);
  const followingRef = useRef(true);
  const [following, setFollowing] = useState(true);

  const onScroll = () => {
    const element = containerRef.current;
    if (!element) return;
    const nearBottom = element.scrollHeight - element.scrollTop - element.clientHeight < 80;
    followingRef.current = nearBottom;
    setFollowing(nearBottom);
  };

  useEffect(() => {
    const element = containerRef.current;
    if (!element || !followingRef.current) return;
    element.scrollTop = element.scrollHeight;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  const scrollToBottom = () => {
    const element = containerRef.current;
    if (!element) return;
    followingRef.current = true;
    setFollowing(true);
    element.scrollTop = element.scrollHeight;
  };

  return { containerRef, onScroll, following, scrollToBottom };
}
