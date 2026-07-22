"use client";

import { Box, Flex, Text } from "@chakra-ui/react";
import type { ReactNode } from "react";

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
        {icon}
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
