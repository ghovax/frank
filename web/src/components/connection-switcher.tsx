"use client";

// The live connection indicator + switcher shown in the chat input toolbar. It
// displays the current backend with a status dot, and its dropdown lists "this
// machine" plus every saved remote so the user can switch without leaving the app.
// It reads the front-end-local store directly, while the parent owns the shared
// Settings dialog opened from the "Connection settings..." menu item.

import { Box, Button, Flex, Text } from "@chakra-ui/react";
import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { LuChevronDown, LuLaptop, LuServer, LuSettings2 } from "react-icons/lu";
import { DropdownMenu, MenuOption, MenuSeparator } from "@/components/ui/menu";
import {
  activateConnectionTarget,
  checkConnection,
  getApiBase,
  getLastTargetId,
  listConnectionTargets,
  LOCAL_TARGET_ID,
  resolveReachableConnectionUrl,
  waitForConnection,
  type ConnectionTarget,
} from "@/lib/connection";
import { isTauri } from "@/lib/connection-store";
import { toaster } from "@/components/ui/toaster";

type ConnectionStatus = "checking" | "online" | "offline";

function statusAppearance(status: ConnectionStatus): { color: string; bg: string } {
  if (status === "online") return { color: "green.fg", bg: "green.solid" };
  if (status === "offline") return { color: "red.fg", bg: "red.solid" };
  return { color: "yellow.fg", bg: "yellow.solid" };
}

function StatusField({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <Flex align="baseline" gap={2}>
      <Text fontSize="2xs" color="fg.subtle" minW={11} flexShrink={0}>
        {label}
      </Text>
      <Text fontSize="2xs" fontFamily={mono ? "var(--app-font-mono)" : undefined} color="fg.muted" truncate>
        {value}
      </Text>
    </Flex>
  );
}

