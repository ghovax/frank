"use client";

// Whether this bundle is running inside the desktop shell.
//
// The same static export is the window in the Tauri app, the page `frank serve` hands a
// browser, and the phone's interface. A handful of things exist only in the first — the native
// tray, the file dialogs, the window chrome — and this is how they are asked for.
//
// It used to live in `app-state.ts`, next to the desktop's own SQLite database of client
// preferences. That database is gone: preferences are the daemon's now, so what remained in
// that file was this one question, which is not about storage at all.
export function isTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}
