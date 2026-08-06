"use client";

import { useCallback, useEffect, useRef, useState } from "react";

// Soft edge fades for scrollable regions, replacing the hard divider borders that used to separate a panel's header/footer from its content.
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

// Scroll-position-driven fades for a plain scroll region (settings tabs, side panel bodies): each edge fades only while content is actually hidden beyond it, so nothing is dimmed while resting at either end.
export function useScrollEdgeFade() {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [hiddenAbove, setHiddenAbove] = useState(false);
  const [hiddenBelow, setHiddenBelow] = useState(false);
  const measure = useCallback(() => {
    const container = containerRef.current;
    if (!container) return;
    setHiddenAbove(container.scrollTop > 0);
    setHiddenBelow(container.scrollHeight - (container.scrollTop + container.clientHeight) > 8);
  }, []);
  useEffect(measure);
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const observer = new ResizeObserver(measure);
    observer.observe(container);
    return () => observer.disconnect();
  }, [measure]);
  const fade = hiddenAbove
    ? (hiddenBelow ? scrollFadeTopBottom : scrollFade)
    : (hiddenBelow ? scrollFadeBottom : undefined);
  return { containerRef, onScroll: measure, fade };
}
