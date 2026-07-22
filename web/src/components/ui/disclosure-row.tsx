"use client";

import { Box, Button, Flex, Text } from "@chakra-ui/react";
import { useEffect, useState, type ReactNode } from "react";
import { LuChevronDown, LuChevronRight } from "react-icons/lu";
import { useScrollEdgeFade } from "@/lib/scroll-fade";
import { ActivityIcon } from "./activity-icon";

// The one collapsible line across the app: a fit-content clickable header (leading
// icon + label + trailing badges + disclosure chevron) whose body hangs off a 2px
// hairline left rule — the same visual grammar as a markdown blockquote. It is the
// single source of truth for that pattern, replacing the three hand-rolled copies
// (transcript tool line, grouped run, capability card) that had silently drifted.
//
// The empty rule lives here and nowhere else: a row with no `children` is not
// collapsible — it renders as a plain line with no chevron, so a disclosure can
// never open onto an empty rail. `disabled` greys a row and makes it inert even
// when it has content (a disabled skill/server).

// The header's settled colour. `muted` brightens to `fg` when open or hovered (the
// default line); `active` is always `fg` (a live/kept-open run); `attention` is
// always the warning tone (a call awaiting input).
export type DisclosureTone = "muted" | "active" | "attention";

export interface DisclosureRowProps {
  icon: ReactNode;
  // The label. Simple callers wrap text in <DisclosureLabel>; callers with an
  // animated label (the grouped run) pass their own node.
  title: ReactNode;
  // Trailing chips, right of the label and left of the chevron.
  badges?: ReactNode;
  actions?: ReactNode;
  // The disclosure body. Absent ⇒ the row is a plain, non-clickable line.
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
  // Bounds the body into a scroll region (with soft edge fades). Omit for an
  // unbounded body that grows with its content.
  maxH?: number | string;
  // When this value changes while open, the body scrolls to the bottom — for a run
  // that streams new lines in. Pass e.g. the child count.
  followTailKey?: unknown;
}

// The standard disclosure label: one line, ellipsized, with Chakra's complete sm
// typography metrics. `shimmer` applies the running gradient for a live line.
export function DisclosureLabel({ children, shimmer }: { children: ReactNode; shimmer?: boolean }) {
  return (
    <Text
      textStyle="sm"
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
      <ActivityIcon>
        {icon}
      </ActivityIcon>
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
          <Flex align="center" gap={0.5} flexShrink={0}>
            {actions}
          </Flex>
        )}
      </Flex>

      {collapsible && isOpen && (
        <Box
          ref={containerRef}
          onScroll={onScroll}
          className="disclosure-enter"
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
