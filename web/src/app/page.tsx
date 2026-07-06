"use client";

// The root launcher page. Shown when no server is connected. Lets the user pick
// the local server or a saved remote, or add a new connection. On success it
// navigates to /chat so the app renders.

import { Box, Button, EmptyState, Flex, Input, Spinner, Text, VStack } from "@chakra-ui/react";
import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
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

export default function HomePage() {
  const router = useRouter();
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
    setStatusLabel(isTauri() ? "Starting the local server\u2026" : "Looking for a local server\u2026");
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
      router.push("/chat");
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
  }, [router]);

  const connectRemote = useCallback(
    async (profile: ConnectionProfile) => {
      setStatusLabel(`Connecting to ${profile.name}\u2026`);
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
        router.push("/chat");
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
    [router]
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
        <VStack gap={3}>
          <Flex align="center" gap={2.5}>
            <Text fontSize="4xl" lineHeight="1">
              {"\uD83C\uDF3C"}
            </Text>
            <Text fontSize="4xl" fontWeight="bold" fontFamily="var(--font-display)" lineHeight="1">
              Daisy
            </Text>
          </Flex>
          <Text fontSize="sm" color="fg.muted" textAlign="center">
            Choose where the next conversation runs
          </Text>
        </VStack>

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
              colorPalette="blue"
              borderRadius="md"
              onClick={connectLocal}
            >
              {failedTarget === LOCAL_TARGET_ID ? <LuRotateCcw /> : <LuLaptop />}
              {failedTarget === LOCAL_TARGET_ID ? "Retry local" : "Local"}
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
    </Flex>
  );
}
