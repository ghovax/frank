"use client";

// Whether this bundle is running inside the desktop shell, since the same export serves three surfaces.
export function isTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}
