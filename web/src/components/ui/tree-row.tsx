"use client";

// One row of a tree, and the only one.

import { Box, Button, Flex } from "@chakra-ui/react";
import type { ReactNode } from "react";
import { LuChevronDown, LuChevronRight } from "react-icons/lu";

// A row that can expand.
export type TreeRowDisclosure = "open" | "closed";

// The row's own height, and the width of each leading slot.
const ROW_HEIGHT = 7;
const SLOT_WIDTH = 4;

// The geometry the rail is derived from, in pixels, because the rail has to land on a half-slot and the spacing scale has no such step.
const ROW_INSET = 6;
const RAIL_WIDTH = 2;
const RAIL_OFFSET = ROW_INSET + 16 / 2 - RAIL_WIDTH / 2;
const CHILD_INSET = 9;

// Where the trailing mark sits, and why it is arithmetic rather than a nudge.
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
  // A leading glyph, and its column, for a list where every row has one — the workspaces and their folder.
  glyph?: ReactNode;
  label: ReactNode;
  // The trailing slot: a count, a status dot.
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
          // The trailing controls appear on hover, and are taken out of layout when they do not — an invisible button still occupies its width, which pushed everything left of it inward on every quiet row.
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
          <Box w={SLOT_WIDTH} flexShrink={0} display="flex" alignItems="center" justifyContent="center" color="fg.muted">
            {glyph}
          </Box>
        ) : null}

        {/* The label fills the row and carries the activation, so clicking anywhere that is not
            the chevron or an action opens the thing named. A plain button rather than a div:
            it is reachable by keyboard and announced as what it is. */}
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
          // The label fills the button, and this is what makes it do so.
          css={{ "& > *": { flex: 1, minWidth: 0, maxWidth: "100%" } }}
        >
          {label}
        </Button>

        {badges ? (
          // Hidden rather than removed while the actions are up: the ⋯ takes exactly this spot, and dropping the slot out of layout would shift everything left of it on hover.
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
            // Centred on the same point the trailing slot occupies, so the ⋯ appears exactly where the status dot was rather than a few pixels beside it.
            right={`${ACTIONS_INSET}px`}
            top="50%"
            transform="translateY(-50%)"
            // Callers wrap their controls in a plain Box, which is a block box — so an inline-flex button inside it sits on the text baseline, a couple of pixels below the centre.
            css={{ "& > *": { display: "flex", alignItems: "center" } }}
          >
            {actions}
          </Flex>
        ) : null}
      </Flex>

      {/* The nested rows hang off the same hairline every disclosure body in the app uses,
          placed so the line descends from the centre of the chevron above it rather than from
          somewhere near it. */}
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
