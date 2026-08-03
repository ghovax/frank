"use client";

// One row of a tree, and the only one. The workspaces in the sidebar, the conversations under
// them, and the delegated work in its panel are all this component — so a chevron, a glyph and
// a label sit at the same three x positions everywhere, and a list reads as one column rather
// than as several that nearly line up.
//
// It exists because they did not. `DisclosureRow` — the app's general collapsible line — puts
// its chevron *after* the label, at the trailing edge, which is right for a tool call in the
// transcript, where the row is a fit-content line and the chevron closes the sentence. A tree
// needs the opposite: the disclosure belongs at the leading edge, on the indent rail, because
// that is the column the eye follows down a hierarchy. Rendering a session tree out of the
// transcript's row meant one arrow on the left of a session and another on the right of the
// workspace above it, which is exactly as confusing as it sounds.
//
// The two rules that make a tree line up:
//
//   • A column exists only for a row that has something to put in it. A chevron for a row that
//     can expand, a glyph for a row that has one — and nothing at all otherwise, so a title
//     starts at the row's own edge rather than behind an indent held open for a mark that will
//     never come. Depth is said by the indent rail, which is the one thing that does say it.
//   • Anything that comes and goes — a status dot, a count — rides the trailing edge, where its
//     arrival cannot move the label. The leading edge is for what a row permanently is.

import { Box, Button, Flex } from "@chakra-ui/react";
import type { ReactNode } from "react";
import { LuChevronDown, LuChevronRight } from "react-icons/lu";

// A row that can expand. One that cannot passes nothing and spends no width on the column:
// depth is already said by the indent rail, and holding the slot open on every leaf put a
// second empty column in front of every title that had nothing to put there.
export type TreeRowDisclosure = "open" | "closed";

// The row's own height, and the width of each leading slot. Fixed rather than content-driven:
// that is what makes the columns exact, and what lets a nested list indent by a whole slot.
const ROW_HEIGHT = 7;
const SLOT_WIDTH = 4;

// The geometry the rail is derived from, in pixels, because the rail has to land on a half-slot
// and the spacing scale has no such step. The row insets its content by 6px and the disclosure
// slot is 16px wide, so a chevron's centre line sits 14px from the row's left edge. The rail
// under it is 2px wide, so its left edge belongs at 13px — otherwise the line that is supposed
// to descend *from* the arrow starts three pixels beside it, which is exactly what it looked
// like. The children then start one step further in.
const ROW_INSET = 6;
const RAIL_WIDTH = 2;
const RAIL_OFFSET = ROW_INSET + 16 / 2 - RAIL_WIDTH / 2;
const CHILD_INSET = 9;

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
  // A leading glyph, and its column, for a list where every row has one — the workspaces and
  // their folder. A list whose rows have nothing to put there passes nothing and spends no
  // width on it, which is what kept the conversation titles pinned a column to the right of
  // where they belonged.
  glyph?: ReactNode;
  label: ReactNode;
  // The trailing slot: a count, a status dot. It sits at the row's right edge, where it cannot
  // push the label around as it comes and goes, and it yields that spot to the actions while
  // the row is hovered.
  badges?: ReactNode;
  actions?: ReactNode;
  selected?: boolean;
  onActivate?: () => void;
  // The nested rows, hanging off the hairline rail. Rendered only while open.
  children?: ReactNode;
}) {
  const expanded = disclosure === "open";
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
          // The trailing controls appear on hover, and are taken out of layout when they do not
          // — an invisible button still occupies its width, which pushed everything left of it
          // inward on every quiet row. Gated on there being a pointer at all: on a touch screen
          // there is no hover, so a control revealed by it is a control that does not exist,
          // and WebKit spends the first tap on the hover state rather than on the row.
          "@media (hover: hover)": {
            "& [data-row-actions]": { display: "none" },
            "&:hover > [data-row-actions]": { display: "flex" },
            "&:focus-within > [data-row-actions]": { display: "flex" },
            "&:hover > [data-row-badges]": { visibility: "hidden" },
            "&:focus-within > [data-row-badges]": { visibility: "hidden" },
          },
        }}
      >
        {disclosure ? (
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
          // The label fills the button, and this is what makes it do so. A Chakra button is an
          // inline-flex box, so a child left to itself is only as wide as its text — and the
          // title's hover mask, which dissolves the last ~68px so a long title passes under the
          // ⋯ rather than through it, was then measured against the text's own width. On a short
          // title that is most of the title, which is why hovering a row appeared to erase it.
          css={{ "& > *": { flex: 1, minWidth: 0, maxWidth: "100%" } }}
        >
          {label}
        </Button>

        {badges ? (
          // Hidden rather than removed while the actions are up: the ⋯ takes exactly this spot,
          // and dropping the slot out of layout would shift everything left of it on hover.
          <Flex data-row-badges align="center" gap={1.5} flexShrink={0} pr={0.5}>
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
            // Centred on the same point the trailing slot occupies, so the ⋯ appears exactly
            // where the status dot was rather than a few pixels beside it: the dot's centre sits
            // 11px from the row's right edge, and this button is 20px wide.
            right="1px"
            top="50%"
            transform="translateY(-50%)"
            // Callers wrap their controls in a plain Box, which is a block box — so an
            // inline-flex button inside it sits on the text baseline, a couple of pixels below
            // the centre. Centring belongs to the slot, not to each caller.
            css={{ "& > *": { display: "flex", alignItems: "center" } }}
          >
            {actions}
          </Flex>
        ) : null}
      </Flex>

      {/* The nested rows hang off the same hairline every disclosure body in the app uses,
          placed so the line descends from the centre of the chevron above it rather than from
          somewhere near it. */}
      {expanded && children ? (
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
