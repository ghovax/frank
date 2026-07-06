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

import { Box, Button, Dialog, EmptyState, Flex, Input, Portal, Spinner, Text, Textarea, VStack } from "@chakra-ui/react";
import { useCallback, useEffect, useState } from "react";
import { LuCheck, LuLaptop, LuNetwork, LuPlug, LuPlus, LuRotateCcw, LuServer, LuTrash2 } from "react-icons/lu";
import { toaster } from "@/components/ui/toaster";
import {
  activateConnection,
  checkConnection,
  listSshHosts,
  LOCAL_CONNECTION_TARGET,
  LOCAL_DEFAULT_URL,
  LOCAL_TARGET_ID,
  resolveReachableConnectionUrl,
  startLocalServer,
  waitForConnection,
  type ConnectionTarget,
  type SshHost,
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
  const [addMode, setAddMode] = useState<"url" | "ssh">("url");
  const [sshHosts, setSshHosts] = useState<SshHost[]>([]);
  const [sshLoading, setSshLoading] = useState(false);
  const [sshAlias, setSshAlias] = useState("");
  const [sshUser, setSshUser] = useState("");
  const [sshPort, setSshPort] = useState("");
  const [sshIdentityFile, setSshIdentityFile] = useState("");
  const [sshLocalPort, setSshLocalPort] = useState("");
  const [sshRemotePort, setSshRemotePort] = useState("8822");
  const [sshContext, setSshContext] = useState("");

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

  const refreshSshHosts = useCallback(async () => {
    if (!isTauri()) {
      setSshHosts([]);
      return;
    }
    setSshLoading(true);
    try {
      setSshHosts(await listSshHosts());
    } catch {
      setSshHosts([]);
    } finally {
      setSshLoading(false);
    }
  }, []);

  useEffect(() => {
    void refreshSshHosts();
  }, [refreshSshHosts]);

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
        const url = await resolveReachableConnectionUrl(profile);
        const ok = profile.kind === "ssh"
          ? await waitForConnection(url)
          : await checkConnection(url);
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
        await activateConnection(url, profile.id, profile.id);
        onConnected({ ...profile, url });
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

  const handleAddSshConnection = useCallback(async () => {
    const alias = sshAlias.trim();
    if (!alias) return;
    const discovered = sshHosts.find((host) => host.alias === alias);
    const profile: ConnectionProfile = {
      id: crypto.randomUUID(),
      name: alias,
      url: `ssh://${alias}`,
      kind: "ssh",
      createdAt: new Date().toISOString(),
      lastUsedAt: null,
      sshHostAlias: alias,
      sshHostName: discovered?.hostName,
      sshUser: sshUser.trim() || discovered?.user || undefined,
      sshPort: sshPort.trim() ? Number(sshPort.trim()) : discovered?.port ?? null,
      sshIdentityFile: sshIdentityFile.trim() || discovered?.identityFiles[0] || undefined,
      sshLocalPort: sshLocalPort.trim() ? Number(sshLocalPort.trim()) : null,
      sshRemotePort: sshRemotePort.trim() ? Number(sshRemotePort.trim()) : 8822,
      sshContext: sshContext.trim() || undefined,
    };
    await saveConnection(profile);
    setSshAlias("");
    setSshUser("");
    setSshPort("");
    setSshIdentityFile("");
    setSshLocalPort("");
    setSshRemotePort("8822");
    setSshContext("");
    await refreshConnections();
  }, [sshAlias, sshHosts, sshUser, sshPort, sshIdentityFile, sshLocalPort, sshRemotePort, sshContext, refreshConnections]);

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
                      {profile.kind === "ssh" ? <LuNetwork size={14} /> : <LuServer size={14} />}
                    </Box>
                    <Box flex={1} minW={0}>
                      <Text fontSize="sm" fontWeight="medium" truncate>
                        {profile.name}
                      </Text>
                      <Text fontSize="xs" color="fg.muted" truncate>
                        {profile.kind === "ssh" ? profile.sshHostAlias : profile.url}
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
            <Flex gap={1} bg="bg.subtle" borderRadius="sm" p={1}>
              <Button size="xs" variant={addMode === "url" ? "solid" : "ghost"} borderRadius="sm" flex={1} onClick={() => setAddMode("url")}>
                <LuServer size={13} />
                Server URL
              </Button>
              <Button size="xs" variant={addMode === "ssh" ? "solid" : "ghost"} borderRadius="sm" flex={1} onClick={() => setAddMode("ssh")}>
                <LuNetwork size={13} />
                SSH host
              </Button>
            </Flex>
            {addMode === "url" ? (
              <>
                <Text fontSize="xs" color="fg.muted">
                  Point at a server you can already reach, such as an existing SSH tunnel.
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
              </>
            ) : (
              <>
                <Text fontSize="xs" color="fg.muted">
                  Pick a host from ~/.ssh/config or type an alias. Daisy opens a local tunnel to the remote server port.
                </Text>
                <Flex gap={1.5} wrap="wrap">
                  {sshHosts.slice(0, 8).map((host) => (
                    <Button
                      key={host.alias}
                      size="xs"
                      variant={sshAlias === host.alias ? "solid" : "outline"}
                      borderRadius="sm"
                      onClick={() => {
                        setSshAlias(host.alias);
                        setSshUser(host.user);
                        setSshPort(String(host.port || 22));
                        setSshIdentityFile(host.identityFiles[0] ?? "");
                      }}
                    >
                      {host.alias}
                    </Button>
                  ))}
                  <Button size="xs" variant="ghost" borderRadius="sm" onClick={refreshSshHosts} loading={sshLoading}>
                    <LuRotateCcw size={12} />
                  </Button>
                </Flex>
                <Input size="sm" bg="bg.subtle" placeholder="Host alias" value={sshAlias} onChange={(event) => setSshAlias(event.target.value)} />
                <Flex gap={2}>
                  <Input size="sm" bg="bg.subtle" placeholder="User override" value={sshUser} onChange={(event) => setSshUser(event.target.value)} />
                  <Input size="sm" bg="bg.subtle" placeholder="SSH port" value={sshPort} onChange={(event) => setSshPort(event.target.value.replace(/\D/g, ""))} maxW="110px" />
                </Flex>
                <Input size="sm" bg="bg.subtle" placeholder="Identity file override" value={sshIdentityFile} onChange={(event) => setSshIdentityFile(event.target.value)} />
                <Flex gap={2}>
                  <Input size="sm" bg="bg.subtle" placeholder="Local port auto" value={sshLocalPort} onChange={(event) => setSshLocalPort(event.target.value.replace(/\D/g, ""))} />
                  <Input size="sm" bg="bg.subtle" placeholder="Remote port" value={sshRemotePort} onChange={(event) => setSshRemotePort(event.target.value.replace(/\D/g, ""))} />
                </Flex>
                <Textarea size="sm" bg="bg.subtle" rows={3} placeholder="Anything Daisy should know about this host" value={sshContext} onChange={(event) => setSshContext(event.target.value)} />
                <Button size="sm" variant="subtle" borderRadius="md" onClick={handleAddSshConnection} disabled={!sshAlias.trim()}>
                  <LuPlus size={14} />
                  Save SSH connection
                </Button>
              </>
            )}
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
