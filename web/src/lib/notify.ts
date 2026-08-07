"use client";

import { swallowed } from "@/lib/swallowed";

// System notifications for calls awaiting a decision, degrading gracefully by platform capability.

const PERMISSION_TAG_PREFIX = "langmesh-permission-";
const APPROVE_ACTION = "approve";

type PermissionActionHandler = (requestId: string) => void;
let actionHandler: PermissionActionHandler | null = null;
let listenerAttached = false;
const notificationTokens = new Map<string, symbol>();

// The app's live decision callback, in a mutable slot because notifications outlive React renders.
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
    // The person declined, or the browser has no notification permission model.
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
        const payload = event.data as {
          type?: string;
          action?: string;
          data?: { requestId?: string };
        } | null;
        if (payload?.type !== "langmesh-notification-click") return;
        const requestId = payload.data?.requestId;
        if (payload.action === APPROVE_ACTION && requestId) actionHandler?.(requestId);
        // A body click just focuses the app: the overlay is on screen with the full context and both choices.
      });
    }
    return registration;
  } catch {
    // No notification support here; the in-app cue is the fallback.
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
    await registration
      .showNotification(title, options)
      .catch((caught) =>
        swallowed(
          { component: "notifications", operation: "show a permission notification" },
          caught,
        ),
      );
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
