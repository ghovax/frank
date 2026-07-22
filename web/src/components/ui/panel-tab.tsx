"use client";

import { Box, Button, IconButton, Text } from "@chakra-ui/react";
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
  mono = false,
}: {
  icon?: ReactNode;
  label: string;
  active: boolean;
  onSelect: () => void;
  onClose?: () => void;
  tooltip?: ReactNode;
  closeLabel?: string;
  maxLabelWidth?: string;
  // Render the label in the monospace font — for tabs that name a file/path.
  mono?: boolean;
}) {
  const tab = (
    <Box
      position="relative"
      h={PANEL_TAB_HEIGHT}
      flexShrink={0}
    >
      <Button
        variant="outline"
        h="full"
        gap={1.5}
        pl={2.5}
        pr={onClose ? 7 : 2.5}
        textStyle="fieldLabel"
        bg={active ? "bg.subtle" : "bg"}
        borderColor={active ? "border.emphasized" : "border"}
        color="fg"
        whiteSpace="nowrap"
        onClick={onSelect}
        _hover={{ bg: active ? "bg.muted" : "bg.subtle" }}
      >
        {icon}
        <Text truncate maxW={maxLabelWidth} fontFamily={mono ? "var(--app-font-mono)" : undefined}>{label}</Text>
      </Button>
      {onClose && (
        <IconButton
          aria-label={closeLabel ?? label}
          variant="ghost"
          size="2xs"
          position="absolute"
          right={1}
          top="50%"
          transform="translateY(-50%)"
          color="fg.subtle"
          _hover={{ bg: "bg.muted", color: "fg" }}
          onClick={(event) => { event.stopPropagation(); onClose(); }}
        >
          <LuX />
        </IconButton>
      )}
    </Box>
  );

  if (!tooltip) return tab;
  return (
    <Tooltip content={tooltip} contentProps={PANEL_TAB_TOOLTIP_PROPS} openDelay={300}>
      {tab}
    </Tooltip>
  );
}
