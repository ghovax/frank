"use client";

// Connection gate. On `/chat` it auto-connects to the last-used backend (showing a
// loading state while connecting). If there's no saved target or the server is
// unreachable it falls back to the connection settings, shown full-screen, so the
// user can pick or add a backend without leaving the app. On `/` it renders children
// directly (the root route just redirects into `/chat`).
//
// The gate owns the auto-connect loop so a fresh tab or a deliberate reconnect
// (via `reloadIntoConnection`) lands in a working app without the connection picker
// flashing first.

import { Flex, Spinner, Text } from "@chakra-ui/react";
import { useEffect, useRef, useState } from "react";
import { usePathname } from "next/navigation";
import {
  activateConnection,
  checkConnection,
  getLastTargetId,
  LOCAL_DEFAULT_URL,
  LOCAL_TARGET_ID,
  startLocalServer,
  waitForConnection,
} from "@/lib/connection";
import { isTauri, listConnections } from "@/lib/connection-store";
import { ConnectionSettings } from "@/components/connection-settings";
import { toaster } from "@/components/ui/toaster";

// Marks that the imminent reload is a deliberate connection switch (not a fresh
// launch), so the reloaded gate auto-connects to the just-chosen backend instead of
// showing the picker. A fresh launch has no flag and shows the picker on failure.
export const RECONNECT_FLAG = "daisy.reconnect";

export function reloadIntoConnection(): void {
  try {
    sessionStorage.setItem(RECONNECT_FLAG, "1");
  } catch {
    // best-effort; without it the reload lands on the connection picker
  }
  window.location.reload();
}

export function ConnectionGate({ children }: { children: React.ReactNode }) {
  const [statusLabel, setStatusLabel] = useState("Connecting…");
  // "connecting" while the auto-connect loop runs, "settings" when it can't reach a
  // backend (show the picker), "ready" once connected (render the app).
  const [phase, setPhase] = useState<"connecting" | "settings" | "ready">("connecting");
  const pathname = usePathname();
  const aliveRef = useRef(true);

  useEffect(() => {
    aliveRef.current = true;
    return () => { aliveRef.current = false; };
  }, []);

  // Auto-connect on `/chat`; pass through on `/` (which redirects into `/chat`).
  useEffect(() => {
    if (pathname === "/") {
      setPhase("ready");
      return;
    }

    let cancelled = false;
    setPhase("connecting");

    (async () => {
      // Check the reconnect flag first (deliberate switch from a previous session).
      const reconnecting =
        typeof sessionStorage !== "undefined"
          ? sessionStorage.getItem(RECONNECT_FLAG)
          : null;
      if (reconnecting) {
        try { sessionStorage.removeItem(RECONNECT_FLAG); } catch { /* best-effort */ }
        const lastTarget = await getLastTargetId();
        if (lastTarget === LOCAL_TARGET_ID) {
          setStatusLabel("Connecting…");
          const ok = await connectToLocal();
          if (!cancelled && aliveRef.current) setPhase(ok ? "ready" : "settings");
          return;
        }
        if (lastTarget) {
          const saved = await listConnections();
          const profile = saved.find((entry) => entry.id === lastTarget);
          if (profile) {
            setStatusLabel(`Connecting to ${profile.name}…`);
            const ok = await connectToRemote(profile.url, profile.name, profile.id);
            if (!cancelled && aliveRef.current) setPhase(ok ? "ready" : "settings");
            return;
          }
        }
        // Saved target points to a deleted profile — fall through to auto-connect.
      }

      // No reconnect flag — try the last target silently.
      const lastTarget = await getLastTargetId();
      if (lastTarget) {
        if (lastTarget === LOCAL_TARGET_ID) {
          setStatusLabel("Connecting to local server…");
          const ok = await connectToLocal();
          if (!cancelled && aliveRef.current) { if (ok) { setPhase("ready"); return; } }
        } else {
          const saved = await listConnections();
          const profile = saved.find((entry) => entry.id === lastTarget);
          if (profile) {
            setStatusLabel(`Connecting to ${profile.name}…`);
            const ok = await connectToRemote(profile.url, profile.name, profile.id);
            if (!cancelled && aliveRef.current) { if (ok) { setPhase("ready"); return; } }
          }
        }
      }

      // Nothing worked (or no saved target yet) — show the connection picker.
      if (!cancelled && aliveRef.current) {
        setPhase("settings");
      }
    })();

    return () => { cancelled = true; };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pathname]);

  async function connectToLocal(): Promise<boolean> {
    try {
      const url = await startLocalServer();
      const ok = isTauri()
        ? await waitForConnection(url)
        : await checkConnection(url, 2000);
      if (!ok) {
        toaster.create({
          type: "error",
          title: "Couldn't connect",
          description: isTauri()
            ? "The local server didn't start."
            : `No server responding at ${LOCAL_DEFAULT_URL.replace(/^https?:\/\//, "")}.`,
          closable: true,
        });
        return false;
      }
      await activateConnection(url, LOCAL_TARGET_ID);
      return true;
    } catch (caught) {
      toaster.create({
        type: "error",
        title: "Couldn't connect",
        description: caught instanceof Error ? caught.message : String(caught),
        closable: true,
      });
      return false;
    }
  }

  async function connectToRemote(url: string, name: string, id: string): Promise<boolean> {
    try {
      const ok = await checkConnection(url);
      if (!ok) {
        toaster.create({
          type: "error",
          title: `Couldn't reach ${name}`,
          description: `No response from ${url}.`,
          closable: true,
        });
        return false;
      }
      await activateConnection(url, id, id);
      return true;
    } catch (caught) {
      toaster.create({
        type: "error",
        title: `Couldn't reach ${name}`,
        description: caught instanceof Error ? caught.message : String(caught),
        closable: true,
      });
      return false;
    }
  }

  if (phase === "ready") {
    return <>{children}</>;
  }

  if (phase === "settings") {
    // ConnectionSettings has already activated the chosen backend by the time it calls
    // back; flip to "ready" so the app mounts and reads the live connection.
    return (
      <Flex
        h="100dvh"
        align="center"
        justify="center"
        pt="var(--titlebar-height, 0px)"
        boxSizing="border-box"
        bg="bg.subtle"
      >
        <ConnectionSettings variant="page" onConnected={() => setPhase("ready")} />
      </Flex>
    );
  }

  // Still trying — show a minimal loading state.
  return (
    <Flex h="100dvh" align="center" justify="center" pt="var(--titlebar-height, 0px)" boxSizing="border-box" bg="bg.subtle">
      <Flex gap={3} align="center" justify="center">
        <Spinner size="md" color="blue.solid" />
        <Text fontSize="md" color="fg.muted">
          {statusLabel}
        </Text>
      </Flex>
    </Flex>
  );
}
