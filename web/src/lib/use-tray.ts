"use client";

// Bridges the app UI to the native menu-bar tray (desktop only). It keeps the
// tray's "Recent Conversations" submenu in sync with the session list, and reacts
// to the tray's "New Chat" and recent-conversation clicks by calling back into the
// app. In a plain browser it does nothing.

import { useEffect, useRef } from "react";
import { isTauri } from "@/lib/connection-store";

export interface TrayRecentItem {
  id: string;
  title: string;
}

interface TrayHandlers {
  recents: TrayRecentItem[];
  onNewChat: () => void;
  onOpenSession: (sessionId: string) => void;
}

export function useTray(handlers: TrayHandlers): void {
  // Latest handlers, so the event subscription can stay mounted-once while always
  // calling through to current state.
  const handlersRef = useRef(handlers);
  handlersRef.current = handlers;

  // Push the recent list to the tray whenever it actually changes.
  const recentsJson = JSON.stringify(handlers.recents);
  useEffect(() => {
    if (!isTauri()) return;
    (async () => {
      const { invoke } = await import("@tauri-apps/api/core");
      await invoke("update_tray_recent", {
        items: JSON.parse(recentsJson) as TrayRecentItem[],
      }).catch(() => {});
    })();
  }, [recentsJson]);

  // Subscribe once to the tray's menu actions.
  useEffect(() => {
    if (!isTauri()) return;
    const unlisteners: Array<() => void> = [];
    (async () => {
      const { listen } = await import("@tauri-apps/api/event");
      unlisteners.push(
        await listen("daisy://new-chat", () => handlersRef.current.onNewChat())
      );
      unlisteners.push(
        await listen<string>("daisy://open-session", (event) =>
          handlersRef.current.onOpenSession(event.payload)
        )
      );
    })();
    return () => {
      unlisteners.forEach((unlisten) => unlisten());
    };
  }, []);
}
