"use client";

// One row of a tree, and the only one, shared by the sidebar, its conversations, and the delegated panel.

import { Box, Button, Flex } from "@chakra-ui/react";
import type { ReactNode } from "react";
import { LuChevronDown, LuChevronRight } from "react-icons/lu";

// A row that can expand; one that cannot passes nothing and spends no width on the column.
export type TreeRowDisclosure = "open" | "closed";

// The row's height and each leading slot's width, fixed so the columns are exact and nesting indents by a slot.
const ROW_HEIGHT = 7;
const SLOT_WIDTH = 4;

// The geometry the rail is derived from, in pixels, because it lands on a half-slot the spacing scale has no step for.
const ROW_INSET = 6;
const RAIL_WIDTH = 2;
const RAIL_OFFSET = ROW_INSET + 16 / 2 - RAIL_WIDTH / 2;
const CHILD_INSET = 9;

// Where the trailing mark sits, computed rather than nudged so the dot is centred in the row.
const DOT_SIZE = 6;
const TRAILING_GAP = (28 - DOT_SIZE) / 2;
const TRAILING_INSET = TRAILING_GAP - ROW_INSET;
const ACTIONS_INSET = TRAILING_GAP + DOT_SIZE / 2 - 20 / 2;

export function TreeRow({
  disclosure,
  onDisclosureChange,
  disclosureLabel,
  glyph,
  label,
  badges,
  actions,
  selected = false,
  onActivate,
  children,
}: {
  disclosure?: TreeRowDisclosure;
  onDisclosureChange?: (open: boolean) => void;
  disclosureLabel?: string;
  // A leading glyph and its column, for a list where every row has one.
  glyph?: ReactNode;
  label: ReactNode;
  // The trailing slot at the row's right edge, which yields its spot to the actions on hover.
  badges?: ReactNode;
  actions?: ReactNode;
  selected?: boolean;
  onActivate?: () => void;
  // The nested rows, hanging off the hairline rail. Rendered only while open.
  children?: ReactNode;
}) {
  // A row is collapsible because it has something to collapse, never because a caller said so.
  const collapsible = children != null;
  const expanded = collapsible && disclosure === "open";
  return (
    <Box minW={0}>
      <Flex
        className="sidebar-row"
        align="center"
        gap={1}
        h={ROW_HEIGHT}
        px={1.5}
        minW={0}
        position="relative"
        borderRadius="md"
        bg={selected ? "blue.subtle" : undefined}
        _hover={{ bg: selected ? "blue.muted" : "bg.subtle" }}
        transition="background-color 0.12s"
        css={{
          // The trailing controls appear on hover and are taken out of layout when they do not.
          "@media (hover: hover)": {
            "& [data-row-actions]": { display: "none" },
            "&:hover > [data-row-actions]": { display: "flex" },
            "&:focus-within > [data-row-actions]": { display: "flex" },
            "&:hover > [data-row-badges]": { visibility: "hidden" },
            "&:focus-within > [data-row-badges]": { visibility: "hidden" },
          },
        }}
      >
        {disclosure && collapsible ? (
          <Button
            type="button"
            aria-label={disclosureLabel}
            aria-expanded={expanded}
            variant="plain"
            boxSize={SLOT_WIDTH}
            minW={0}
            flexShrink={0}
            p={0}
            color="fg.subtle"
            _hover={{ bg: "transparent", color: "fg" }}
            _focusVisible={{ outline: "none", boxShadow: "none", color: "fg" }}
            onClick={() => onDisclosureChange?.(!expanded)}
          >
            {expanded ? <LuChevronDown size={12} /> : <LuChevronRight size={12} />}
          </Button>
        ) : null}

        {glyph ? (
          <Box
            w={SLOT_WIDTH}
            flexShrink={0}
            display="flex"
            alignItems="center"
            justifyContent="center"
            color="fg.muted"
          >
            {glyph}
          </Box>
        ) : null}

        {/* The label fills the row and carries the activation, as a real button so it is reachable by keyboard. */}
        <Button
          type="button"
          variant="plain"
          flex={1}
          minW={0}
          h="full"
          p={0}
          gap={0}
          justifyContent="flex-start"
          textAlign="left"
          fontWeight="normal"
          userSelect="none"
          color={selected ? "blue.fg" : "fg"}
          _hover={{ bg: "transparent" }}
          _focusVisible={{ outline: "none", boxShadow: "none" }}
          onClick={onActivate}
          // The label fills the button, which a Chakra button's inline-flex child would otherwise not do.
          css={{ "& > *": { flex: 1, minWidth: 0, maxWidth: "100%" } }}
        >
          {label}
        </Button>

        {badges ? (
          // Hidden rather than removed while the actions are up, so nothing shifts left of it on hover.
          <Flex data-row-badges align="center" gap={1.5} flexShrink={0} pr={`${TRAILING_INSET}px`}>
            {badges}
          </Flex>
        ) : null}

        {actions ? (
          <Flex
            data-row-actions
            align="center"
            gap={0.5}
            flexShrink={0}
            position="absolute"
            // Centred on the same point the trailing slot occupies, so the menu appears exactly where the dot was.
            right={`${ACTIONS_INSET}px`}
            top="50%"
            transform="translateY(-50%)"
            // Callers wrap their controls in a block box, so the centring belongs here rather than to each caller.
            css={{ "& > *": { display: "flex", alignItems: "center" } }}
          >
            {actions}
          </Flex>
        ) : null}
      </Flex>

      {/* The nested rows hang off the hairline every disclosure body uses, descending from the chevron's centre. */}
      {expanded ? (
        <Box
          ml={`${RAIL_OFFSET}px`}
          pl={`${CHILD_INSET}px`}
          py={1}
          borderLeft={`${RAIL_WIDTH}px solid`}
          borderColor="border.muted"
        >
          {children}
        </Box>
      ) : null}
    </Box>
  );
}
