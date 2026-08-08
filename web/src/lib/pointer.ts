"use client";

/** Facts about the browser this page landed in, rather than about the app. */

import { useSyncExternalStore } from "react";

/** Nothing to subscribe to: an origin does not change without the document being replaced. */
function neverChanges(): () => void {
  return () => {};
}

// Whether hovering can happen at all, which is not the same question as whether the pointer is coarse.
const HOVERLESS = "(hover: none)";

// Declared once rather than per render: a fresh subscriber makes React drop and re-establish the listener
// after every commit, and this hook rides in every tooltip and every menu on the page.
function subscribeHoverless(onChange: () => void): () => void {
  if (typeof window === "undefined" || !window.matchMedia) return () => {};
  const query = window.matchMedia(HOVERLESS);
  query.addEventListener("change", onChange);
  return () => query.removeEventListener("change", onChange);
}

function hoverless(): boolean {
  return (
    typeof window !== "undefined" && !!window.matchMedia && window.matchMedia(HOVERLESS).matches
  );
}

// Pointer-driven renders first, since the other way round would briefly show controls built for a thumb.
function assumeHover(): boolean {
  return false;
}

/** Whether the thing pointing at this page can hover, which a great deal of the interface assumes. */
export function useCoarsePointer(): boolean {
  return useSyncExternalStore(subscribeHoverless, hoverless, assumeHover);
}

/** Where this page was served from, or `""` before there is a window to ask. */
export function useOrigin(): string {
  return useSyncExternalStore(
    neverChanges,
    () => (typeof window === "undefined" ? "" : window.location.origin),
    () => "",
  );
}
