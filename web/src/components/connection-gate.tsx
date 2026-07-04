"use client";

// The startup launcher. It gates the app on a working backend connection: on
// launch it auto-connects to whatever was used last (starting the bundled local
// server if that's the target), and only shows the launcher UI when there's a
// choice to make or the last target is unreachable. Once connected it renders the
// app; the status pill can reopen it to switch backends.

import { Box, Button, EmptyState, Flex, Input, Spinner, Text, VStack } from "@chakra-ui/react";
import { useCallback, useEffect, useRef, useState } from "react";
import { LuLaptop, LuPlug, LuPlus, LuRotateCcw, LuServer, LuTrash2 } from "react-icons/lu";
import { toaster } from "@/components/ui/toaster";
import {
  activateConnection,
  checkConnection,
  getLastTargetId,
  LOCAL_DEFAULT_URL,
  LOCAL_TARGET_ID,
  startLocalServer,
  waitForConnection,
} from "@/lib/connection";
import {
  deleteConnection,
  isTauri,
  listConnections,
  saveConnection,
  type ConnectionProfile,
} from "@/lib/connection-store";

type Phase = "init" | "launcher" | "connecting" | "connected";

// Event that reopens the launcher (home view) over a live session — dispatched by
// the sidebar Home button and the connection switcher's "Connection settings".
export const OPEN_LAUNCHER_EVENT = "daisy:open-launcher";

// Event that asks the gate to switch to a specific target (LOCAL_TARGET_ID or a
// saved connection's id) — dispatched by the connection switcher's menu entries.
export const CONNECT_TO_EVENT = "daisy:connect-to";

// Marks that the imminent reload is a deliberate connection switch (not a fresh
// launch), so the reloaded gate auto-connects to the just-chosen backend instead of
// returning to the launcher. A fresh launch has no flag and shows the launcher.
const RECONNECT_FLAG = "daisy.reconnect";

function reloadIntoConnection(): void {
  try {
    sessionStorage.setItem(RECONNECT_FLAG, "1");
  } catch {
    // best-effort; without it the reload simply lands on the launcher
  }
  window.location.reload();
}

