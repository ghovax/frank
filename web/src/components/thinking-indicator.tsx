"use client";

import { Box, Flex, Text } from "@chakra-ui/react";
import { useState } from "react";
import { LuChevronRight, LuChevronDown, LuBrain } from "react-icons/lu";

interface ThinkingIndicatorProps {
  content?: string;
  status?: string;
}

export function ThinkingIndicator({ content, status }: ThinkingIndicatorProps) {
  const [open, setOpen] = useState(false);
  // `content` holds the reasoning body (if any). With a body, its first line is
  // the title (expand for the rest); without one, it's a bare "Thinking" that
  // shimmers while the step is still running.
  const hasReasoning = !!content && content !== "Thinking";
  const title = hasReasoning ? content! : "Thinking";
  const showShimmer = !hasReasoning && status === "running";

  return (
    <Box borderRadius="sm" overflow="hidden" bg="bg.subtle" border="1px solid" borderColor="border">
      <Flex
        align="center"
        gap={1.5}
        px={2}
        py={1.5}
        minH="8"
        cursor={hasReasoning ? "pointer" : undefined}
        onClick={() => hasReasoning && setOpen((current) => !current)}
        userSelect="none"
      >
        <Box color="purple.fg" fontSize="sm" flexShrink={0}>
          <LuBrain size={12} />
        </Box>
        <Text
          fontSize="xs"
          fontWeight="medium"
          truncate
          flex={1}
          className={showShimmer ? "running-title-shimmer" : undefined}
        >
          {title}
        </Text>
        {hasReasoning && (
          <Box color="fg.muted" fontSize="xs" ml="auto" flexShrink={0}>
            {open ? <LuChevronDown size={12} /> : <LuChevronRight size={12} />}
          </Box>
        )}
      </Flex>

      {hasReasoning && open && (
        <Box maxH="250px" overflowY="auto" borderTop="1px solid" borderColor="border" px={2} py={1.5} fontSize="sm" whiteSpace="pre-wrap">
          {content}
        </Box>
      )}
    </Box>
  );
}
