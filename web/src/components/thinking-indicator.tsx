"use client";

import { Box, Flex, Text } from "@chakra-ui/react";
import { useState } from "react";
import { LuChevronRight, LuChevronDown, LuBrain } from "react-icons/lu";
import { MarkdownContent } from "./markdown-content";

interface ThinkingIndicatorProps {
  content?: string;
  status?: string;
  durationMs?: number;
}

// "Thought for 5s" / "1m 12s" — the server measures the reasoning phase and
// sends the duration, so this only formats it.
function formatThinkingDuration(durationMs: number): string {
  if (durationMs < 1000) return "<1s";
  const totalSeconds = Math.round(durationMs / 1000);
  if (totalSeconds < 60) return `${totalSeconds}s`;
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return seconds ? `${minutes}m ${seconds}s` : `${minutes}m`;
}

export function ThinkingIndicator({ content, status, durationMs }: ThinkingIndicatorProps) {
  const [open, setOpen] = useState(false);
  // The header is the phase's status: "Thinking" (shimmering) while the model
  // reasons, then "Thought for Ns" once it finishes. `content` holds the
  // reasoning body, surfaced in the expandable section below. The phase shimmers
  // until it's done; the duration label appears only once it has been measured.
  const running = status !== "done" && status !== "completed";
  const hasReasoning = !!content && content !== "Thinking";
  const title = !running && durationMs != null ? `Thought for ${formatThinkingDuration(durationMs)}` : "Thinking";

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
          className={running ? "running-title-shimmer" : undefined}
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
        <Box maxH="250px" overflowY="auto" borderTop="1px solid" borderColor="border" px={2} py={1.5} fontSize="sm">
          <MarkdownContent content={content ?? ""} />
        </Box>
      )}
    </Box>
  );
}
