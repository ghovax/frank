"use client";

/**
 * Facts about the browser this page landed in, rather than about the app.
 *
 * Both of these are read through `useSyncExternalStore`, which is what it is for: a value that
 * lives outside React, may change without React being told, and must answer differently during a
 * server render than after hydration. Reading them into state from an effect does the same job
 * one render later and trips the lint rule that exists to say so.
 *
 * The server snapshot is the neutral one in each case. This app is a static export, so the first
 * render happens at build time with no window at all, and a snapshot that guessed would be a
 * hydration mismatch.
 */

import { useSyncExternalStore } from "react";

/** Nothing to subscribe to: an origin does not change without the document being replaced. */
function neverChanges(): () => void {
  return () => {};
}

/**
 * Whether the thing pointing at this page can hover.
 *
 * Asked because a great deal of an interface is built on the assumption that it can. Hover
 * reveals the sidebar's row actions, hover opens every tooltip, hover is how a control admits it
 * is a control — and a finger does none of that. Where the answer only affects appearance, CSS
 * answers it with `@media (hover: hover)` and nothing here needs to know; this is for the cases
 * where *behaviour* has to differ, which a stylesheet cannot express.
 */
export function useCoarsePointer(): boolean {
  return useSyncExternalStore(
    (onChange) => {
      if (typeof window === "undefined" || !window.matchMedia) return () => {};
      // `hover: none` rather than `pointer: coarse`: what matters is whether hovering can happen
      // at all, and the two come apart — a laptop with a touchscreen is both.
      const query = window.matchMedia("(hover: none)");
      query.addEventListener("change", onChange);
      return () => query.removeEventListener("change", onChange);
    },
    () => typeof window !== "undefined" && !!window.matchMedia && window.matchMedia("(hover: none)").matches,
    // Pointer-driven is what renders first, and a phone corrects itself immediately. The other
    // way round would briefly show a desktop controls built for a thumb.
    () => false,
  );
}

/** Where this page was served from, or `""` before there is a window to ask. */
export function useOrigin(): string {
  return useSyncExternalStore(
    neverChanges,
    () => (typeof window === "undefined" ? "" : window.location.origin),
    () => "",
  );
}
