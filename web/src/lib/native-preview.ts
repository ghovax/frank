// Thin wrappers over the Rust `preview_*` commands that drive the embedded native
// webview used to preview external websites at full browser fidelity (a real engine
// doing a top-level navigation, so X-Frame-Options / CSP framing never applies).
//
// These are no-ops outside the desktop (Tauri) build — the web build falls back to
// the server-side proxied iframe. Every call is best-effort: a failed invoke is
// swallowed so a preview hiccup never surfaces as an app error.
import { isTauri } from "@/lib/connection-store";

export interface PreviewBounds {
  x: number;
  y: number;
  width: number;
  height: number;
}

// True only in the desktop shell — the single gate every caller checks before
// choosing the native path over the proxied iframe.
export function nativePreviewAvailable(): boolean {
  return isTauri();
}

async function invokePreview(command: string, args: Record<string, unknown>): Promise<void> {
  if (!isTauri()) return;
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    await invoke(command, args);
  } catch {
    // Best-effort: the panel keeps its placeholder if the webview cannot be driven.
  }
}

// Show (creating on first use) the native preview webview at the given rect,
// navigating it when the URL changes.
export function nativePreviewShow(url: string, bounds: PreviewBounds): Promise<void> {
  return invokePreview("preview_show", { url, x: bounds.x, y: bounds.y, width: bounds.width, height: bounds.height });
}

// Reposition/resize without navigating — called as the panel's rect changes.
export function nativePreviewSetBounds(bounds: PreviewBounds): Promise<void> {
  return invokePreview("preview_set_bounds", { x: bounds.x, y: bounds.y, width: bounds.width, height: bounds.height });
}

// Park the webview off-screen (kept alive) while the panel is closed or covered.
export function nativePreviewHide(): Promise<void> {
  return invokePreview("preview_hide", {});
}

// Destroy the webview entirely (stops its scripts, media, and network).
export function nativePreviewClose(): Promise<void> {
  return invokePreview("preview_close", {});
}
