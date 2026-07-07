"use client";

// The connection-management UI: pick the local server or a saved remote, add a new
// connection, or remove one. Extracted from the old launcher page so it can live in
// two places without duplication:
//   - full-screen ("page" variant) as the connection gate's disconnected fallback,
//   - inside the main Settings dialog ("dialog" variant).
// It never navigates. On a successful connect it activates the backend (points the
// API client at it and remembers it for next launch) and hands the chosen target to
// `onConnected`, letting the caller decide what happens next (render the app, or
// switch the live session and close the dialog).

import { Box, Button, EmptyState, Field, Flex, Input, Spinner, Text, Textarea, VStack } from "@chakra-ui/react";
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
  onDirtyChange,
  variant = "page",
}: {
  // The currently-active target, so it reads as "connected" rather than offering to
  // reconnect to where we already are. Undefined when nothing is connected yet (the
  // gate's first-launch fallback).
  currentTargetId?: string;
  // Fired after a backend is health-checked and activated. The caller renders the app
  // (gate) or switches the live session and closes the dialog (switcher).
  onConnected: (target: ConnectionTarget) => void;
  onDirtyChange?: (dirty: boolean) => void;
  // "page" shows the Daisy brand lockup for the full-screen fallback; "dialog" drops
  // it since the dialog already has a titled header.
  variant?: "page" | "dialog";
}) {
  const [connections, setConnections] = useState<ConnectionProfile[]>([]);
  const [connecting, setConnecting] = useState(false);
  const [statusLabel, setStatusLabel] = useState("");
  const [failedTarget, setFailedTarget] = useState<string | null>(null);
  const [savingConnection, setSavingConnection] = useState(false);
  const [savingSshConnection, setSavingSshConnection] = useState(false);
  const [deletingConnectionId, setDeletingConnectionId] = useState<string | null>(null);
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
    const startedAt = performance.now();
    setSshLoading(true);
    try {
      setSshHosts(await listSshHosts());
    } catch {
      setSshHosts([]);
    } finally {
      window.setTimeout(() => setSshLoading(false), Math.max(0, 450 - (performance.now() - startedAt)));
    }
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => void refreshSshHosts(), 0);
    return () => window.clearTimeout(timer);
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
    const startedAt = performance.now();
    setSavingConnection(true);
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
    try {
      await saveConnection(profile);
      setNewUrl("");
      await refreshConnections();
    } finally {
      window.setTimeout(() => setSavingConnection(false), Math.max(0, 450 - (performance.now() - startedAt)));
    }
  }, [newUrl, refreshConnections]);

  const handleAddSshConnection = useCallback(async () => {
    const alias = sshAlias.trim();
    if (!alias) return;
    const startedAt = performance.now();
    setSavingSshConnection(true);
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
    try {
      await saveConnection(profile);
      setSshAlias("");
      setSshUser("");
      setSshPort("");
      setSshIdentityFile("");
      setSshLocalPort("");
      setSshRemotePort("8822");
      setSshContext("");
      await refreshConnections();
    } finally {
      window.setTimeout(() => setSavingSshConnection(false), Math.max(0, 450 - (performance.now() - startedAt)));
    }
  }, [sshAlias, sshHosts, sshUser, sshPort, sshIdentityFile, sshLocalPort, sshRemotePort, sshContext, refreshConnections]);

  const handleDelete = useCallback(
    async (id: string) => {
      const startedAt = performance.now();
      setDeletingConnectionId(id);
      try {
        await deleteConnection(id);
        await refreshConnections();
      } finally {
        window.setTimeout(() => setDeletingConnectionId(null), Math.max(0, 450 - (performance.now() - startedAt)));
      }
    },
    [refreshConnections]
  );

  const localActive = currentTargetId === LOCAL_TARGET_ID;
  const draftDirty = !!(
    newUrl.trim()
    || sshAlias.trim()
    || sshUser.trim()
    || sshPort.trim()
    || sshIdentityFile.trim()
    || sshLocalPort.trim()
    || sshContext.trim()
    || (sshRemotePort.trim() && sshRemotePort.trim() !== "8822")
  );

  useEffect(() => {
    onDirtyChange?.(draftDirty);
  }, [draftDirty, onDirtyChange]);

  return (
    <VStack gap={5} w="100%" h={variant === "dialog" ? "100%" : undefined} minH={0} maxW={variant === "page" ? "420px" : undefined} px={variant === "page" ? 6 : 0}>
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
        <VStack gap={4} w="100%" minH={0} align="stretch">
          <Button
            w="100%"
            variant={localActive ? "subtle" : "outline"}
            borderRadius="md"
            bg={localActive ? "bg.subtle" : "bg"}
            borderColor="border"
            color="fg"
            _hover={{ bg: "bg.muted" }}
            onClick={connectLocal}
            disabled={localActive}
          >
            {localActive ? <LuCheck /> : failedTarget === LOCAL_TARGET_ID ? <LuRotateCcw /> : <LuLaptop />}
            {localActive ? "Local server" : failedTarget === LOCAL_TARGET_ID ? "Retry local server" : "Local server"}
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
                      loading={deletingConnectionId === profile.id}
                      disabled={deletingConnectionId !== null}
                      aria-label={`Delete ${profile.url}`}
                    >
                      <LuTrash2 size={12} />
                    </Button>
                  </Flex>
                );
              })
            )}
          </VStack>

          <VStack gap={3} align="stretch">
            <Flex align="center" gap={1.5} color="fg.muted">
              <LuPlus size={15} />
              <Text fontSize="sm" fontWeight="bold">
                New connection
              </Text>
            </Flex>
            <Flex gap={2.5}>
              <Button
                size="sm"
                h="auto"
                py={2.5}
                variant={addMode === "url" ? "subtle" : "outline"}
                colorPalette={addMode === "url" ? "blue" : "gray"}
                borderRadius="md"
                flex={1}
                justifyContent="flex-start"
                onClick={() => setAddMode("url")}
              >
                <LuServer size={14} />
                <Box textAlign="left" pl={1.5}>
                  <Text fontSize="sm" fontWeight="medium">Server URL</Text>
                  <Text fontSize="xs" color="fg.muted">Direct HTTP endpoint</Text>
                </Box>
              </Button>
              <Button
                size="sm"
                h="auto"
                py={2.5}
                variant={addMode === "ssh" ? "subtle" : "outline"}
                colorPalette={addMode === "ssh" ? "blue" : "gray"}
                borderRadius="md"
                flex={1}
                justifyContent="flex-start"
                onClick={() => setAddMode("ssh")}
              >
                <LuNetwork size={14} />
                <Box textAlign="left" pl={1.5}>
                  <Text fontSize="sm" fontWeight="medium">SSH host</Text>
                  <Text fontSize="xs" color="fg.muted">From ~/.ssh/config</Text>
                </Box>
              </Button>
            </Flex>
            <Box minH={addMode === "ssh" ? "344px" : "164px"}>
              {addMode === "url" ? (
                <VStack align="stretch" gap={3}>
                  <Field.Root>
                    <Field.Label fontSize="xs">Server URL</Field.Label>
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
                    <Field.HelperText fontSize="2xs">
                      Use this for an already reachable Daisy server or an existing tunnel.
                    </Field.HelperText>
                  </Field.Root>
                  <Flex justify="flex-end">
                    <Button
                      size="sm"
                      variant="subtle"
                      borderRadius="md"
                      onClick={handleAddConnection}
                      loading={savingConnection}
                      disabled={!newUrl.trim() || savingConnection}
                    >
                      <LuPlus size={14} />
                      Save connection
                    </Button>
                  </Flex>
                </VStack>
              ) : (
                <VStack align="stretch" gap={3}>
                  <Field.Root>
                    <Field.Label fontSize="xs">Host alias</Field.Label>
                    {sshHosts.length > 0 && (
                      <Flex gap={1.5} wrap="wrap" mb={2}>
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
                      </Flex>
                    )}
                    <Flex gap={2} align="center" w="100%">
                      <Input flex={1} minW={0} size="sm" bg="bg.subtle" placeholder="biowulf, lab-gpu, coder.myworkspace" value={sshAlias} onChange={(event) => setSshAlias(event.target.value)} />
                      <Button size="sm" variant="ghost" borderRadius="sm" onClick={refreshSshHosts} loading={sshLoading} disabled={sshLoading} flexShrink={0}>
                        <LuRotateCcw size={12} />
                        Reload hosts
                      </Button>
                    </Flex>
                    <Field.HelperText fontSize="2xs">
                      We resolve this with ssh -G and open a local tunnel to the remote server.
                    </Field.HelperText>
                  </Field.Root>
                  <Flex gap={2} align="flex-start">
                    <Field.Root>
                      <Field.Label fontSize="xs">User override</Field.Label>
                      <Input size="sm" bg="bg.subtle" placeholder="Use ~/.ssh/config user" value={sshUser} onChange={(event) => setSshUser(event.target.value)} />
                    </Field.Root>
                    <Field.Root maxW="132px">
                      <Field.Label fontSize="xs">SSH port</Field.Label>
                      <Input size="sm" bg="bg.subtle" placeholder="22" value={sshPort} onChange={(event) => setSshPort(event.target.value.replace(/\D/g, ""))} />
                    </Field.Root>
                  </Flex>
                  <Field.Root>
                    <Field.Label fontSize="xs">Identity file override</Field.Label>
                    <Input size="sm" bg="bg.subtle" placeholder="Use ssh-agent or ~/.ssh/config IdentityFile" value={sshIdentityFile} onChange={(event) => setSshIdentityFile(event.target.value)} />
                  </Field.Root>
                  <Flex gap={2} align="flex-start">
                    <Field.Root>
                      <Field.Label fontSize="xs">Local tunnel port</Field.Label>
                      <Input size="sm" bg="bg.subtle" placeholder="Pick an open local port" value={sshLocalPort} onChange={(event) => setSshLocalPort(event.target.value.replace(/\D/g, ""))} />
                    </Field.Root>
                    <Field.Root>
                      <Field.Label fontSize="xs">Server port on host</Field.Label>
                      <Input size="sm" bg="bg.subtle" placeholder="8822" value={sshRemotePort} onChange={(event) => setSshRemotePort(event.target.value.replace(/\D/g, ""))} />
                    </Field.Root>
                  </Flex>
                  <Field.Root>
                    <Field.Label fontSize="xs">Host notes</Field.Label>
                    <Textarea size="sm" bg="bg.subtle" rows={3} placeholder="Scheduler, modules, environment rules, preferred scratch paths..." value={sshContext} onChange={(event) => setSshContext(event.target.value)} />
                  </Field.Root>
                  <Flex justify="flex-end">
                    <Button size="sm" variant="subtle" borderRadius="md" onClick={handleAddSshConnection} loading={savingSshConnection} disabled={!sshAlias.trim() || savingSshConnection}>
                      <LuPlus size={14} />
                      Save SSH connection
                    </Button>
                  </Flex>
                </VStack>
              )}
            </Box>
          </VStack>
        </VStack>
      )}
    </VStack>
  );
}
