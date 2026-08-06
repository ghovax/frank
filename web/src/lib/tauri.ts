"use client";

// Whether this bundle is running inside the desktop shell.
export function isTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}
