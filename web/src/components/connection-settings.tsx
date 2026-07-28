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

import { Box, Button, EmptyState, Field, Flex, Input, Text, Textarea, VStack } from "@chakra-ui/react";
import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { LuCheck, LuLaptop, LuNetwork, LuPlug, LuPlus, LuRotateCcw, LuServer, LuTrash2 } from "react-icons/lu";
import { FrankMark } from "@/components/ui/frank-mark";
import { toaster } from "@/components/ui/toaster";
import { SectionHeader } from "@/components/ui/section-header";
import {
  activateConnection,
  checkConnection,
  listSshHosts,
  LOCAL_CONNECTION_TARGET,
  LOCAL_TARGET_ID,
  resolveReachableConnectionUrl,
  findLocalDaemon,
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

// Every "a place you can connect to" control is laid out from this one object: the local
// row, each saved row, and the two picker cards. They are not the same element — the cards
// are Chakra Buttons and the rows are Flexes — so left alone the rows inherit nothing while
// the cards inherit the button recipe (px 3.5, gap 2, radius l2). Matching them by eye is
// what let them drift: the rows ended up a hair wider, squarer, and with the icon 8px closer
// to its label than the cards sitting directly beneath them. Stated explicitly on both, they
// cannot disagree again, and neither depends on a recipe default that may change.
const CONNECTION_TILE = {
  borderWidth: "1px",
  borderRadius: "l2",
  px: 3.5,
  py: 2.5,
  gap: 2,
} as const;

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
  // "page" shows the Frank brand lockup for the full-screen fallback; "dialog" drops
  // it since the dialog already has a titled header.
  variant?: "page" | "dialog";
}) {
  const translation = useTranslations("ConnectionSettings");
  const [connections, setConnections] = useState<ConnectionProfile[]>([]);
  // Which target is mid-connect (its id), or null. Deriving `connecting` from this — instead
  // of a separate flag that swapped the entire picker out for a spinner — keeps the picker
  // mounted while just the clicked button shows its own loading state, so a failed attempt no
  // longer flashes the whole screen out and back in.
  const [connectingTarget, setConnectingTarget] = useState<string | null>(null);
  const connecting = connectingTarget !== null;
  const [failedTarget, setFailedTarget] = useState<string | null>(null);
  const [savingConnection, setSavingConnection] = useState(false);
  const [savingSshConnection, setSavingSshConnection] = useState(false);
  const [deletingConnectionId, setDeletingConnectionId] = useState<string | null>(null);
  const [newUrl, setNewUrl] = useState("");
  // Every `frankd` mints its own capability token at boot and publishes it in its runtime
  // directory. That directory is on the *remote* machine, so this client cannot read it —
  // the user pastes the token in, once, when saving the connection.
  const [newToken, setNewToken] = useState("");
  const [sshToken, setSshToken] = useState("");
  const [addMode, setAddMode] = useState<"url" | "ssh">("url");
  const [sshHosts, setSshHosts] = useState<SshHost[]>([]);
  const [sshLoading, setSshLoading] = useState(false);
  const [sshAlias, setSshAlias] = useState("");
  const [sshUser, setSshUser] = useState("");
  const [sshPort, setSshPort] = useState("");
  const [sshIdentityFile, setSshIdentityFile] = useState("");
  const [sshLocalPort, setSshLocalPort] = useState("");
  // No default: the daemon binds an ephemeral port, so there is no conventional number to
  // guess. `frank daemon endpoint` on that host reports the port and the token together.
  const [sshRemotePort, setSshRemotePort] = useState("");
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
    setConnectingTarget(LOCAL_TARGET_ID);
    setFailedTarget(null);
    // A loopback probe answers, or is refused, in single-digit milliseconds. Left alone the
    // spinner appears and vanishes inside one frame, so a successful connection looks like
    // nothing happened and a refused one looks like the button broke — it snaps to "Retry"
    // before a person can register that it was ever trying. Holding the attempt for a beat
    // is what makes the outcome readable; it is a floor on the animation, not a delay on
    // the work, which proceeds underneath it.
    const settled = new Promise((resolve) => window.setTimeout(resolve, 420));
    try {
      const [{ url, listening }] = await Promise.all([findLocalDaemon(), settled]);
      if (!listening) {
        setFailedTarget(LOCAL_TARGET_ID);
        setConnectingTarget(null);
        toaster.create({
          type: "error",
          title: translation("noLocalDaemon"),
          description: translation("noLocalDaemonHint", { url: url.replace(/^https?:\/\//, "") }),
          closable: true,
        });
        return;
      }
      await activateConnection(url, LOCAL_TARGET_ID);
      onConnected({ ...LOCAL_CONNECTION_TARGET, url });
    } catch (caught) {
      setFailedTarget(LOCAL_TARGET_ID);
      setConnectingTarget(null);
      toaster.create({
        type: "error",
        title: translation("couldNotConnect"),
        description: caught instanceof Error ? caught.message : String(caught),
        closable: true,
      });
    }
  }, [onConnected, translation]);

  const connectRemote = useCallback(
    async (profile: ConnectionProfile) => {
      setConnectingTarget(profile.id);
      setFailedTarget(null);
      try {
        const url = await resolveReachableConnectionUrl(profile);
        const ok = profile.kind === "ssh"
          ? await waitForConnection(url, { token: profile.token ?? "" })
          : await checkConnection(url, { token: profile.token ?? "" });
        if (!ok) {
          setFailedTarget(profile.id);
          setConnectingTarget(null);
          toaster.create({
            type: "error",
            title: translation("couldNotReach", { name: profile.name }),
            description: translation("noResponse", { url: profile.url }),
            closable: true,
          });
          return;
        }
        await activateConnection(url, profile.id, { token: profile.token ?? "", profileId: profile.id });
        onConnected({ ...profile, url });
      } catch (caught) {
        setFailedTarget(profile.id);
        setConnectingTarget(null);
        toaster.create({
          type: "error",
          title: translation("couldNotReach", { name: profile.name }),
          description: caught instanceof Error ? caught.message : String(caught),
          closable: true,
        });
      }
    },
    [onConnected, translation]
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
      token: newToken.trim() || undefined,
    };
    try {
      await saveConnection(profile);
      setNewUrl("");
      setNewToken("");
      await refreshConnections();
    } finally {
      window.setTimeout(() => setSavingConnection(false), Math.max(0, 450 - (performance.now() - startedAt)));
    }
  }, [newUrl, newToken, refreshConnections]);

  const handleAddSshConnection = useCallback(async () => {
    const alias = sshAlias.trim();
    // The port is not optional: `frankd` binds an ephemeral one, so a tunnel with a guessed
    // number forwards to nothing.
    if (!alias || !sshRemotePort.trim()) return;
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
      token: sshToken.trim() || undefined,
      sshHostAlias: alias,
      sshHostName: discovered?.hostName,
      sshUser: sshUser.trim() || discovered?.user || undefined,
      sshPort: sshPort.trim() ? Number(sshPort.trim()) : discovered?.port ?? null,
      sshIdentityFile: sshIdentityFile.trim() || discovered?.identityFiles[0] || undefined,
      sshLocalPort: sshLocalPort.trim() ? Number(sshLocalPort.trim()) : null,
      sshRemotePort: Number(sshRemotePort.trim()),
      sshContext: sshContext.trim() || undefined,
    };
    try {
      await saveConnection(profile);
      setSshAlias("");
      setSshUser("");
      setSshPort("");
      setSshIdentityFile("");
      setSshLocalPort("");
      setSshRemotePort("");
      setSshToken("");
      setSshContext("");
      await refreshConnections();
    } finally {
      window.setTimeout(() => setSavingSshConnection(false), Math.max(0, 450 - (performance.now() - startedAt)));
    }
  }, [sshAlias, sshHosts, sshUser, sshPort, sshIdentityFile, sshLocalPort, sshRemotePort, sshToken, sshContext, refreshConnections]);

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
    || sshRemotePort.trim()
    || sshToken.trim()
    || newToken.trim()
  );

  useEffect(() => {
    onDirtyChange?.(draftDirty);
  }, [draftDirty, onDirtyChange]);

  return (
    <VStack gap={4} w="100%" minH={0} maxW={variant === "page" ? "680px" : undefined} px={variant === "page" ? 6 : 0} pb={variant === "dialog" ? 6 : 0}>
      {variant === "page" && (
        <VStack gap={3}>
          <Flex align="center" gap={2.5}>
            <FrankMark size="44px" style={{ flexShrink: 0 }} />
            <Text fontSize="4xl" fontWeight="bold" fontFamily="var(--font-display)" lineHeight="1" letterSpacing="tight">
              Frank
            </Text>
          </Flex>
          <Text fontSize="sm" color="fg.muted" textAlign="center">
            {translation("selectEnvironment")}
          </Text>
        </VStack>
      )}

      <VStack gap={4} w="100%" minH={0} align="stretch">
          {/* This machine is one of the places you can connect to, not a headline action, so it
              is a row in the same shape as a saved connection rather than a full-width button
              above the first heading — where it had no section to belong to and outweighed the
              choices it sits beside. */}
          <VStack gap={2} align="stretch">
            <SectionHeader mb={0} icon={<LuLaptop size={15} />} title={translation("thisMachine")} />
            <Flex
              align="center"
              {...CONNECTION_TILE}
              borderColor={localActive ? "green.emphasized" : "border"}
              pr={2.5}
            >
              <Box
                color={localActive ? "green.fg" : "fg.muted"}
                flexShrink={0}
                display="flex"
                alignItems="center"
              >
                <LuLaptop size={14} />
              </Box>
              <Box textAlign="left" pl={1.5} flex={1} minW={0}>
                <Text fontSize="sm" fontWeight="medium" truncate>
                  {LOCAL_CONNECTION_TARGET.name}
                </Text>
                <Text fontSize="xs" color="fg.muted" truncate>
                  {LOCAL_CONNECTION_TARGET.url}
                </Text>
              </Box>
              {localActive ? (
                <Flex align="center" gap={1} color="green.fg" px={1} flexShrink={0}>
                  <LuCheck size={12} />
                  <Text textStyle="fieldLabel">{translation("connected")}</Text>
                </Flex>
              ) : (
                <Button
                  variant="outline"
                  onClick={connectLocal}
                  disabled={connecting}
                  loading={connectingTarget === LOCAL_TARGET_ID}
                  loadingText={translation("lookingForLocalDaemon")}
                >
                  {failedTarget === LOCAL_TARGET_ID ? <LuRotateCcw size={12} /> : <LuPlug size={12} />}
                  {failedTarget === LOCAL_TARGET_ID ? translation("retry") : translation("connect")}
                </Button>
              )}
            </Flex>
          </VStack>

          <VStack gap={2} align="stretch">
            <SectionHeader mb={0} icon={<LuServer size={15} />} title={translation("savedConnections")} />
            {connections.length === 0 ? (
              <EmptyState.Root size="sm">
                <EmptyState.Content pt={2}>
                  <EmptyState.Indicator>
                    <LuServer />
                  </EmptyState.Indicator>
                  <VStack gap={0}>
                    <EmptyState.Title fontSize="sm">{translation("noSavedConnections")}</EmptyState.Title>
                    <EmptyState.Description fontSize="xs">
                      {translation("noSavedConnectionsHint")}
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
                    {...CONNECTION_TILE}
                    borderColor={active ? "green.emphasized" : "border"}
                    pr={2.5}
                  >
                    <Box
                      color={active ? "green.fg" : "fg.muted"}
                      flexShrink={0}
                      display="flex"
                      alignItems="center"
                    >
                      {profile.kind === "ssh" ? <LuNetwork size={14} /> : <LuServer size={14} />}
                    </Box>
                    <Box textAlign="left" pl={1.5} flex={1} minW={0}>
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
                        <Text textStyle="fieldLabel">{translation("connected")}</Text>
                      </Flex>
                    ) : (
                      <Button
                        variant="outline"
                        onClick={() => connectRemote(profile)}
                        disabled={connecting}
                        loading={connectingTarget === profile.id}
                        loadingText={translation("connectingTo", { name: profile.name })}
                      >
                        {failedTarget === profile.id ? <LuRotateCcw size={12} /> : <LuPlug size={12} />}
                        {failedTarget === profile.id ? translation("retry") : translation("connect")}
                      </Button>
                    )}
                    <Button
                      variant="ghost"
                      colorPalette="red"
                      onClick={() => handleDelete(profile.id)}
                      loading={deletingConnectionId === profile.id}
                      disabled={deletingConnectionId !== null}
                      aria-label={translation("deleteConnection", { url: profile.url })}
                    >
                      <LuTrash2 size={12} />
                    </Button>
                  </Flex>
                );
              })
            )}
          </VStack>

          <VStack gap={3} align="stretch">
            <SectionHeader mb={0} icon={<LuPlus size={15} />} title={translation("newRemoteConnection")} />
            <Flex gap={2.5}>
              <Button
                h="auto"
                {...CONNECTION_TILE}
                variant={addMode === "url" ? "subtle" : "outline"}
                colorPalette={addMode === "url" ? "blue" : "gray"}
                flex={1}
                justifyContent="flex-start"
                onClick={() => setAddMode("url")}
              >
                <LuServer size={14} />
                <Box textAlign="left" pl={1.5}>
                  <Text fontSize="sm" fontWeight="medium">{translation("serverUrl")}</Text>
                  <Text fontSize="xs" color="fg.muted">{translation("serverUrlSubtitle")}</Text>
                </Box>
              </Button>
              <Button
                h="auto"
                {...CONNECTION_TILE}
                variant={addMode === "ssh" ? "subtle" : "outline"}
                colorPalette={addMode === "ssh" ? "blue" : "gray"}
                flex={1}
                justifyContent="flex-start"
                onClick={() => setAddMode("ssh")}
              >
                <LuNetwork size={14} />
                <Box textAlign="left" pl={1.5}>
                  <Text fontSize="sm" fontWeight="medium">{translation("sshHost")}</Text>
                  <Text fontSize="xs" color="fg.muted">{translation("sshHostSubtitle")}</Text>
                </Box>
              </Button>
            </Flex>
            <Box minH={addMode === "ssh" ? "436px" : "266px"}>
              {addMode === "url" ? (
                <VStack align="stretch" gap={3}>
                  <Field.Root>
                    <Field.Label textStyle="fieldLabel">{translation("serverUrl")}</Field.Label>
                    <Input
                      bg="bg.subtle"
                      placeholder="http://localhost:8822"
                      value={newUrl}
                      onChange={(event) => setNewUrl(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter") void handleAddConnection();
                      }}
                    />
                    <Field.HelperText fontSize="xs">
                      {translation("serverUrlHelper")}
                    </Field.HelperText>
                  </Field.Root>
                  <Field.Root>
                    <Field.Label textStyle="fieldLabel">{translation("accessToken")}</Field.Label>
                    <Input
                      bg="bg.subtle"
                      type="password"
                      placeholder={translation("accessTokenPlaceholder")}
                      value={newToken}
                      onChange={(event) => setNewToken(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter") void handleAddConnection();
                      }}
                    />
                    <Field.HelperText fontSize="xs">
                      {translation("accessTokenHelper")}
                    </Field.HelperText>
                  </Field.Root>
                  <Flex justify="flex-end">
                    <Button
                      variant="subtle"
                      onClick={handleAddConnection}
                      loading={savingConnection}
                      disabled={!newUrl.trim() || savingConnection}
                    >
                      <LuPlus size={14} />
                      {translation("saveConnection")}
                    </Button>
                  </Flex>
                </VStack>
              ) : (
                <VStack align="stretch" gap={3}>
                  <Field.Root>
                    <Field.Label textStyle="fieldLabel">{translation("hostAlias")}</Field.Label>
                    {sshHosts.length > 0 && (
                      <Flex gap={1.5} wrap="wrap" mb={2}>
                        {sshHosts.slice(0, 8).map((host) => (
                          <Button
                            key={host.alias}
                            variant={sshAlias === host.alias ? "solid" : "outline"}
                            borderRadius="md"
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
                      <Input flex={1} minW={0} bg="bg.subtle" placeholder="biowulf, lab-gpu, coder.myworkspace" value={sshAlias} onChange={(event) => setSshAlias(event.target.value)} />
                      <Button variant="ghost" onClick={refreshSshHosts} loading={sshLoading} disabled={sshLoading} flexShrink={0}>
                        <LuRotateCcw size={12} />
                        {translation("reloadHosts")}
                      </Button>
                    </Flex>
                    <Field.HelperText fontSize="xs">
                      {translation("hostAliasHelper")}
                    </Field.HelperText>
                  </Field.Root>
                  <Flex gap={2} align="flex-start">
                    <Field.Root>
                      <Field.Label textStyle="fieldLabel">{translation("userOverride")}</Field.Label>
                      <Input bg="bg.subtle" placeholder={translation("userOverridePlaceholder")} value={sshUser} onChange={(event) => setSshUser(event.target.value)} />
                    </Field.Root>
                    <Field.Root maxW="132px">
                      <Field.Label textStyle="fieldLabel">{translation("sshPort")}</Field.Label>
                      <Input bg="bg.subtle" placeholder="22" value={sshPort} onChange={(event) => setSshPort(event.target.value.replace(/\D/g, ""))} />
                    </Field.Root>
                  </Flex>
                  <Field.Root>
                    <Field.Label textStyle="fieldLabel">{translation("identityFileOverride")}</Field.Label>
                    <Input bg="bg.subtle" placeholder={translation("identityFilePlaceholder")} value={sshIdentityFile} onChange={(event) => setSshIdentityFile(event.target.value)} />
                  </Field.Root>
                  <Flex gap={2} align="flex-start">
                    <Field.Root>
                      <Field.Label textStyle="fieldLabel">{translation("localTunnelPort")}</Field.Label>
                      <Input bg="bg.subtle" placeholder={translation("localTunnelPortPlaceholder")} value={sshLocalPort} onChange={(event) => setSshLocalPort(event.target.value.replace(/\D/g, ""))} />
                    </Field.Root>
                    <Field.Root>
                      <Field.Label textStyle="fieldLabel">{translation("serverPortOnHost")}</Field.Label>
                      <Input bg="bg.subtle" placeholder={translation("serverPortOnHostPlaceholder")} value={sshRemotePort} onChange={(event) => setSshRemotePort(event.target.value.replace(/\D/g, ""))} />
                    </Field.Root>
                  </Flex>
                  <Field.Root>
                    <Field.Label textStyle="fieldLabel">{translation("accessToken")}</Field.Label>
                    <Input bg="bg.subtle" type="password" placeholder={translation("accessTokenPlaceholder")} value={sshToken} onChange={(event) => setSshToken(event.target.value)} />
                    <Field.HelperText fontSize="xs">
                      {translation("accessTokenHelper")}
                    </Field.HelperText>
                  </Field.Root>
                  <Field.Root>
                    <Field.Label textStyle="fieldLabel">{translation("hostNotes")}</Field.Label>
                    <Textarea bg="bg.subtle" rows={3} placeholder={translation("hostNotesPlaceholder")} value={sshContext} onChange={(event) => setSshContext(event.target.value)} />
                  </Field.Root>
                  <Flex justify="flex-end">
                    <Button variant="subtle" onClick={handleAddSshConnection} loading={savingSshConnection} disabled={!sshAlias.trim() || !sshRemotePort.trim() || savingSshConnection}>
                      <LuPlus size={14} />
                      {translation("saveSshConnection")}
                    </Button>
                  </Flex>
                </VStack>
              )}
            </Box>
          </VStack>
        </VStack>
    </VStack>
  );
}
