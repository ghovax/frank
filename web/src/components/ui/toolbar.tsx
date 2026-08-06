"use client";

// One toolbar action: an icon button with a tooltip, an active state, and an optional status dot.

import { Box, IconButton } from "@chakra-ui/react";
import type { ReactNode } from "react";
import { Tooltip } from "./tooltip";

export function ToolbarAction({
  label,
  icon,
  onClick,
  active,
  colorPalette,
  indicator,
}: {
  label: string;
  icon: ReactNode;
  onClick?: () => void;
  // Whether the action's target (a panel) is currently open — tints the button.
  active?: boolean;
  // Palette for the active tint and the status dot (e.g. "green", "purple", "blue").
  colorPalette?: string;
  // Show the small status dot (e.g. there is running work behind this action).
  indicator?: boolean;
}) {
  return (
    <Tooltip content={label} openDelay={300}>
      <IconButton
        aria-label={label}
        variant={active ? "subtle" : "ghost"}
        colorPalette={colorPalette}
        position="relative"
        onClick={onClick}
      >
        {icon}
        {indicator && colorPalette ? (
          <Box
            position="absolute"
            top={1}
            right="5px"
            w={1.5}
            h={1.5}
            borderRadius="full"
            bg={`${colorPalette}.solid`}
            boxShadow="0 0 0 1px var(--chakra-colors-bg)"
          />
        ) : null}
      </IconButton>
    </Tooltip>
  );
}
