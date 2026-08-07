"use client";

import { useCallback, useEffect, useRef, useState } from "react";

// Soft edge fades for scrollable regions, applied as a CSS mask instead of a hard divider.
const TOP = 14;
const BOTTOM = 28;
const topGradient = `linear-gradient(to bottom, transparent 0, #000 ${TOP}px, #000 100%)`;
const bottomGradient = `linear-gradient(to bottom, #000 0, #000 calc(100% - ${BOTTOM}px), transparent 100%)`;
const topBottomGradient = `linear-gradient(to bottom, transparent 0, #000 ${TOP}px, #000 calc(100% - ${BOTTOM}px), transparent 100%)`;

export const scrollFade = { maskImage: topGradient, WebkitMaskImage: topGradient } as const;

/** The fade as an overlay: painted above the scroller, so it cannot alter the layout it sits on. */
export const fadeOverlay = (edge: "top" | "bottom", height: number) => ({
  position: "absolute" as const,
  left: 0,
  right: 0,
  [edge]: 0,
  height: `${height}px`,
  pointerEvents: "none" as const,
  zIndex: 1,
  backgroundImage: `linear-gradient(to ${edge === "top" ? "bottom" : "top"}, var(--chakra-colors-bg-panel), transparent)`,
});
export const FADE_TOP = TOP;
export const FADE_BOTTOM = BOTTOM;

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
    // A tolerance, because the mask this drives moves the metrics by a fraction of a pixel.
    setHiddenAbove(container.scrollTop > 2);
    setHiddenBelow(container.scrollHeight - (container.scrollTop + container.clientHeight) > 8);
  }, []);
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    // Measured from the element and never from a render, and by size only: watching the subtree meant
    // markdown's own DOM churn fed the mask, and the mask fed the watcher that saw it.
    let scheduled = 0;
    const settle = () => {
      window.cancelAnimationFrame(scheduled);
      scheduled = window.requestAnimationFrame(measure);
    };
    settle();
    const observer = new ResizeObserver(settle);
    observer.observe(container);
    return () => {
      window.cancelAnimationFrame(scheduled);
      observer.disconnect();
    };
  }, [measure]);
  const fade = hiddenAbove
    ? hiddenBelow
      ? scrollFadeTopBottom
      : scrollFade
    : hiddenBelow
      ? scrollFadeBottom
      : undefined;
  return { containerRef, onScroll: measure, fade, hiddenAbove, hiddenBelow };
}
