"use client";

import { Box, Flex, Text } from "@chakra-ui/react";
import type { ReactNode } from "react";

// The icon's box, stated rather than left to the glyph. An `<svg size={14}>` sits on the text
// baseline with whatever bearing its own artwork gives it, so centring two boxes of different
// heights lands every heading a fraction differently depending on which icon it was given. A
// fixed square, centred in both axes and filled by the glyph, makes the alignment a constraint
// the layout has to satisfy rather than a coincidence each icon either produces or does not.
//
// The title's own line height is deliberately left alone. Setting it here to "match" the square
// was wrong twice over: it is the *icon* that should adapt to the text, not the text to the
// icon, and a unitless `lineHeight` is a multiplier — so the value that was meant to say 16px
// said four times the font size, and every heading grew a 56px line box that shoved its
// description down the panel.
const ICON_BOX_SIZE = "4";

// A section heading inside a panel or dialog body: a muted leading icon + a
// panel-title label, with an optional description line below. One component for the
// "icon + title (+ description)" strip that was re-spelled inline across the skills
// browser and the connection settings.
export function SectionHeader({
  icon,
  title,
  description,
  mb = 2,
}: {
  icon: ReactNode;
  title: ReactNode;
  description?: ReactNode;
  mb?: number;
}) {
  return (
    <Box mb={mb}>
      <Flex align="center" gap={1.5} color="fg.muted">
        <Box
          boxSize={ICON_BOX_SIZE}
          flexShrink={0}
          display="flex"
          alignItems="center"
          justifyContent="center"
          // The glyph fills its square, so a 14px icon and a 16px one both centre on the same
          // axis rather than each defining its own.
          css={{ "& > svg": { width: "100%", height: "100%" } }}
        >
          {icon}
        </Box>
        <Text textStyle="panelTitle">{title}</Text>
      </Flex>
      {description && (
        <Box mt={2} color="fg.muted">
          <Text fontSize="xs">{description}</Text>
        </Box>
      )}
    </Box>
  );
}
