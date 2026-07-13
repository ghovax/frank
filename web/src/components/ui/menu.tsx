"use client";

// Dropdown menu helpers that fold away the repeated Chakra Menu boilerplate. Every menu in
// the app is a trigger plus a portalled, positioned Content of items; `DropdownMenu` owns
// that wrapper so call sites pass only the trigger and the items, and `MenuOption` owns the
// standard item shape (optional leading icon, label, optional subtitle, optional trailing
// check or busy spinner, danger/accent style).

import { Box, Flex, Menu, Portal, Spinner } from "@chakra-ui/react";
import type { ComponentProps, ReactNode } from "react";
import { LuCheck } from "react-icons/lu";

// One minimum width for every dropdown, so a project switcher and a connection switcher open
// to the same comfortable width instead of each picking its own. A spacing-scale token
// (14rem / 224px) rather than a raw pixel string.
const DROPDOWN_MIN_W = "56";

export function DropdownMenu({
  trigger,
  children,
  minW = DROPDOWN_MIN_W,
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
    // `size="sm"` is the app's one menu scale — set here so every dropdown's item height and
    // font match instead of defaulting to Chakra's larger `md`.
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

// A divider between item groups, with vertical margin that matches the items' horizontal
// inset — so it sits evenly within the menu's padding instead of crowding the rows around
// it. Lives here (not per call site) so every menu's divider breathes the same way.
export function MenuSeparator() {
  return <Menu.Separator my={1.5} />;
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
