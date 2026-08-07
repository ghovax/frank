"use client";

import { Box, Button, Flex, Text } from "@chakra-ui/react";
import { useEffect, useState, type ReactNode } from "react";
import { LuChevronDown, LuChevronRight } from "react-icons/lu";
import { useScrollEdgeFade } from "@/lib/scroll-fade";
import { ActivityIcon } from "./activity-icon";

// The one collapsible line across the app: a clickable header whose body hangs off a hairline left rule.

// The header's settled colour: `muted` brightens on hover, `active` stays lit, `attention` warns.
export type DisclosureTone = "muted" | "active" | "attention";

export interface DisclosureRowProps {
  // Optional: a row without one spends no width on the slot. See `triggerContent`.
  icon?: ReactNode;
  // The label: simple callers wrap text, and one with an animated label passes its own node.
  title: ReactNode;
  // Trailing chips, right of the label and left of the chevron.
  badges?: ReactNode;
  actions?: ReactNode;
  // Actions lie over the right edge, so hover-only controls do not permanently shorten every title.
  actionsOverlay?: boolean;
  // The disclosure body. Absent means the row is a plain, non-clickable line.
  children?: ReactNode;
  // Controlled open state; omit to let the row own it (with `defaultOpen`).
  open?: boolean;
  defaultOpen?: boolean;
  onOpenChange?: (open: boolean) => void;
  onActivate?: () => void;
  fill?: boolean;
  // Has content but must not expand (and reads greyed): a disabled capability.
  disabled?: boolean;
  tone?: DisclosureTone;
  // Bounds the body into a scroll region. Omit for a body that grows with its content.
  maxH?: number | string;
  // When this changes while open, the body scrolls to the bottom — pass the child count for a live run.
  followTailKey?: unknown;
}

// The standard disclosure label: one ellipsized line, with a shimmer for a live one.
export function DisclosureLabel({
  children,
  shimmer,
  size = "sm",
}: {
  children: ReactNode;
  shimmer?: boolean;
  size?: "sm" | "xs";
}) {
  return (
    <Text
      textStyle={size}
      fontWeight="normal"
      whiteSpace="nowrap"
      overflow="hidden"
      textOverflow="ellipsis"
      className={shimmer ? "running-title-shimmer" : undefined}
    >
      {children}
    </Text>
  );
}

export function DisclosureRow({
  icon,
  title,
  badges,
  actions,
  actionsOverlay = false,
  children,
  open,
  defaultOpen = false,
  onOpenChange,
  onActivate,
  fill = false,
  disabled = false,
  tone = "muted",
  maxH,
  followTailKey,
}: DisclosureRowProps) {
  const [internalOpen, setInternalOpen] = useState(defaultOpen);
  const isControlled = open !== undefined;
  const isOpen = isControlled ? open : internalOpen;
  const collapsible = !!children && !disabled;
  const interactive = (collapsible || !!onActivate) && !disabled;
  const { containerRef, onScroll, fade } = useScrollEdgeFade();

  useEffect(() => {
    if (isOpen && followTailKey !== undefined && containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight;
    }
  }, [followTailKey, isOpen, containerRef]);

  const activate = () => {
    if (collapsible) {
      const next = !isOpen;
      if (!isControlled) setInternalOpen(next);
      onOpenChange?.(next);
      return;
    }
    onActivate?.();
  };

  const color =
    tone === "attention" ? "yellow.fg" : tone === "active" ? "fg" : isOpen ? "fg" : "fg.muted";
  const triggerContent = (
    <>
      {/* The glyph slot exists only for rows that have a glyph, so a list without icons gets its space back. */}
      {icon ? <ActivityIcon>{icon}</ActivityIcon> : null}
      <Box minW={0} flex={fill ? 1 : undefined} flexShrink={1}>
        {title}
      </Box>
      {badges && (
        <Flex align="center" gap={1.5} flexShrink={0} minW={0}>
          {badges}
        </Flex>
      )}
      {collapsible && (
        <Box display="flex" alignItems="center" flexShrink={0} opacity={0.7}>
          {isOpen ? <LuChevronDown size={12} /> : <LuChevronRight size={12} />}
        </Box>
      )}
    </>
  );

  return (
    <Box minW={0} opacity={disabled ? 0.55 : 1}>
      <Flex
        align="center"
        gap={1}
        h={6}
        w={fill ? "full" : "fit-content"}
        maxW="100%"
        minW={0}
        position={actionsOverlay ? "relative" : undefined}
      >
        {interactive ? (
          <Button
            variant="plain"
            h={6}
            w={fill ? "auto" : "fit-content"}
            maxW="100%"
            minW={0}
            flex={fill ? 1 : undefined}
            p={0}
            borderWidth={0}
            gap={1.5}
            justifyContent="flex-start"
            flexShrink={1}
            textAlign="left"
            fontWeight="normal"
            userSelect="none"
            color={color}
            onClick={activate}
            _hover={tone === "muted" ? { color: "fg" } : undefined}
          >
            {triggerContent}
          </Button>
        ) : (
          <Flex
            align="center"
            gap={1.5}
            h={6}
            w="fit-content"
            maxW="100%"
            minW={0}
            flexShrink={1}
            textAlign="left"
            userSelect="none"
            color={color}
          >
            {triggerContent}
          </Flex>
        )}
        {actions && (
          <Flex
            align="center"
            gap={0.5}
            flexShrink={0}
            {...(actionsOverlay
              ? {
                  position: "absolute" as const,
                  right: 0,
                  top: "50%",
                  transform: "translateY(-50%)",
                }
              : {})}
            // Centred here, or an inline-flex button inside a block box sits on the text baseline and rides low.
            css={{ "& > *": { display: "flex", alignItems: "center" } }}
          >
            {actions}
          </Flex>
        )}
      </Flex>

      {collapsible && isOpen && (
        <Box
          ref={containerRef}
          onScroll={onScroll}
          py={1}
          ml={1.5}
          pl={3.5}
          borderLeft="2px solid"
          borderColor="border.muted"
          maxH={maxH}
          overflowY={maxH ? "auto" : undefined}
          overflowX={maxH ? "auto" : undefined}
          css={fade}
        >
          {children}
        </Box>
      )}
    </Box>
  );
}