export function ConnectionSwitcher({
  currentTargetId,
  onConnectionChange,
  onOpenConnectionSettings,
}: {
  currentTargetId?: string;
  onConnectionChange?: (target: ConnectionTarget) => void;
  onOpenConnectionSettings: () => void;
}) {
  const translation = useTranslations("ConnectionSwitcher");
  const [targets, setTargets] = useState<ConnectionTarget[]>([]);
  const [currentTarget, setCurrentTarget] = useState<string>(LOCAL_TARGET_ID);
  const [switchingTarget, setSwitchingTarget] = useState<string | null>(null);
  const [status, setStatus] = useState<ConnectionStatus>("checking");

  const load = useCallback(async () => {
    const [savedTargets, last] = await Promise.all([
      listConnectionTargets().catch(() => [] as ConnectionTarget[]),
      getLastTargetId().catch(() => null),
    ]);
    setTargets(savedTargets);
    setCurrentTarget(currentTargetId ?? last ?? LOCAL_TARGET_ID);
  }, [currentTargetId]);

  useEffect(() => {
    let cancelled = false;
    async function refreshStatus() {
      setStatus("checking");
      const ok = await checkConnection(getApiBase(), { timeoutMs: 1800 });
      if (!cancelled) setStatus(ok ? "online" : "offline");
    }
    void refreshStatus();
    const interval = window.setInterval(refreshStatus, 15000);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [currentTarget]);

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      listConnectionTargets().catch(() => [] as ConnectionTarget[]),
      getLastTargetId().catch(() => null),
    ]).then(([savedTargets, last]) => {
      if (cancelled) return;
      setTargets(savedTargets);
      setCurrentTarget(currentTargetId ?? last ?? LOCAL_TARGET_ID);
    });
    return () => { cancelled = true; };
  }, [currentTargetId]);

  const currentLabel =
    currentTarget === LOCAL_TARGET_ID
      ? translation("localServer")
      : targets.find((entry) => entry.id === currentTarget)?.name ?? translation("connected");
  const currentTargetRecord = targets.find((entry) => entry.id === currentTarget);
  const activeStatus = switchingTarget ? "checking" : status;
  const activeStatusAppearance = statusAppearance(activeStatus);
  const activeStatusLabel = translation(`status.${activeStatus}`);
  const activeKind = currentTargetRecord?.kind ?? (currentTarget === LOCAL_TARGET_ID ? "local" : "remote");
  const activeUrl = getApiBase();

  const isLocal = currentTarget === LOCAL_TARGET_ID;
  const remoteTargets = targets.filter((target) => target.id !== LOCAL_TARGET_ID);
  const localTarget = targets.find((entry) => entry.id === LOCAL_TARGET_ID);

  const switchTo = async (target: ConnectionTarget) => {
    const targetId = target.id;
    if (targetId === currentTarget) return;
    setSwitchingTarget(targetId);
    try {
      const url = await resolveReachableConnectionUrl(target);
      const token = target.kind === "local" ? undefined : target.token ?? "";
      const ok = (target.kind === "local" || target.kind === "ssh") && isTauri()
        ? await waitForConnection(url, { token })
        : await checkConnection(url, { token });
      if (!ok) {
        toaster.create({
          type: "error",
          title: translation("couldNotReach", { name: target.name }),
          description: translation("noResponse", { url }),
          closable: true,
        });
        return;
      }
      const activated = { ...target, url };
      await activateConnectionTarget(activated);
      setCurrentTarget(targetId);
      onConnectionChange?.(activated);
    } catch (caught) {
      toaster.create({
        type: "error",
        title: translation("couldNotReach", { name: target.name }),
        description: caught instanceof Error ? caught.message : String(caught),
        closable: true,
      });
    } finally {
      setSwitchingTarget(null);
    }
  };

  return (
    <DropdownMenu
      onOpenChange={(event) => {
        if (event.open) void load();
      }}
      trigger={
        <Button
          variant="outline"
          px={2}
          gap={1.5}
          bg={isLocal ? "bg.subtle" : "bg"}
          borderColor="border"
          _hover={{ bg: "bg.muted" }}
          flexShrink={0}
          title={translation("switchConnection")}
        >
          <Box
            boxSize="2"
            borderRadius="full"
            bg={activeStatusAppearance.bg}
            boxShadow="0 0 0 2px var(--chakra-colors-bg)"
            flexShrink={0}
            title={`${activeStatusLabel}: ${activeUrl}`}
          />
          {/* A real connection-kind icon (laptop for the local server, server for a
              remote), tinted green to signal a live connection. */}
          <Box color={isLocal ? "fg.muted" : activeStatusAppearance.color} display="flex" alignItems="center" flexShrink={0}>
            {isLocal ? <LuLaptop size={13} /> : <LuServer size={13} />}
          </Box>
          <Text truncate maxW="130px">
            {currentLabel}
          </Text>
          <LuChevronDown size={12} />
        </Button>
      }
    >
      <Box px={2} pt={2.5} pb={2}>
        <Flex align="center" gap={2}>
          <Box boxSize="2" borderRadius="full" bg={activeStatusAppearance.bg} flexShrink={0} />
          <Text fontSize="xs" fontWeight="semibold">
            {activeStatusLabel}
          </Text>
        </Flex>
        <Flex direction="column" gap={1} mt={1.5}>
          <StatusField label={translation("type")} value={activeKind === "ssh" ? translation("sshTunnel") : activeKind === "local" ? translation("localServer") : translation("remoteServer")} />
          <StatusField label={translation("url")} value={activeUrl} mono />
        </Flex>
      </Box>
      <MenuSeparator />
      <MenuOption
        value={LOCAL_TARGET_ID}
        icon={<Box color="fg.muted" flexShrink={0}><LuLaptop size={13} /></Box>}
        selected={currentTarget === LOCAL_TARGET_ID}
        busy={switchingTarget === LOCAL_TARGET_ID}
        busyLabel={translation("connecting")}
        onClick={() => {
          if (localTarget) void switchTo(localTarget);
        }}
      >
        <Text fontWeight="medium" truncate>{translation("localServer")}</Text>
      </MenuOption>
      {remoteTargets.map((profile) => (
        <MenuOption
          key={profile.id}
          value={profile.id}
          icon={<Box color="fg.muted" flexShrink={0}><LuServer size={13} /></Box>}
          selected={currentTarget === profile.id}
          subtitle={profile.kind === "ssh" ? profile.sshHostAlias ?? profile.url : profile.url}
          busy={switchingTarget === profile.id}
          busyLabel={translation("connecting")}
          onClick={() => void switchTo(profile)}
        >
          <Text fontWeight="medium" truncate>{profile.name}</Text>
        </MenuOption>
      ))}
      <MenuSeparator />
      <MenuOption
        value="__settings"
        accent
        icon={<LuSettings2 size={13} />}
        onClick={onOpenConnectionSettings}
      >
        <Text fontWeight="medium">{translation("openConnectionSettings")}</Text>
      </MenuOption>
    </DropdownMenu>
  );
}
