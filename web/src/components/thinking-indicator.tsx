"use client";

import { Box, Flex, Text } from "@chakra-ui/react";
import { useState } from "react";
import { LuBrain, LuChevronRight, LuChevronDown } from "react-icons/lu";

interface ThinkingIndicatorProps {
  content?: string;
}

export function ThinkingIndicator({ content }: ThinkingIndicatorProps) {
  const [open, setOpen] = useState(false);

  return (
    <Box borderRadius="sm" overflow="hidden" bg="bg.subtle" border="1px solid" borderColor="border">
      <Flex
        align="center"
        gap={1.5}
        px={2}
        py={1.5}
        minH="8"
        cursor={content ? "pointer" : undefined}
        onClick={() => content && setOpen((current) => !current)}
        userSelect="none"
      >
        <Box color="purple.fg" fontSize="sm">
          <LuBrain size={12} />
        </Box>
        <Text fontSize="xs" fontWeight="medium" truncate flex={1}>
          {content || "Thinking"}
        </Text>
        {content && (
          <Box color="fg.muted" fontSize="xs" ml="auto">
            {open ? <LuChevronDown size={12} /> : <LuChevronRight size={12} />}
          </Box>
        )}
      </Flex>

      {content && open && (
        <Box maxH="250px" overflowY="auto" borderTop="1px solid" borderColor="border" px={2} py={1.5} fontSize="sm" whiteSpace="pre-wrap">
          {content}
        </Box>
      )}
    </Box>
  );
}
