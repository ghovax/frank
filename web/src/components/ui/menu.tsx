"use client";

// Dropdown menu helpers that fold away the repeated Chakra Menu boilerplate. Every menu in
// the app is a trigger plus a portalled, positioned Content of items; `DropdownMenu` owns
// that wrapper so call sites pass only the trigger and the items, and `MenuOption` owns the
// standard item shape (optional leading icon, label, optional trailing check, danger style).

import { Box, Menu, Portal } from "@chakra-ui/react";
import type { ComponentProps, ReactNode } from "react";
import { LuCheck } from "react-icons/lu";

export function DropdownMenu({
  trigger,
  children,
  minW,
  positioning,
}: {
  trigger: ReactNode;
  children: ReactNode;
  minW?: string;
  positioning?: ComponentProps<typeof Menu.Root>["positioning"];
}) {
  return (
    <Menu.Root positioning={positioning}>
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
  children: ReactNode;
}) {
  return (
    <Menu.Item
      value={value}
      fontSize="xs"
      onClick={onClick}
      {...(danger ? { color: "red.fg", _hover: { bg: "red.subtle" } } : {})}
    >
      {icon}
      <Box flex={1}>{children}</Box>
      {selected ? <LuCheck size={14} /> : null}
    </Menu.Item>
  );
}
