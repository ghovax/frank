"use client";

import { useCallback, useEffect, useRef, useState } from "react";

// Soft edge fades for scrollable regions, applied as a CSS mask instead of a hard divider.
const TOP = 14;
const BOTTOM = 28;
const topGradient = `linear-gradient(to bottom, transparent 0, #000 ${TOP}px, #000 100%)`;
const bottomGradient = `linear-gradient(to bottom, #000 0, #000 calc(100% - ${BOTTOM}px), transparent 100%)`;
const topBottomGradient = `linear-gradient(to bottom, transparent 0, #000 ${TOP}px, #000 calc(100% - ${BOTTOM}px), transparent 100%)`;

export const scrollFade = {
  maskImage: topGradient,
  WebkitMaskImage: topGradient,
} as const;

export const scrollFadeBottom = {
  maskImage: bottomGradient,
  WebkitMaskImage: bottomGradient,
} as const;

export const scrollFadeTopBottom = {
  maskImage: topBottomGradient,
  WebkitMaskImage: topBottomGradient,
} as const;

// Scroll-driven fades: each edge fades only while content is hidden beyond it.
export function useScrollEdgeFade() {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [hiddenAbove, setHiddenAbove] = useState(false);
  const [hiddenBelow, setHiddenBelow] = useState(false);
  const measure = useCallback(() => {
    const container = containerRef.current;
    if (!container) return;
    // A tolerance either side, because the mask this drives can move the metrics by a fraction of a pixel,
    // and an edge decided on that fraction flips back and forth instead of settling.
    setHiddenAbove(container.scrollTop > 2);
    setHiddenBelow(container.scrollHeight - (container.scrollTop + container.clientHeight) > 8);
  }, []);
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    // Measured from the element, never from a render: measuring on every render fed the state that
    // decides the mask, and the mask fed the observer that measured again — a cycle with no exit.
    let scheduled = 0;
    const settle = () => {
      window.cancelAnimationFrame(scheduled);
      scheduled = window.requestAnimationFrame(measure);
    };
    settle();
    const observer = new ResizeObserver(settle);
    observer.observe(container);
    const watcher = new MutationObserver(settle);
    watcher.observe(container, { childList: true, subtree: true, characterData: true });
    return () => {
      window.cancelAnimationFrame(scheduled);
      observer.disconnect();
      watcher.disconnect();
    };
  }, [measure]);
  const fade = hiddenAbove
    ? (hiddenBelow ? scrollFadeTopBottom : scrollFade)
    : (hiddenBelow ? scrollFadeBottom : undefined);
  return { containerRef, onScroll: measure, fade };
}
