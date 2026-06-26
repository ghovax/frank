"use client";

import { Box, Flex, Text } from "@chakra-ui/react";
import { useState } from "react";
import { LuChevronRight, LuChevronDown } from "react-icons/lu";
import { getFocusIcon } from "@/lib/focus-icon";

interface ThinkingIndicatorProps {
  content?: string;
  focus?: string;
  icon?: string;
  status?: string;
}

export function ThinkingIndicator({ content, focus, icon, status }: ThinkingIndicatorProps) {
  const [open, setOpen] = useState(false);
  const title = focus || content || "Thinking";
  const isWaiting = icon === "waiting" || title === "Waiting for tools...";
  // `content` holds the reasoning body (if any); a real label means we are not
  // showing a bare placeholder, so the shimmer only applies before any focus.
  const hasReasoning = !!content && content !== "Thinking";
  const showShimmer = !focus && status === "running" && !isWaiting;

  const { icon: FocusIcon, color: iconColor } = getFocusIcon(icon, title);

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
        <Box color={iconColor} fontSize="sm" flexShrink={0}>
          <FocusIcon size={12} />
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
