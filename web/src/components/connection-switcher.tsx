"use client";

// The live connection indicator + switcher shown in the chat input toolbar. It
// displays the current backend with a status dot, and its dropdown lists "this
// machine" plus every saved remote so the user can switch without leaving the app.
// It reads the front-end-local store directly, while the parent owns the shared
// Settings dialog opened from the "Connection settings..." menu item.

import { Box, Button, Flex, Menu, Portal, Spinner, Text } from "@chakra-ui/react";
import { useCallback, useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { LuCheck, LuChevronDown, LuLaptop, LuServer, LuSettings2 } from "react-icons/lu";
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

function StatusField({ label, value, large, mono = false }: { label: string; value: string; large: boolean; mono?: boolean }) {
  return (
    <Flex align="baseline" gap={2}>
      <Text fontSize={large ? "xs" : "2xs"} color="fg.subtle" minW={11} flexShrink={0}>
        {label}
      </Text>
      <Text fontSize={large ? "xs" : "2xs"} fontFamily={mono ? "var(--app-font-mono)" : undefined} color="fg.muted" truncate>
        {value}
      </Text>
    </Flex>
  );
}

export function ConnectionSwitcher({
  currentTargetId,
  onConnectionChange,
  onOpenConnectionSettings,
  size = "xs",
}: {
  currentTargetId?: string;
  onConnectionChange?: (target: ConnectionTarget) => void;
  onOpenConnectionSettings: () => void;
  // "xs" is the compact composer-toolbar style; "sm"/"md" are the larger welcome-screen
  // sizes that match the model picker's scale so the switcher isn't a small outlier.
  size?: "xs" | "sm" | "md";
}) {
  const t = useTranslations("ConnectionSwitcher");
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
      const ok = await checkConnection(getApiBase(), 1800);
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
      ? t("localServer")
      : targets.find((entry) => entry.id === currentTarget)?.name ?? t("connected");
  const currentTargetRecord = targets.find((entry) => entry.id === currentTarget);
  const activeStatus = switchingTarget ? "checking" : status;
  const activeStatusAppearance = statusAppearance(activeStatus);
  const activeStatusLabel = t(`status.${activeStatus}`);
  const activeKind = currentTargetRecord?.kind ?? (currentTarget === LOCAL_TARGET_ID ? "local" : "remote");
  const activeUrl = getApiBase();

  // The compact "xs" toolbar variant needs explicit overrides to fit the 28px composer
  // row. The larger welcome-screen variants ("sm"/"md") inherit Chakra's native size
  // metrics, so their height and icon-to-text gap match the sibling action buttons and
  // the model picker. All sizes lead with a connection-kind icon scaled to fit.
  const trigger =
    size === "md"
      ? { size: "md" as const, height: undefined, borderRadius: "md" as const, fontSize: undefined, paddingX: undefined, gap: undefined, icon: 17, chevron: 16, labelMaxWidth: "240px" }
      : size === "sm"
        ? { size: "sm" as const, height: undefined, borderRadius: "md" as const, fontSize: undefined, paddingX: undefined, gap: undefined, icon: 15, chevron: 14, labelMaxWidth: "200px" }
        : { size: "xs" as const, height: "28px", borderRadius: "md" as const, fontSize: "xs", paddingX: 2, gap: 1.5, icon: 13, chevron: 12, labelMaxWidth: "130px" };
  const isLocal = currentTarget === LOCAL_TARGET_ID;
  // The larger welcome-screen variants also open a larger, more comfortable menu
  // (bigger items and font) so the dropdown matches the model picker's scale rather
  // than reading as a cramped toolbar popover.
  const large = size !== "xs";

  const switchTo = async (target: ConnectionTarget) => {
    const targetId = target.id;
    if (targetId === currentTarget) return;
    setSwitchingTarget(targetId);
    try {
      const url = await resolveReachableConnectionUrl(target);
      const ok = (target.kind === "local" || target.kind === "ssh") && isTauri()
        ? await waitForConnection(url)
        : await checkConnection(url);
      if (!ok) {
        toaster.create({
          type: "error",
          title: t("couldNotReach", { name: target.name }),
          description: t("noResponse", { url }),
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
        title: t("couldNotReach", { name: target.name }),
        description: caught instanceof Error ? caught.message : String(caught),
        closable: true,
      });
    } finally {
      setSwitchingTarget(null);
    }
  };

  return (
    <>
    <Menu.Root
      size={large ? "md" : "sm"}
      onOpenChange={(event) => {
        if (event.open) void load();
      }}
    >
      <Menu.Trigger asChild>
        <Button
          size={trigger.size}
          variant="outline"
          borderRadius={trigger.borderRadius}
          fontSize={trigger.fontSize}
          h={trigger.height}
          px={trigger.paddingX}
          gap={trigger.gap}
          bg={isLocal ? "bg.subtle" : "bg"}
          borderColor="border"
          _hover={{ bg: "bg.muted" }}
          flexShrink={0}
          title={t("switchConnection")}
        >
          <Box
            w={large ? "9px" : "7px"}
            h={large ? "9px" : "7px"}
            borderRadius="full"
            bg={activeStatusAppearance.bg}
            boxShadow="0 0 0 2px var(--chakra-colors-bg)"
            flexShrink={0}
            title={`${activeStatusLabel}: ${activeUrl}`}
          />
          {/* A real connection-kind icon (laptop for the local server, server for a
              remote), tinted green to signal a live connection — consistent across the
              compact composer toolbar and the larger welcome screen. */}
          <Box color={isLocal ? "fg.muted" : activeStatusAppearance.color} display="flex" alignItems="center" flexShrink={0}>
            {isLocal ? <LuLaptop size={trigger.icon} /> : <LuServer size={trigger.icon} />}
          </Box>
          <Text truncate maxW={trigger.labelMaxWidth}>
            {currentLabel}
          </Text>
          <LuChevronDown size={trigger.chevron} />
        </Button>
      </Menu.Trigger>
      <Portal>
        <Menu.Positioner>
          <Menu.Content minW={large ? 75 : 55}>
            <Box px={2} pt={2.5} pb={2}>
              <Flex align="center" gap={2}>
                <Box w={2} h={2} borderRadius="full" bg={activeStatusAppearance.bg} flexShrink={0} />
                <Text fontSize={large ? "sm" : "xs"} fontWeight="semibold">
                  {activeStatusLabel}
                </Text>
              </Flex>
              <Flex direction="column" gap={0.5} mt={1.5}>
                <StatusField label={t("type")} value={activeKind === "ssh" ? t("sshTunnel") : activeKind === "local" ? t("localServer") : t("remoteServer")} large={large} />
                <StatusField label={t("url")} value={activeUrl} large={large} mono />
              </Flex>
            </Box>
            <Menu.Separator my={large ? 1.5 : 1} />
            <ConnectionMenuItem
              value={LOCAL_TARGET_ID}
              active={currentTarget === LOCAL_TARGET_ID}
              icon={<LuLaptop size={large ? 16 : 13} />}
              label={t("localServer")}
              large={large}
              busy={switchingTarget === LOCAL_TARGET_ID}
              onClick={() => {
                const local = targets.find((entry) => entry.id === LOCAL_TARGET_ID);
                if (local) void switchTo(local);
              }}
            />
            {targets.filter((target) => target.id !== LOCAL_TARGET_ID).map((profile) => (
              <ConnectionMenuItem
                key={profile.id}
                value={profile.id}
                active={currentTarget === profile.id}
                icon={<LuServer size={large ? 16 : 13} />}
                label={profile.name}
                subtitle={profile.kind === "ssh" ? profile.sshHostAlias ?? profile.url : profile.url}
                large={large}
                busy={switchingTarget === profile.id}
                onClick={() => void switchTo(profile)}
              />
            ))}
            <Menu.Separator my={large ? 1.5 : 1} />
            <Menu.Item
              value="__settings"
              color="blue.fg"
              _hover={{ bg: "blue.subtle" }}
              onClick={onOpenConnectionSettings}
            >
              <Flex align="center" gap={2} color="blue.fg">
                <LuSettings2 size={large ? 16 : 13} />
                <Text fontSize={large ? "sm" : "xs"} fontWeight="medium">{t("openConnectionSettings")}</Text>
              </Flex>
            </Menu.Item>
          </Menu.Content>
        </Menu.Positioner>
      </Portal>
    </Menu.Root>
    </>
  );
}

function ConnectionMenuItem({
  value,
  active,
  icon,
  label,
  subtitle,
  large = false,
  busy = false,
  onClick,
}: {
  value: string;
  active: boolean;
  icon: React.ReactNode;
  label: string;
  subtitle?: string;
  large?: boolean;
  busy?: boolean;
  onClick: () => void;
}) {
  const t = useTranslations("ConnectionSwitcher");
  return (
    <Menu.Item value={value} onClick={onClick}>
      <Flex align="center" gap={2} flex={1} minW={0}>
        <Box color="fg.muted" flexShrink={0}>
          {icon}
        </Box>
        <Box flex={1} minW={0}>
          <Text fontSize={large ? "sm" : "xs"} fontWeight="medium" truncate>
            {label}
          </Text>
          {subtitle && (
            <Text fontSize={large ? "xs" : "2xs"} color="fg.muted" truncate>
              {subtitle}
            </Text>
          )}
        </Box>
        {active && !busy && (
          <Box color="green.fg" flexShrink={0}>
            <LuCheck size={large ? 16 : 13} />
          </Box>
        )}
        {busy && (
          <Flex align="center" gap={1} fontSize={large ? "xs" : "2xs"} color="fg.muted" flexShrink={0}>
            <Spinner size="xs" borderWidth="1px" />
            {t("connecting")}
          </Flex>
        )}
      </Flex>
    </Menu.Item>
  );
}