export function ConnectionGate({ children }: { children: React.ReactNode }) {
  const [phase, setPhase] = useState<Phase>("init");
  const [connections, setConnections] = useState<ConnectionProfile[]>([]);
  const [statusLabel, setStatusLabel] = useState("Connecting…");
  // The target ("local" or a profile id) whose last attempt failed, so its button
  // can offer a retry. Errors themselves surface as toasts, not inline.
  const [failedTarget, setFailedTarget] = useState<string | null>(null);
  const [newUrl, setNewUrl] = useState("");
  // Whether we were already connected before this connect attempt — switching a
  // live session needs a reload so caches/streams restart cleanly against the new
  // backend; the first connect just mounts the app.
  const wasConnectedRef = useRef(false);

  const refreshConnections = useCallback(async () => {
    try {
      setConnections(await listConnections());
    } catch {
      setConnections([]);
    }
  }, []);

  // Surface a connection failure as a toast (matching the rest of the app), and
  // remember which target failed so its button can offer a retry.
  const reportFailure = useCallback((target: string, title: string, description: string) => {
    setFailedTarget(target);
    setPhase("launcher");
    toaster.create({ type: "error", title, description, closable: true });
  }, []);

  // Bring up the bundled/local server and wait for it to answer.
  const connectLocal = useCallback(async () => {
    setStatusLabel(isTauri() ? "Starting the local server…" : "Looking for a local server…");
    setPhase("connecting");
    try {
      const url = await startLocalServer();
      const ok = isTauri()
        ? await waitForConnection(url)
        : await checkConnection(url, 2000);
      if (!ok) {
        reportFailure(
          LOCAL_TARGET_ID,
          "Couldn't connect",
          isTauri()
            ? "The local server didn't start."
            : `No server responding at ${LOCAL_DEFAULT_URL.replace(/^https?:\/\//, "")}.`
        );
        return;
      }
      setFailedTarget(null);
      await activateConnection(url, LOCAL_TARGET_ID);
      if (wasConnectedRef.current) window.location.reload();
      else setPhase("connected");
    } catch (caught) {
      reportFailure(LOCAL_TARGET_ID, "Couldn't connect", caught instanceof Error ? caught.message : String(caught));
    }
  }, [reportFailure]);

  const connectRemote = useCallback(async (profile: ConnectionProfile) => {
    setStatusLabel(`Connecting to ${profile.name}…`);
    setPhase("connecting");
    try {
      const ok = await checkConnection(profile.url);
      if (!ok) {
        reportFailure(profile.id, `Couldn't reach ${profile.name}`, `No response from ${profile.url}.`);
        return;
      }
      setFailedTarget(null);
      await activateConnection(profile.url, profile.id, profile.id);
      if (wasConnectedRef.current) window.location.reload();
      else setPhase("connected");
    } catch (caught) {
      reportFailure(profile.id, `Couldn't reach ${profile.name}`, caught instanceof Error ? caught.message : String(caught));
    }
  }, [reportFailure]);

  // On launch, always present the choice — connect to this machine, or pick a
  // previously-used remote — rather than silently reconnecting. The launcher is the
  // home view the app returns to; it never auto-connects.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      await refreshConnections();
      if (!cancelled) setPhase("launcher");
    })();
    return () => {
      cancelled = true;
    };
  }, [refreshConnections]);

  // Let the status pill reopen the launcher to switch backends.
  useEffect(() => {
    const open = () => {
      wasConnectedRef.current = true;
      setFailedTarget(null);
      void refreshConnections();
      setPhase("launcher");
    };
    window.addEventListener(OPEN_LAUNCHER_EVENT, open);
    return () => window.removeEventListener(OPEN_LAUNCHER_EVENT, open);
  }, [refreshConnections]);

  // Switch to a specific backend from the connection switcher, without a detour
  // through the launcher. Reconnecting a live session reloads on success.
  useEffect(() => {
    const onConnectTo = (event: Event) => {
      const targetId = (event as CustomEvent<string>).detail;
      wasConnectedRef.current = true;
      if (targetId === LOCAL_TARGET_ID) {
        void connectLocal();
        return;
      }
      void listConnections()
        .then((saved) => {
          const profile = saved.find((entry) => entry.id === targetId);
          if (profile) void connectRemote(profile);
        })
        .catch(() => {});
    };
    window.addEventListener(CONNECT_TO_EVENT, onConnectTo as EventListener);
    return () => window.removeEventListener(CONNECT_TO_EVENT, onConnectTo as EventListener);
  }, [connectLocal, connectRemote]);

  const handleAddConnection = useCallback(async () => {
    const url = newUrl.trim().replace(/\/+$/, "");
    if (!url) return;
    // A connection is just an address — no separate name. Derive a compact label
    // from the host for the few places that still show one (the switcher menu),
    // but the address is the identity the UI surfaces.
    let derivedName = url;
    try {
      derivedName = new URL(url).host || url;
    } catch {
      derivedName = url;
    }
    const profile: ConnectionProfile = {
      id: crypto.randomUUID(),
      name: derivedName,
      url,
      kind: "remote",
      createdAt: new Date().toISOString(),
      lastUsedAt: null,
    };
    await saveConnection(profile);
    setNewUrl("");
    await refreshConnections();
  }, [newUrl, refreshConnections]);

  const handleDelete = useCallback(
    async (id: string) => {
      await deleteConnection(id);
      await refreshConnections();
    },
    [refreshConnections]
  );

  if (phase === "connected") {
    // The live connection indicator/switcher lives in the chat input toolbar
    // (ConnectionSwitcher), not here — this just renders the app.
    return <>{children}</>;
  }

  const isBusy = phase === "connecting" || phase === "init";

  return (
    <Flex
      h="100dvh"
      align="center"
      justify="center"
      pt="var(--titlebar-height, 0px)"
      boxSizing="border-box"
      bg="bg.subtle"
    >
      <VStack gap={5} w="100%" maxW="420px" px={6}>
        <VStack gap={1.5}>
          <Flex align="center" gap={2.5}>
            <Text fontSize="4xl" lineHeight="1">
              🌼
            </Text>
            <Text fontSize="4xl" fontWeight="bold" fontFamily="var(--font-display)" lineHeight="1">
              Daisy
            </Text>
          </Flex>
          <Text fontSize="sm" color="fg.muted" textAlign="center">
            Connect to a harness server to begin.
          </Text>
        </VStack>

        {isBusy ? (
          <Flex gap={3} py={6} align="center" justify="center">
            <Spinner size="md" color="blue.solid" />
            <Text fontSize="md" color="fg.muted">
              {statusLabel}
            </Text>
          </Flex>
        ) : (
          <VStack gap={4} w="100%" align="stretch">
            <Button
              w="100%"
              colorPalette="blue"
              borderRadius="md"
              onClick={() => connectLocal()}
            >
              {failedTarget === LOCAL_TARGET_ID ? <LuRotateCcw /> : <LuLaptop />}
              {failedTarget === LOCAL_TARGET_ID ? "Retry this machine" : "This machine"}
            </Button>

            <VStack gap={2} align="stretch">
              <Flex align="center" gap={1.5} color="fg.muted">
                <LuServer size={15} />
                <Text fontSize="sm" fontWeight="bold">
                  Saved connections
                </Text>
              </Flex>
              {connections.length === 0 ? (
                <EmptyState.Root size="sm">
                  <EmptyState.Content pt={2}>
                    <EmptyState.Indicator>
                      <LuServer />
                    </EmptyState.Indicator>
                    <VStack gap={0}>
                      <EmptyState.Title fontSize="sm">No saved connections</EmptyState.Title>
                      <EmptyState.Description fontSize="xs">
                        Add a server address below to reach it later
                      </EmptyState.Description>
                    </VStack>
                  </EmptyState.Content>
                </EmptyState.Root>
              ) : (
                connections.map((profile) => (
                  <Flex
                    key={profile.id}
                    align="center"
                    gap={2}
                    borderWidth="1px"
                    borderColor="border"
                    borderRadius="md"
                    px={3}
                    py={2}
                  >
                    <Box color="fg.muted">
                      <LuServer size={14} />
                    </Box>
                    <Box flex={1} minW={0}>
                      <Text fontSize="sm" fontWeight="medium" truncate>
                        {profile.url}
                      </Text>
                    </Box>
                    <Button
                      size="xs"
                      variant="outline"
                      borderRadius="sm"
                      onClick={() => connectRemote(profile)}
                    >
                      {failedTarget === profile.id ? <LuRotateCcw size={12} /> : <LuPlug size={12} />}
                      {failedTarget === profile.id ? "Retry" : "Connect"}
                    </Button>
                    <Button
                      size="xs"
                      variant="ghost"
                      colorPalette="red"
                      borderRadius="sm"
                      onClick={() => handleDelete(profile.id)}
                      aria-label={`Delete ${profile.url}`}
                    >
                      <LuTrash2 size={12} />
                    </Button>
                  </Flex>
                ))
              )}
            </VStack>

            <VStack
              gap={2.5}
              align="stretch"
              bg="bg.panel"
              borderWidth="1px"
              borderColor="border"
              borderRadius="lg"
              p={3}
            >
              <Flex align="center" gap={1.5} color="fg.muted">
                <LuPlus size={15} />
                <Text fontSize="sm" fontWeight="bold">
                  Add a connection
                </Text>
              </Flex>
              <Text fontSize="xs" color="fg.muted">
                Point at a server you can already reach — e.g. a local SSH tunnel:
                {" "}
                <Text as="span" fontFamily="var(--font-mono)">
                  ssh -L 8822:localhost:8822 host
                </Text>
              </Text>
              <Input
                size="sm"
                bg="bg.subtle"
                placeholder="http://localhost:8822"
                value={newUrl}
                onChange={(event) => setNewUrl(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") void handleAddConnection();
                }}
              />
              <Button
                size="sm"
                variant="subtle"
                borderRadius="md"
                onClick={() => handleAddConnection()}
                disabled={!newUrl.trim()}
              >
                <LuPlus size={14} />
                Save connection
              </Button>
            </VStack>
          </VStack>
        )}
      </VStack>
    </Flex>
  );
}
