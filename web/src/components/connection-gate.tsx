"use client";

// Connection gate. It auto-connects to the last-used backend (showing a loading
// state while connecting) on every route — the app has no route that works without a
// backend (the root `/` is the Projects home, which lists projects from the server).
// If there's no saved target or the server is unreachable it falls back to the
// connection settings, shown full-screen, so the user can pick or add a backend
// without leaving the app.
//
// The gate owns the auto-connect loop so a fresh tab or a deliberate reconnect
// (via `reloadIntoConnection`) lands in a working app without the connection picker
// flashing first.

import { Flex, Spinner, Text } from "@chakra-ui/react";
import { useTranslations } from "next-intl";
import { usePathname } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  activateConnection,
  checkConnection,
  getLastTargetId,
  LOCAL_DEFAULT_URL,
  LOCAL_TARGET_ID,
  resolveReachableConnectionUrl,
  startLocalServer,
  waitForConnection,
} from "@/lib/connection";
import { isTauri, listConnections, type ConnectionProfile } from "@/lib/connection-store";
import { ConnectionSettings } from "@/components/connection-settings";
import { toaster } from "@/components/ui/toaster";

// Marks that the imminent reload is a deliberate connection switch (not a fresh
// launch), so the reloaded gate auto-connects to the just-chosen backend instead of
// showing the picker. A fresh launch has no flag and shows the picker on failure.
export const RECONNECT_FLAG = "xeac.reconnect";

export function reloadIntoConnection(): void {
  try {
    sessionStorage.setItem(RECONNECT_FLAG, "1");
  } catch {
    // best-effort; without it the reload lands on the connection picker
  }
  window.location.reload();
}

export function ConnectionGate({ children }: { children: React.ReactNode }) {
  const translation = useTranslations("ConnectionGate");
  // The design gallery (/gallery) is a backend-less fixture sheet of the app's
  // building blocks, used for visual audits — the one route that must render
  // without a server, so it bypasses the gate entirely.
  const pathname = usePathname();
  const [statusLabel, setStatusLabel] = useState(translation("connecting"));
  // "connecting" while the auto-connect loop runs, "settings" when it can't reach a
  // backend (show the picker), "ready" once connected (render the app).
  const [phase, setPhase] = useState<"connecting" | "settings" | "ready">("connecting");
  const aliveRef = useRef(true);

  useEffect(() => {
    aliveRef.current = true;
    return () => { aliveRef.current = false; };
  }, []);

  const connectToLocal = useCallback(async (): Promise<boolean> => {
    try {
      const url = await startLocalServer();
      const ok = isTauri()
        ? await waitForConnection(url)
        : await checkConnection(url, 2000);
      if (!ok) {
        toaster.create({
          type: "error",
          title: translation("couldntConnect"),
          description: isTauri()
            ? translation("localServerNotStarted")
            : translation("noServerAt", { url: LOCAL_DEFAULT_URL.replace(/^https?:\/\//, "") }),
          closable: true,
        });
        return false;
      }
      await activateConnection(url, LOCAL_TARGET_ID);
      return true;
    } catch (caught) {
      toaster.create({
        type: "error",
        title: translation("couldntConnect"),
        description: caught instanceof Error ? caught.message : String(caught),
        closable: true,
      });
      return false;
    }
  }, [translation]);

  const connectToProfile = useCallback(async (profile: ConnectionProfile): Promise<boolean> => {
    try {
      const url = await resolveReachableConnectionUrl(profile);
      const ok = profile.kind === "ssh"
        ? await waitForConnection(url)
        : await checkConnection(url);
      if (!ok) {
        toaster.create({
          type: "error",
          title: translation("couldntReach", { name: profile.name }),
          description: translation("noResponseFrom", { url }),
          closable: true,
        });
        return false;
      }
      await activateConnection(url, profile.id, profile.id);
      return true;
    } catch (caught) {
      toaster.create({
        type: "error",
        title: translation("couldntReach", { name: profile.name }),
        description: caught instanceof Error ? caught.message : String(caught),
        closable: true,
      });
      return false;
    }
  }, [translation]);

  // Auto-connect on every route — no route works without a backend (except the
  // gallery, which skips the loop so it never spawns a server or toasts failures).
  useEffect(() => {
    if (pathname?.startsWith("/gallery")) return;
    let cancelled = false;

    (async () => {
      setPhase("connecting");
      // Check the reconnect flag first (deliberate switch from a previous session).
      const reconnecting =
        typeof sessionStorage !== "undefined"
          ? sessionStorage.getItem(RECONNECT_FLAG)
          : null;
      if (reconnecting) {
        try { sessionStorage.removeItem(RECONNECT_FLAG); } catch { /* best-effort */ }
        const lastTarget = await getLastTargetId();
        if (lastTarget === LOCAL_TARGET_ID) {
          setStatusLabel(translation("connecting"));
          const ok = await connectToLocal();
          if (!cancelled && aliveRef.current) setPhase(ok ? "ready" : "settings");
          return;
        }
        if (lastTarget) {
          const saved = await listConnections();
          const profile = saved.find((entry) => entry.id === lastTarget);
          if (profile) {
            setStatusLabel(translation("connectingTo", { name: profile.name }));
            const ok = await connectToProfile(profile);
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
          setStatusLabel(translation("reopeningLast"));
          const ok = await connectToLocal();
          if (!cancelled && aliveRef.current) { if (ok) { setPhase("ready"); return; } }
        } else {
          const saved = await listConnections();
          const profile = saved.find((entry) => entry.id === lastTarget);
          if (profile) {
            setStatusLabel(translation("connectingTo", { name: profile.name }));
            const ok = await connectToProfile(profile);
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
  }, [connectToLocal, connectToProfile, translation, pathname]);

  if (pathname?.startsWith("/gallery") || phase === "ready") {
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
