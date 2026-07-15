"use client";

// System notifications for tool calls awaiting a decision, with the suggested
// action ("Allow once" — the same primary the on-screen overlay defaults to)
// as a notification action button where the platform supports it.
//
// Tiered by capability, degrading gracefully:
//   1. Service worker + showNotification → real action buttons (Chrome/Edge);
//      clicks come back via postMessage and route to the registered handler.
//   2. Plain Notification → no buttons; clicking the body focuses the app,
//      where the overlay is already waiting.
//   3. No Notification API (e.g. a webview without a notification bridge) →
//      silent no-op; the in-app overlay and attention sound still fire.
//
// Notifications are only shown while the window is unfocused — the overlay
// already owns the focused case — and are closed programmatically the moment
// the request is resolved from anywhere, so stale "Approval needed" toasts
// never linger in the notification center.

const PERMISSION_TAG_PREFIX = "daisy-permission-";
const APPROVE_ACTION = "approve";

type PermissionActionHandler = (requestId: string) => void;
let actionHandler: PermissionActionHandler | null = null;
let listenerAttached = false;
const notificationTokens = new Map<string, symbol>();

// The app's live decision callback (rebound as sessions change). Kept as a
// single mutable slot because notifications outlive React renders.
export function setPermissionNotificationHandler(handler: PermissionActionHandler | null): void {
  actionHandler = handler;
}

function notificationsSupported(): boolean {
  return typeof window !== "undefined" && "Notification" in window;
}

async function ensurePermission(): Promise<boolean> {
  if (!notificationsSupported()) return false;
  if (Notification.permission === "granted") return true;
  if (Notification.permission === "denied") return false;
  try {
    return (await Notification.requestPermission()) === "granted";
  } catch {
    return false;
  }
}

async function swRegistration(): Promise<ServiceWorkerRegistration | null> {
  if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) return null;
  try {
    const registration = await navigator.serviceWorker.register("/notification-sw.js");
    if (!listenerAttached) {
      listenerAttached = true;
      navigator.serviceWorker.addEventListener("message", (event) => {
        const payload = event.data as { type?: string; action?: string; data?: { requestId?: string } } | null;
        if (payload?.type !== "daisy-notification-click") return;
        const requestId = payload.data?.requestId;
        if (payload.action === APPROVE_ACTION && requestId) actionHandler?.(requestId);
        // A body click just focuses the app (the worker already did); the
        // overlay is on screen with the full context and all three choices.
      });
    }
    return registration;
  } catch {
    return null;
  }
}

export async function notifyPermissionRequest({
  requestId,
  title,
  body,
  actionLabel,
}: {
  requestId: string;
  title: string;
  body: string;
  actionLabel: string;
}): Promise<void> {
  // Focused window: the overlay is in view, a system notification would nag.
  if (typeof document === "undefined" || document.hasFocus()) return;
  const notificationToken = Symbol(requestId);
  notificationTokens.set(requestId, notificationToken);
  if (!(await ensurePermission())) return;
  if (notificationTokens.get(requestId) !== notificationToken) return;
  const tag = PERMISSION_TAG_PREFIX + requestId;
  const registration = await swRegistration();
  if (notificationTokens.get(requestId) !== notificationToken) return;
  if (registration) {
    // `actions` is not yet in TS's NotificationOptions (Notification API level 2).
    const options = {
      body,
      tag,
      data: { requestId },
      requireInteraction: true,
      actions: [{ action: APPROVE_ACTION, title: actionLabel }],
    } as NotificationOptions;
    await registration.showNotification(title, options).catch(() => {});
    return;
  }
  try {
    const notification = new Notification(title, { body, tag });
    notification.onclick = () => {
      window.focus();
      notification.close();
    };
  } catch {
    // Constructor may throw in webviews that expose the symbol without support.
  }
}

// Retract the notification for a resolved (or superseded) request.
export async function closePermissionNotification(requestId: string): Promise<void> {
  notificationTokens.delete(requestId);
  if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) return;
  try {
    const registration = await navigator.serviceWorker.getRegistration("/notification-sw.js");
    const open = await registration?.getNotifications({ tag: PERMISSION_TAG_PREFIX + requestId });
    for (const notification of open ?? []) notification.close();
  } catch {
    // Nothing to retract.
  }
}
