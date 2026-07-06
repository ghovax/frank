"use client";

// The live connection indicator + switcher shown in the chat input toolbar. It
// displays the current backend with a status dot, and its dropdown lists "this
// machine" plus every saved remote so the user can switch without leaving the app.
// It's self-contained: it reads the front-end-local store and drives the gate via
// window events, so it needs no props.

import { Box, Button, Flex, Menu, Portal, Text } from "@chakra-ui/react";
import { useCallback, useEffect, useState } from "react";
import { LuCheck, LuChevronDown, LuLaptop, LuServer, LuSettings2 } from "react-icons/lu";
import {
  activateConnectionTarget,
  checkConnection,
  getLastTargetId,
  listConnectionTargets,
  LOCAL_TARGET_ID,
  resolveReachableConnectionUrl,
  waitForConnection,
  type ConnectionTarget,
} from "@/lib/connection";
import { isTauri } from "@/lib/connection-store";
import { ConnectionSettingsDialog } from "@/components/connection-settings";
import { toaster } from "@/components/ui/toaster";


export function ConnectionSwitcher({
  currentTargetId,
  onConnectionChange,
  size = "xs",
}: {
  currentTargetId?: string;
  onConnectionChange?: (target: ConnectionTarget) => void;
  // "xs" is the compact composer-toolbar style; "sm"/"md" are the larger welcome-screen
  // sizes that match the model picker's scale so the switcher isn't a small outlier.
  size?: "xs" | "sm" | "md";
}) {
  const [targets, setTargets] = useState<ConnectionTarget[]>([]);
  const [currentTarget, setCurrentTarget] = useState<string>(LOCAL_TARGET_ID);
  const [switchingTarget, setSwitchingTarget] = useState<string | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);

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
      ? "Local"
      : targets.find((entry) => entry.id === currentTarget)?.name ?? "Connected";

  // The compact "xs" toolbar variant needs explicit overrides to fit the 28px composer
  // row. The larger welcome-screen variants ("sm"/"md") inherit Chakra's native size
  // metrics, so their height and icon-to-text gap match the sibling action buttons and
  // the model picker. All sizes lead with a connection-kind icon scaled to fit.
  const trigger =
    size === "md"
      ? { size: "md" as const, height: undefined, borderRadius: "md" as const, fontSize: undefined, paddingX: undefined, gap: undefined, icon: 17, chevron: 16, labelMaxWidth: "240px" }
      : size === "sm"
        ? { size: "sm" as const, height: undefined, borderRadius: "md" as const, fontSize: undefined, paddingX: undefined, gap: undefined, icon: 15, chevron: 14, labelMaxWidth: "200px" }
        : { size: "xs" as const, height: "28px", borderRadius: "sm" as const, fontSize: "xs", paddingX: 2, gap: 1.5, icon: 13, chevron: 12, labelMaxWidth: "130px" };
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
          title: `Couldn't reach ${target.name}`,
          description: `No response from ${url}.`,
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
        title: `Couldn't reach ${target.name}`,
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
          bg="bg"
          borderColor="border"
          flexShrink={0}
          title="Switch connection"
        >
          {/* A real connection-kind icon (laptop for the local server, server for a
              remote), tinted green to signal a live connection — consistent across the
              compact composer toolbar and the larger welcome screen. */}
          <Box color="green.fg" display="flex" alignItems="center" flexShrink={0}>
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
          <Menu.Content borderRadius={large ? "md" : "sm"} minW={large ? "300px" : "220px"}>
            <ConnectionMenuItem
              value={LOCAL_TARGET_ID}
              active={currentTarget === LOCAL_TARGET_ID}
              icon={<LuLaptop size={large ? 16 : 13} />}
              label="Local"
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
                sub={profile.kind === "ssh" ? profile.sshHostAlias ?? profile.url : profile.url}
                large={large}
                busy={switchingTarget === profile.id}
                onClick={() => void switchTo(profile)}
              />
            ))}
            <Menu.Separator />
            <Menu.Item
              value="__settings"
              color="blue.fg"
              _hover={{ bg: "blue.subtle" }}
              onClick={() => setSettingsOpen(true)}
            >
              <Flex align="center" gap={2} color="blue.fg">
                <LuSettings2 size={large ? 16 : 13} />
                <Text fontSize={large ? "sm" : "xs"} fontWeight="medium">Connection settings…</Text>
              </Flex>
            </Menu.Item>
          </Menu.Content>
        </Menu.Positioner>
      </Portal>
    </Menu.Root>
    <ConnectionSettingsDialog
      open={settingsOpen}
      onOpenChange={setSettingsOpen}
      currentTargetId={currentTarget}
      onConnected={(target) => {
        // ConnectionSettings already health-checked and activated the backend; mirror
        // switchTo's tail — update the pill and switch the live session, then close.
        setCurrentTarget(target.id);
        onConnectionChange?.(target);
        setSettingsOpen(false);
      }}
    />
    </>
  );
}

function ConnectionMenuItem({
  value,
  active,
  icon,
  label,
  sub,
  large = false,
  busy = false,
  onClick,
}: {
  value: string;
  active: boolean;
  icon: React.ReactNode;
  label: string;
  sub?: string;
  large?: boolean;
  busy?: boolean;
  onClick: () => void;
}) {
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
          {sub && (
            <Text fontSize={large ? "xs" : "2xs"} color="fg.muted" truncate>
              {sub}
            </Text>
          )}
        </Box>
        {active && !busy && (
          <Box color="green.fg" flexShrink={0}>
            <LuCheck size={large ? 16 : 13} />
          </Box>
        )}
        {busy && (
          <Text fontSize={large ? "xs" : "2xs"} color="fg.muted" flexShrink={0}>
            Connecting
          </Text>
        )}
      </Flex>
    </Menu.Item>
  );
}
