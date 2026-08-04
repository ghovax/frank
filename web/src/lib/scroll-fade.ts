"use client";

import { useCallback, useEffect, useRef, useState } from "react";

// Soft edge fades for scrollable regions, replacing the hard divider borders that used
// to separate a panel's header/footer from its content. Applied via a CSS mask (black =
// visible, transparent = hidden).
//
// `scrollFade` fades only the TOP: content dissolves as it scrolls up under the header.
// A permanent bottom fade is deliberately avoided here because it would dim the last line
// of content once it is scrolled fully into view.
//
// `scrollFadeTopBottom` fades BOTH edges. Use it only while there is more content below
// the fold (i.e. NOT scrolled to the bottom) — e.g. the chat transcript toggles to this
// variant when scrolled up so the content softly fades above the composer, and back to the
// top-only variant at the bottom so the assistant's final words stay crisp.
//
// `scrollFadeBottom` fades only the BOTTOM. For regions with no top padding (e.g. the
// settings tabs), where a permanent top fade would dim the first line while resting at
// the top — use this while at the top with more below, and switch to the top variants
// once scrolled.
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

// Scroll-position-driven fades for a plain scroll region (settings tabs, side panel
// bodies): each edge fades only while content is actually hidden beyond it, so nothing
// is dimmed while resting at either end. Attach `containerRef` + `onScroll` to the
// scroll container and spread `fade` into its `css`. Content-size changes arrive via
// React re-renders (the after-render measure) and container resizes via the observer,
// so no content ref is needed. The chat transcript keeps its own variant of this logic,
// integrated with its pin-to-bottom machinery.
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
