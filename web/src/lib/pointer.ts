"use client";

/** Facts about the browser this page landed in, rather than about the app. */

import { useSyncExternalStore } from "react";

/** Nothing to subscribe to: an origin does not change without the document being replaced. */
function neverChanges(): () => void {
  return () => {};
}

/** Whether the thing pointing at this page can hover, which a great deal of the interface assumes. */
export function useCoarsePointer(): boolean {
  return useSyncExternalStore(
    (onChange) => {
      if (typeof window === "undefined" || !window.matchMedia) return () => {};
      // Whether hovering can happen at all, which is not the same question as whether the pointer is coarse.
      const query = window.matchMedia("(hover: none)");
      query.addEventListener("change", onChange);
      return () => query.removeEventListener("change", onChange);
    },
    () =>
      typeof window !== "undefined" &&
      !!window.matchMedia &&
      window.matchMedia("(hover: none)").matches,
    // Pointer-driven renders first, since the other way round would briefly show controls built for a thumb.
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
