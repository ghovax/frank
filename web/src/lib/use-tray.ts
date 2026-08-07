"use client";

// Bridges the app to the native menu-bar tray, and does nothing in a plain browser.

import { useEffect, useRef } from "react";
import { isTauri } from "@/lib/tauri";
import { swallowed } from "@/lib/swallowed";

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
  // Latest handlers, so the subscription stays mounted once while calling through to current state.
  const handlersRef = useRef(handlers);

  useEffect(() => {
    handlersRef.current = handlers;
  }, [handlers]);

  // Push the recent list to the tray whenever it actually changes.
  const recentsJson = JSON.stringify(handlers.recents);
  useEffect(() => {
    if (!isTauri()) return;
    (async () => {
      const { invoke } = await import("@tauri-apps/api/core");
      await invoke("update_tray_recent", {
        items: JSON.parse(recentsJson) as TrayRecentItem[],
      }).catch((caught) =>
        swallowed({ component: "tray", operation: "update the recent items" }, caught),
      );
    })();
  }, [recentsJson]);

  // Subscribe once to the tray's menu actions.
  useEffect(() => {
    if (!isTauri()) return;
    const unlisteners: Array<() => void> = [];
    (async () => {
      const { listen } = await import("@tauri-apps/api/event");
      unlisteners.push(await listen("langmesh://new-chat", () => handlersRef.current.onNewChat()));
      unlisteners.push(
        await listen<string>("langmesh://open-session", (event) =>
          handlersRef.current.onOpenSession(event.payload),
        ),
      );
    })();
    return () => {
      unlisteners.forEach((unlisten) => unlisten());
    };
  }, []);
}
