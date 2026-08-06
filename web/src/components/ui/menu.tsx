"use client";

// Dropdown helpers that fold away the repeated menu boilerplate.

import { Box, Flex, Menu, Portal, Spinner } from "@chakra-ui/react";
import type { ComponentProps, ReactNode } from "react";
import { LuCheck } from "react-icons/lu";

// One minimum width for every dropdown, from the spacing scale rather than a bare number.
const DROPDOWN_MINIMUM_W = "56";

export function DropdownMenu({
  trigger,
  children,
  minW = DROPDOWN_MINIMUM_W,
  positioning,
  onOpenChange,
}: {
  trigger: ReactNode;
  children: ReactNode;
  minW?: string;
  positioning?: ComponentProps<typeof Menu.Root>["positioning"];
  onOpenChange?: ComponentProps<typeof Menu.Root>["onOpenChange"];
}) {
  return (
    // The app's one menu scale, set here so every dropdown's item height and font match.
    <Menu.Root size="sm" positioning={positioning} onOpenChange={onOpenChange}>
      <Menu.Trigger asChild>{trigger}</Menu.Trigger>
      <Portal>
        <Menu.Positioner>
          <Menu.Content minW={minW}>{children}</Menu.Content>
        </Menu.Positioner>
      </Portal>
    </Menu.Root>
  );
}

// A divider between item groups, with margin matching the items' inset so it sits evenly.
export function MenuSeparator() {
  return <Menu.Separator />;
}

export function MenuOption({
  value,
  onClick,
  icon,
  selected,
  danger,
  accent,
  subtitle,
  busy,
  busyLabel,
  children,
}: {
  value: string;
  onClick?: () => void;
  // Leading icon (menus that lead with an icon).
  icon?: ReactNode;
  // Show a trailing check (menus where an item is the current selection).
  selected?: boolean;
  // Destructive action — styled red.
  danger?: boolean;
  // Affirmative accent — styled blue (e.g. an "open settings" item).
  accent?: boolean;
  // Secondary line under the label.
  subtitle?: ReactNode;
  // Show a trailing spinner (with an optional label) instead of the check.
  busy?: boolean;
  busyLabel?: ReactNode;
  children: ReactNode;
}) {
  const tone = danger
    ? { color: "red.fg", _hover: { bg: "red.subtle" } }
    : accent
      ? { color: "blue.fg", _hover: { bg: "blue.subtle" } }
      : {};
  return (
    <Menu.Item value={value} onClick={onClick} {...tone}>
      {icon}
      <Box flex={1} minW={0}>
        {children}
        {subtitle ? (
          <Box fontSize="2xs" color="fg.muted" truncate>
            {subtitle}
          </Box>
        ) : null}
      </Box>
      {busy ? (
        <Flex align="center" gap={1} fontSize="2xs" color="fg.muted" flexShrink={0}>
          <Spinner size="xs" borderWidth="1px" />
          {busyLabel}
        </Flex>
      ) : selected ? (
        <LuCheck size={14} />
      ) : null}
    </Menu.Item>
  );
}
