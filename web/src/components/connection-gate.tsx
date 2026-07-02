"use client";

// The startup launcher. It gates the app on a working backend connection: on
// launch it auto-connects to whatever was used last (starting the bundled local
// server if that's the target), and only shows the launcher UI when there's a
// choice to make or the last target is unreachable. Once connected it renders the
// app; the status pill can reopen it to switch backends.

import { Box, Button, Flex, Input, Spinner, Text, VStack } from "@chakra-ui/react";
import { useCallback, useEffect, useRef, useState } from "react";
import { LuLaptop, LuPlug, LuPlus, LuRotateCcw, LuServer, LuTrash2 } from "react-icons/lu";
import { toaster } from "@/components/ui/toaster";
import {
  activateConnection,
  checkConnection,
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

// Event the status pill dispatches to reopen the launcher over a live session.
export const OPEN_LAUNCHER_EVENT = "daisy:open-launcher";

export function ConnectionGate({ children }: { children: React.ReactNode }) {
  const [phase, setPhase] = useState<Phase>("init");
  const [connections, setConnections] = useState<ConnectionProfile[]>([]);
  const [statusLabel, setStatusLabel] = useState("Connecting…");
  // The name of the backend currently connected — shown in the titlebar pill.
  const [connectedLabel, setConnectedLabel] = useState("This machine");
  // The target ("local" or a profile id) whose last attempt failed, so its button
  // can offer a retry. Errors themselves surface as toasts, not inline.
  const [failedTarget, setFailedTarget] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
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
      setConnectedLabel("This machine");
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
      setConnectedLabel(profile.name);
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

  const handleAddConnection = useCallback(async () => {
    const name = newName.trim();
    const url = newUrl.trim().replace(/\/+$/, "");
    if (!name || !url) return;
    const profile: ConnectionProfile = {
      id: crypto.randomUUID(),
      name,
      url,
      kind: "remote",
      createdAt: new Date().toISOString(),
      lastUsedAt: null,
    };
    await saveConnection(profile);
    setNewName("");
    setNewUrl("");
    await refreshConnections();
  }, [newName, newUrl, refreshConnections]);

  const handleDelete = useCallback(
    async (id: string) => {
      await deleteConnection(id);
      await refreshConnections();
    },
    [refreshConnections]
  );

  if (phase === "connected") {
    const reopenLauncher = () => {
      wasConnectedRef.current = true;
      setFailedTarget(null);
      void refreshConnections();
      setPhase("launcher");
    };
    return (
      <>
        {isTauri() && (
          // Lives in the native titlebar strip (above the drag region so it's
          // clickable); shows the current backend and reopens the launcher.
          <Flex
            position="fixed"
            top={0}
            right={0}
            height="var(--titlebar-height, 0px)"
            align="center"
            pr={3}
            zIndex={1001}
          >
            <Button
              size="2xs"
              variant="subtle"
              borderRadius="full"
              gap={1.5}
              px={2.5}
              onClick={reopenLauncher}
              title="Switch connection"
            >
              <Box boxSize="7px" borderRadius="full" bg="green.solid" />
              <Text fontSize="xs" truncate maxW="160px">
                {connectedLabel}
              </Text>
            </Button>
          </Flex>
        )}
        {children}
      </>
    );
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
        <VStack gap={1}>
          <Text fontSize="4xl" lineHeight="1">
            🌼
          </Text>
          <Text fontSize="xl" fontWeight="bold" fontFamily="var(--font-display)">
            Daisy
          </Text>
          <Text fontSize="sm" color="fg.muted" textAlign="center">
            Connect to a harness server to begin.
          </Text>
        </VStack>

        {isBusy ? (
          <VStack gap={3} py={6}>
            <Spinner size="lg" color="blue.solid" />
            <Text fontSize="sm" color="fg.muted">
              {statusLabel}
            </Text>
          </VStack>
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

            {connections.length > 0 && (
              <VStack gap={2} align="stretch">
                <Flex align="center" gap={1.5} color="fg.muted">
                  <LuServer size={15} />
                  <Text fontSize="sm" fontWeight="bold">
                    Saved connections
                  </Text>
                </Flex>
                {connections.map((profile) => (
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
                        {profile.name}
                      </Text>
                      <Text fontSize="xs" color="fg.muted" truncate>
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
                      aria-label={`Delete ${profile.name}`}
                    >
                      <LuTrash2 size={12} />
                    </Button>
                  </Flex>
                ))}
              </VStack>
            )}

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
                placeholder="Name (e.g. Workstation)"
                value={newName}
                onChange={(event) => setNewName(event.target.value)}
              />
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
                disabled={!newName.trim() || !newUrl.trim()}
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
