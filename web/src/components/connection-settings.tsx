"use client";

// The connection-management UI: pick the local server or a saved remote, add a new
// connection, or remove one. Extracted from the old launcher page so it can live in
// two places without duplication:
//   - full-screen ("page" variant) as the connection gate's disconnected fallback,
//   - inside a dialog ("dialog" variant) opened from the composer's connection
//     switcher ("Connection settings…").
// It never navigates. On a successful connect it activates the backend (points the
// API client at it and remembers it for next launch) and hands the chosen target to
// `onConnected`, letting the caller decide what happens next (render the app, or
// switch the live session and close the dialog).

import { Box, Button, Dialog, EmptyState, Flex, Input, Portal, Spinner, Text, VStack } from "@chakra-ui/react";
import { useCallback, useEffect, useState } from "react";
import { LuCheck, LuLaptop, LuPlug, LuPlus, LuRotateCcw, LuServer, LuTrash2 } from "react-icons/lu";
import { toaster } from "@/components/ui/toaster";
import {
  activateConnection,
  checkConnection,
  LOCAL_CONNECTION_TARGET,
  LOCAL_DEFAULT_URL,
  LOCAL_TARGET_ID,
  startLocalServer,
  waitForConnection,
  type ConnectionTarget,
} from "@/lib/connection";
import {
  deleteConnection,
  isTauri,
  listConnections,
  saveConnection,
  type ConnectionProfile,
} from "@/lib/connection-store";

