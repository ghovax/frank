// Service worker whose only job is relaying notification clicks, with their action, back to the app.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    (async () => {
      const clients = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      for (const client of clients) {
        client.postMessage({
          type: "langmesh-notification-click",
          action: event.action || "",
          data: event.notification.data || {},
        });
      }
      const focusable = clients.find((client) => "focus" in client);
      if (focusable) await focusable.focus();
    })(),
  );
});
