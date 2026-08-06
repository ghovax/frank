"use client";

/** Facts about the browser this page landed in, rather than about the app. */

import { useSyncExternalStore } from "react";

/** Nothing to subscribe to: an origin does not change without the document being replaced. */
function neverChanges(): () => void {
  return () => {};
}

/** Whether the thing pointing at this page can hover. */
export function useCoarsePointer(): boolean {
  return useSyncExternalStore(
    (onChange) => {
      if (typeof window === "undefined" || !window.matchMedia) return () => {};
      // `hover: none` rather than `pointer: coarse`: what matters is whether hovering can happen at all, and the two come apart — a laptop with a touchscreen is both.
      const query = window.matchMedia("(hover: none)");
      query.addEventListener("change", onChange);
      return () => query.removeEventListener("change", onChange);
    },
    () => typeof window !== "undefined" && !!window.matchMedia && window.matchMedia("(hover: none)").matches,
    // Pointer-driven is what renders first, and a phone corrects itself immediately.
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
