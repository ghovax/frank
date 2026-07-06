"use client";

// The live connection indicator + switcher shown in the chat input toolbar. It
// displays the current backend with a status dot, and its dropdown lists "this
// machine" plus every saved remote so the user can switch without leaving the app.
// It's self-contained: it reads the front-end-local store and drives the gate via
// window events, so it needs no props.

import { Box, Button, Flex, Menu, Portal, Text } from "@chakra-ui/react";
import { useCallback, useEffect, useState } from "react";
import { LuCheck, LuChevronDown, LuLaptop, LuServer, LuSettings2 } from "react-icons/lu";
import { useRouter } from "next/navigation";
import {
  activateConnectionTarget,
  checkConnection,
  getLastTargetId,
  listConnectionTargets,
  LOCAL_TARGET_ID,
  startLocalServer,
  waitForConnection,
  type ConnectionTarget,
} from "@/lib/connection";
import { isTauri } from "@/lib/connection-store";
import { toaster } from "@/components/ui/toaster";


export function ConnectionSwitcher({
  currentTargetId,
  onConnectionChange,
}: {
  currentTargetId?: string;
  onConnectionChange?: (target: ConnectionTarget) => void;
}) {
  const router = useRouter();
  const [targets, setTargets] = useState<ConnectionTarget[]>([]);
  const [currentTarget, setCurrentTarget] = useState<string>(LOCAL_TARGET_ID);
  const [switchingTarget, setSwitchingTarget] = useState<string | null>(null);

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
      ? "This machine"
      : targets.find((entry) => entry.id === currentTarget)?.name ?? "Connected";

  const switchTo = async (target: ConnectionTarget) => {
    const targetId = target.id;
    if (targetId === currentTarget) return;
    setSwitchingTarget(targetId);
    try {
      const url = target.kind === "local" ? await startLocalServer() : target.url;
      const ok = target.kind === "local" && isTauri()
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
    <Menu.Root
      size="sm"
      onOpenChange={(event) => {
        if (event.open) void load();
      }}
    >
      <Menu.Trigger asChild>
        <Button
          size="xs"
          variant="outline"
          borderRadius="sm"
          fontSize="xs"
          h="28px"
          px={2}
          gap={1.5}
          bg="bg"
          borderColor="border"
          flexShrink={0}
          title="Switch connection"
        >
          <Box boxSize="7px" borderRadius="full" bg="green.solid" flexShrink={0} />
          <Text truncate maxW="130px">
            {currentLabel}
          </Text>
          <LuChevronDown size={12} />
        </Button>
      </Menu.Trigger>
      <Portal>
        <Menu.Positioner>
          <Menu.Content borderRadius="sm" minW="220px">
            <ConnectionMenuItem
              value={LOCAL_TARGET_ID}
              active={currentTarget === LOCAL_TARGET_ID}
              icon={<LuLaptop size={13} />}
              label="This machine"
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
                icon={<LuServer size={13} />}
                label={profile.name}
                sub={profile.url}
                busy={switchingTarget === profile.id}
                onClick={() => void switchTo(profile)}
              />
            ))}
            <Menu.Separator />
            <Menu.Item
              value="__settings"
              onClick={() => router.push("/")}
            >
              <Flex align="center" gap={2}>
                <Box color="fg.muted">
                  <LuSettings2 size={13} />
                </Box>
                <Text fontSize="xs">Connection settings…</Text>
              </Flex>
            </Menu.Item>
          </Menu.Content>
        </Menu.Positioner>
      </Portal>
    </Menu.Root>
  );
}

function ConnectionMenuItem({
  value,
  active,
  icon,
  label,
  sub,
  busy = false,
  onClick,
}: {
  value: string;
  active: boolean;
  icon: React.ReactNode;
  label: string;
  sub?: string;
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
          <Text fontSize="xs" fontWeight="medium" truncate>
            {label}
          </Text>
          {sub && (
            <Text fontSize="2xs" color="fg.muted" truncate>
              {sub}
            </Text>
          )}
        </Box>
        {active && !busy && (
          <Box color="green.fg" flexShrink={0}>
            <LuCheck size={13} />
          </Box>
        )}
        {busy && (
          <Text fontSize="2xs" color="fg.muted" flexShrink={0}>
            Connecting
          </Text>
        )}
      </Flex>
    </Menu.Item>
  );
}
