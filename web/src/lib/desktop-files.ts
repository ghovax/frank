// Desktop-only file capture. In the Tauri app a dropped or picked file carries its
// real OS path, which is what lets attachments be referenced *in place* (no copy). All
// Tauri modules are imported dynamically and only under Tauri, so the static export and
// the plain-browser build never touch them (there the composer falls back to the web
// <input>/dataTransfer byte path).

import { isTauri } from "@/lib/app-state";

// Open the native file picker and return the chosen absolute paths (empty if cancelled).
export async function pickDesktopFilePaths(): Promise<string[]> {
  if (!isTauri()) return [];
  const { open } = await import("@tauri-apps/plugin-dialog");
  const selection = await open({ multiple: true, directory: false });
  if (!selection) return [];
  return Array.isArray(selection) ? selection : [selection];
}

export type DesktopDropPhase = "enter" | "over" | "drop" | "leave";

export interface DesktopDropEvent {
  phase: DesktopDropPhase;
  // Absolute paths of the dropped files (present on "enter" and "drop").
  paths: string[];
  // Pointer position in CSS pixels relative to the webview, or null on "leave".
  position: { x: number; y: number } | null;
}

// Subscribe to the webview's native file-drop stream. Tauri intercepts OS file drops
// (they never reach the HTML drag events), delivering the file paths plus the pointer
// position in *physical* pixels — converted here to CSS pixels so callers can hit-test
// against DOM rects. Returns an unlisten function; a no-op outside Tauri.
export async function watchDesktopFileDrop(
  onEvent: (event: DesktopDropEvent) => void,
): Promise<() => void> {
  if (!isTauri()) return () => {};
  const { getCurrentWebview } = await import("@tauri-apps/api/webview");
  const ratio = typeof window !== "undefined" ? window.devicePixelRatio || 1 : 1;
  const toCss = (position: { x: number; y: number }) => ({ x: position.x / ratio, y: position.y / ratio });
  const unlisten = await getCurrentWebview().onDragDropEvent((event) => {
    const payload = event.payload;
    if (payload.type === "enter") {
      onEvent({ phase: "enter", paths: payload.paths, position: toCss(payload.position) });
    } else if (payload.type === "over") {
      onEvent({ phase: "over", paths: [], position: toCss(payload.position) });
    } else if (payload.type === "drop") {
      onEvent({ phase: "drop", paths: payload.paths, position: toCss(payload.position) });
    } else {
      onEvent({ phase: "leave", paths: [], position: null });
    }
  });
  return unlisten;
}
