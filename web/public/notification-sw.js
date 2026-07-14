// Service worker for actionable notifications (see src/lib/notify.ts). Its only
// job is relaying notification clicks back to the app: an action button click
// carries its action id, a body click carries "" — both are posted to every
// window client along with the notification's data, and the window is focused.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    (async () => {
      const clients = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      for (const client of clients) {
        client.postMessage({
          type: "daisy-notification-click",
          action: event.action || "",
          data: event.notification.data || {},
        });
      }
      const focusable = clients.find((client) => "focus" in client);
      if (focusable) await focusable.focus();
    })(),
  );
});
