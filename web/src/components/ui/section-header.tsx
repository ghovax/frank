"use client";

import { Box, Flex, Text } from "@chakra-ui/react";
import type { ReactNode } from "react";

// The icon's box, stated rather than left to whatever bearing a glyph's own artwork gives it.
const ICON_BOX_SIZE = "4";

// A section heading inside a panel: a muted icon, a title, and an optional description line.
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
          // The glyph fills its square, so a 14px icon and a 16px one centre on the same axis.
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
