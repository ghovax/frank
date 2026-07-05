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
import { getLastTargetId, LOCAL_TARGET_ID } from "@/lib/connection";
import { listConnections, type ConnectionProfile } from "@/lib/connection-store";


export function ConnectionSwitcher() {
  const router = useRouter();
  const [connections, setConnections] = useState<ConnectionProfile[]>([]);
  const [currentTarget, setCurrentTarget] = useState<string>(LOCAL_TARGET_ID);

  const load = useCallback(async () => {
    const [saved, last] = await Promise.all([
      listConnections().catch(() => [] as ConnectionProfile[]),
      getLastTargetId().catch(() => null),
    ]);
    setConnections(saved);
    setCurrentTarget(last ?? LOCAL_TARGET_ID);
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const currentLabel =
    currentTarget === LOCAL_TARGET_ID
      ? "This machine"
      : connections.find((entry) => entry.id === currentTarget)?.name ?? "Connected";

  const switchTo = (targetId: string) => {
    if (targetId === currentTarget) return;
    router.push("/home");
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
              onClick={() => switchTo(LOCAL_TARGET_ID)}
            />
            {connections.map((profile) => (
              <ConnectionMenuItem
                key={profile.id}
                value={profile.id}
                active={currentTarget === profile.id}
                icon={<LuServer size={13} />}
                label={profile.name}
                sub={profile.url}
                onClick={() => switchTo(profile.id)}
              />
            ))}
            <Menu.Separator />
            <Menu.Item
              value="__settings"
              onClick={() => router.push("/home")}
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
  onClick,
}: {
  value: string;
  active: boolean;
  icon: React.ReactNode;
  label: string;
  sub?: string;
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
        {active && (
          <Box color="green.fg" flexShrink={0}>
            <LuCheck size={13} />
          </Box>
        )}
      </Flex>
    </Menu.Item>
  );
}
