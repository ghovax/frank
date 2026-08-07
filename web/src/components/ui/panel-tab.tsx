"use client";

import { Box, Button, IconButton, Text } from "@chakra-ui/react";
import type { ReactNode } from "react";
import { LuX } from "react-icons/lu";
import { Tooltip } from "./tooltip";

// The shared height for a panel's top strip, so tabs and the controls beside them line up.
export const PANEL_TAB_HEIGHT = "32px";

// The tooltip card styling, matched to the token counter's so every panel's tabs read identically.
const PANEL_TAB_TOOLTIP_PROPS = {
  p: 3,
  bg: "bg",
  color: "fg",
  borderRadius: "md",
  boxShadow: "lg",
  border: "1px solid",
  borderColor: "border",
} as const;

// One selectable tab, shared by every panel that has them, so they can never drift in height or styling.
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
    <Box position="relative" h={PANEL_TAB_HEIGHT} flexShrink={0}>
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
        <Text truncate maxW={maxLabelWidth} fontFamily={mono ? "var(--app-font-mono)" : undefined}>
          {label}
        </Text>
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
          onClick={(event) => {
            event.stopPropagation();
            onClose();
          }}
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