export function ConnectionSettings({
  currentTargetId,
  onConnected,
  variant = "page",
}: {
  // The currently-active target, so it reads as "connected" rather than offering to
  // reconnect to where we already are. Undefined when nothing is connected yet (the
  // gate's first-launch fallback).
  currentTargetId?: string;
  // Fired after a backend is health-checked and activated. The caller renders the app
  // (gate) or switches the live session and closes the dialog (switcher).
  onConnected: (target: ConnectionTarget) => void;
  // "page" shows the Daisy brand lockup for the full-screen fallback; "dialog" drops
  // it since the dialog already has a titled header.
  variant?: "page" | "dialog";
}) {
  const [connections, setConnections] = useState<ConnectionProfile[]>([]);
  const [connecting, setConnecting] = useState(false);
  const [statusLabel, setStatusLabel] = useState("");
  const [failedTarget, setFailedTarget] = useState<string | null>(null);
  const [newUrl, setNewUrl] = useState("");

  const refreshConnections = useCallback(async () => {
    try {
      setConnections(await listConnections());
    } catch {
      setConnections([]);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    listConnections()
      .then((items) => {
        if (!cancelled) setConnections(items);
      })
      .catch(() => {
        if (!cancelled) setConnections([]);
      });
    return () => { cancelled = true; };
  }, []);

  const connectLocal = useCallback(async () => {
    setStatusLabel(isTauri() ? "Starting the local server…" : "Looking for a local server…");
    setConnecting(true);
    setFailedTarget(null);
    try {
      const url = await startLocalServer();
      const ok = isTauri()
        ? await waitForConnection(url)
        : await checkConnection(url, 2000);
      if (!ok) {
        setFailedTarget(LOCAL_TARGET_ID);
        setConnecting(false);
        toaster.create({
          type: "error",
          title: "Couldn't connect",
          description: isTauri()
            ? "The local server didn't start."
            : `No server responding at ${LOCAL_DEFAULT_URL.replace(/^https?:\/\//, "")}.`,
          closable: true,
        });
        return;
      }
      await activateConnection(url, LOCAL_TARGET_ID);
      onConnected({ ...LOCAL_CONNECTION_TARGET, url });
    } catch (caught) {
      setFailedTarget(LOCAL_TARGET_ID);
      setConnecting(false);
      toaster.create({
        type: "error",
        title: "Couldn't connect",
        description: caught instanceof Error ? caught.message : String(caught),
        closable: true,
      });
    }
  }, [onConnected]);

  const connectRemote = useCallback(
    async (profile: ConnectionProfile) => {
      setStatusLabel(`Connecting to ${profile.name}…`);
      setConnecting(true);
      setFailedTarget(null);
      try {
        const ok = await checkConnection(profile.url);
        if (!ok) {
          setFailedTarget(profile.id);
          setConnecting(false);
          toaster.create({
            type: "error",
            title: `Couldn't reach ${profile.name}`,
            description: `No response from ${profile.url}.`,
            closable: true,
          });
          return;
        }
        await activateConnection(profile.url, profile.id, profile.id);
        onConnected({ id: profile.id, name: profile.name, url: profile.url, kind: profile.kind });
      } catch (caught) {
        setFailedTarget(profile.id);
        setConnecting(false);
        toaster.create({
          type: "error",
          title: `Couldn't reach ${profile.name}`,
          description: caught instanceof Error ? caught.message : String(caught),
          closable: true,
        });
      }
    },
    [onConnected]
  );

  const handleAddConnection = useCallback(async () => {
    const url = newUrl.trim().replace(/\/+$/, "");
    if (!url) return;
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

  const localActive = currentTargetId === LOCAL_TARGET_ID;

  return (
    <VStack gap={5} w="100%" maxW={variant === "page" ? "420px" : undefined} px={variant === "page" ? 6 : 0}>
      {variant === "page" && (
        <VStack gap={3}>
          <Flex align="center" gap={2.5}>
            <Text fontSize="4xl" lineHeight="1">
              {"🌼"}
            </Text>
            <Text fontSize="4xl" fontWeight="bold" fontFamily="var(--font-display)" lineHeight="1">
              Daisy
            </Text>
          </Flex>
          <Text fontSize="sm" color="fg.muted" textAlign="center">
            Choose where the next conversation runs
          </Text>
        </VStack>
      )}

      {connecting ? (
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
            colorPalette={localActive ? "green" : "blue"}
            variant={localActive ? "subtle" : "solid"}
            borderRadius="md"
            onClick={connectLocal}
            disabled={localActive}
          >
            {localActive ? <LuCheck /> : failedTarget === LOCAL_TARGET_ID ? <LuRotateCcw /> : <LuLaptop />}
            {localActive ? "Local" : failedTarget === LOCAL_TARGET_ID ? "Retry local" : "Local"}
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
              connections.map((profile) => {
                const active = currentTargetId === profile.id;
                return (
                  <Flex
                    key={profile.id}
                    align="center"
                    gap={2}
                    borderWidth="1px"
                    borderColor={active ? "green.emphasized" : "border"}
                    borderRadius="md"
                    px={3}
                    py={2}
                  >
                    <Box color={active ? "green.fg" : "fg.muted"}>
                      <LuServer size={14} />
                    </Box>
                    <Box flex={1} minW={0}>
                      <Text fontSize="sm" fontWeight="medium" truncate>
                        {profile.url}
                      </Text>
                    </Box>
                    {active ? (
                      <Flex align="center" gap={1} color="green.fg" px={1} flexShrink={0}>
                        <LuCheck size={12} />
                        <Text fontSize="xs" fontWeight="medium">Connected</Text>
                      </Flex>
                    ) : (
                      <Button
                        size="xs"
                        variant="outline"
                        borderRadius="sm"
                        onClick={() => connectRemote(profile)}
                      >
                        {failedTarget === profile.id ? <LuRotateCcw size={12} /> : <LuPlug size={12} />}
                        {failedTarget === profile.id ? "Retry" : "Connect"}
                      </Button>
                    )}
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
                );
              })
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
              onClick={handleAddConnection}
              disabled={!newUrl.trim()}
            >
              <LuPlus size={14} />
              Save connection
            </Button>
          </VStack>
        </VStack>
      )}
    </VStack>
  );
}

// The dialog wrapper opened from the connection switcher. Same connection UI, but in
// a centered modal instead of a full page.
export function ConnectionSettingsDialog({
  open,
  onOpenChange,
  currentTargetId,
  onConnected,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  currentTargetId?: string;
  onConnected: (target: ConnectionTarget) => void;
}) {
  return (
    <Dialog.Root open={open} onOpenChange={(event) => onOpenChange(event.open)} placement="center">
      <Portal>
        <Dialog.Backdrop />
        <Dialog.Positioner>
          <Dialog.Content borderRadius="md" maxW="460px">
            <Dialog.Header>
              <Dialog.Title fontSize="sm">Connection settings</Dialog.Title>
            </Dialog.Header>
            <Dialog.Body pb={5}>
              <ConnectionSettings
                variant="dialog"
                currentTargetId={currentTargetId}
                onConnected={onConnected}
              />
            </Dialog.Body>
          </Dialog.Content>
        </Dialog.Positioner>
      </Portal>
    </Dialog.Root>
  );
}
