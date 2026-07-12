// Thin wrappers over the Rust `artifact_*` commands that drive the embedded native
// webview used to render external websites at full browser fidelity (a real engine
// doing a top-level navigation, so X-Frame-Options / CSP framing never applies).
//
// These are no-ops outside the desktop (Tauri) build — the web build falls back to
// the server-side proxied iframe. Every call is best-effort: a failed invoke is
// swallowed so a rendering hiccup never surfaces as an app error.
import { isTauri } from "@/lib/connection-store";

export interface WebviewBounds {
  x: number;
  y: number;
  width: number;
  height: number;
}

// True only in the desktop shell — the single gate every caller checks before
// choosing the native path over the proxied iframe.
export function nativeWebviewAvailable(): boolean {
  return isTauri();
}

async function invokeWebview(command: string, args: Record<string, unknown>): Promise<void> {
  if (!isTauri()) return;
  try {
    const { invoke } = await import("@tauri-apps/api/core");
    await invoke(command, args);
  } catch {
    // Best-effort: the panel keeps its placeholder if the webview cannot be driven.
  }
}

// Show (creating on first use) the native artifact webview at the given rect,
// navigating it when the URL changes.
export function nativeWebviewShow(url: string, bounds: WebviewBounds): Promise<void> {
  return invokeWebview("artifact_show", { url, x: bounds.x, y: bounds.y, width: bounds.width, height: bounds.height });
}

// Reposition/resize without navigating — called as the panel's rect changes.
export function nativeWebviewSetBounds(bounds: WebviewBounds): Promise<void> {
  return invokeWebview("artifact_set_bounds", { x: bounds.x, y: bounds.y, width: bounds.width, height: bounds.height });
}

// Park the webview off-screen (kept alive) while the panel is closed or covered.
export function nativeWebviewHide(): Promise<void> {
  return invokeWebview("artifact_hide", {});
}

// Destroy the webview entirely (stops its scripts, media, and network).
export function nativeWebviewClose(): Promise<void> {
  return invokeWebview("artifact_close", {});
}
