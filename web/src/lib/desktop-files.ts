// Desktop-only file capture, where a dropped file carries the real OS path an attachment references.

import { isTauri } from "@/lib/tauri";

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

// The webview's native file-drop stream, its physical pixels converted so callers can hit-test.
export async function watchDesktopFileDrop(
  onEvent: (event: DesktopDropEvent) => void,
): Promise<() => void> {
  if (!isTauri()) return () => {};
  const { getCurrentWebview } = await import("@tauri-apps/api/webview");
  const ratio = typeof window !== "undefined" ? window.devicePixelRatio || 1 : 1;
  const toCss = (position: { x: number; y: number }) => ({
    x: position.x / ratio,
    y: position.y / ratio,
  });
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
