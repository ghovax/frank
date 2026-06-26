"use client";

import { Box, Flex, Text } from "@chakra-ui/react";
import { useState } from "react";
import { LuBrain, LuChevronRight, LuChevronDown, LuHourglass } from "react-icons/lu";

interface ThinkingIndicatorProps {
  content?: string;
}

export function ThinkingIndicator({ content }: ThinkingIndicatorProps) {
  const [open, setOpen] = useState(false);
  const isWaitingForTools = content === "Waiting for tool results...";

  return (
    <Box borderRadius="sm" overflow="hidden" bg="bg.subtle" border="1px solid" borderColor="border">
      <Flex
        align="center"
        gap={1.5}
        px={2}
        py={1.5}
        minH="8"
        cursor={content && !isWaitingForTools ? "pointer" : undefined}
        onClick={() => content && !isWaitingForTools && setOpen((current) => !current)}
        userSelect="none"
      >
        <Box color={isWaitingForTools ? "blue.fg" : "purple.fg"} fontSize="sm">
          {isWaitingForTools ? <LuHourglass size={12} /> : <LuBrain size={12} />}
        </Box>
        <Text fontSize="xs" fontWeight="medium" truncate flex={1}>
          {content || "Thinking"}
        </Text>
        {content && !isWaitingForTools && (
          <Box color="fg.muted" fontSize="xs" ml="auto">
            {open ? <LuChevronDown size={12} /> : <LuChevronRight size={12} />}
          </Box>
        )}
      </Flex>

      {content && !isWaitingForTools && open && (
        <Box maxH="250px" overflowY="auto" borderTop="1px solid" borderColor="border" px={2} py={1.5} fontSize="sm" whiteSpace="pre-wrap">
          {content}
        </Box>
      )}
    </Box>
  );
}
