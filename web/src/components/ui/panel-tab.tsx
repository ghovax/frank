"use client";

import { Box, Flex, Text } from "@chakra-ui/react";
import type { ReactNode } from "react";
import { LuX } from "react-icons/lu";
import { Tooltip } from "./tooltip";

// The shared height for a panel's top strip — its tabs and any controls sitting beside
// them (the "＋" button, an environment switcher) all use this so the whole strip lines
// up at one height instead of each control picking its own.
export const PANEL_TAB_HEIGHT = "32px";

// The card styling for a panel tab's rich hover tooltip — matched to the context/token
// counter tooltip so every panel's tab tooltips read identically.
const PANEL_TAB_TOOLTIP_PROPS = { p: 3, bg: "bg", color: "fg", borderRadius: "md", boxShadow: "lg", border: "1px solid", borderColor: "border" } as const;

// A single selectable tab in a panel's top strip. One implementation shared by the
// Artifacts panel and the terminal panel so they look and behave identically and can never
// drift apart in height or styling. Pass `tooltip` for a rich hover card (built by the
// caller, styled here) — the same affordance every panel's tabs get for free.
export function PanelTab({
  icon,
  label,
  active,
  onSelect,
  onClose,
  tooltip,
  closeLabel,
  maxLabelWidth = "130px",
}: {
  icon?: ReactNode;
  label: string;
  active: boolean;
  onSelect: () => void;
  onClose?: () => void;
  tooltip?: ReactNode;
  closeLabel?: string;
  maxLabelWidth?: string;
}) {
  const tab = (
    <Flex
      as="button"
      align="center"
      gap={1.5}
      pl={2.5}
      pr={onClose ? 1.5 : 2.5}
      h={PANEL_TAB_HEIGHT}
      fontSize="xs"
      fontWeight="medium"
      borderRadius="md"
      bg={active ? "bg.subtle" : "bg"}
      border="1px solid"
      borderColor={active ? "border.emphasized" : "border"}
      color="fg"
      cursor="pointer"
      flexShrink={0}
      whiteSpace="nowrap"
      onClick={onSelect}
      _hover={{ bg: active ? "bg.muted" : "bg.subtle" }}
    >
      {icon}
      <Text truncate maxW={maxLabelWidth}>{label}</Text>
      {onClose && (
        <Box
          as="span"
          display="inline-flex"
          alignItems="center"
          justifyContent="center"
          borderRadius="sm"
          w={4.5}
          h={4.5}
          flexShrink={0}
          color="fg.subtle"
          _hover={{ bg: "bg.muted", color: "fg" }}
          onClick={(event) => { event.stopPropagation(); onClose(); }}
          aria-label={closeLabel}
        >
          <LuX size={12} />
        </Box>
      )}
    </Flex>
  );

  if (!tooltip) return tab;
  return (
    <Tooltip content={tooltip} contentProps={PANEL_TAB_TOOLTIP_PROPS} openDelay={300}>
      {tab}
    </Tooltip>
  );
}
