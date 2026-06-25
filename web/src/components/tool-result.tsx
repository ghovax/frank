"use client";

import { Box, Flex, Text } from "@chakra-ui/react";
import { useState } from "react";
import { LuChevronRight, LuChevronDown } from "react-icons/lu";
import { getToolResultDisplay } from "@/lib/tool-display";
import { ToolResultView } from "./tool-views";

interface ToolResultProps {
  name?: string;
  content: string;
  sequenceNumber?: number;
}

export function ToolResult({ name, content, sequenceNumber }: ToolResultProps) {
  const [open, setOpen] = useState(false);

  const { icon: Icon, iconColor, label } = getToolResultDisplay(name, content);

  return (
    <Box borderRadius="sm" overflow="hidden" bg="bg.subtle" border="1px solid" borderColor="border">
      <Flex
        align="center"
        gap={1.5}
        px={2}
        py={1.5}
        minH="8"
        cursor="pointer"
        onClick={() => setOpen((current) => !current)}
        userSelect="none"
      >
        {sequenceNumber != null && (
          <Text fontSize="xs" color="fg.subtle" fontWeight="medium" flexShrink={0}>
            #{sequenceNumber}
          </Text>
        )}
        <Box color={iconColor} fontSize="sm" flexShrink={0}>
          <Icon size={12} />
        </Box>
        <Text fontSize="xs" fontWeight="medium" truncate flex={1}>
          {label}
        </Text>
        <Box color="fg.muted" fontSize="xs" ml="auto" flexShrink={0}>
          {open ? <LuChevronDown size={12} /> : <LuChevronRight size={12} />}
        </Box>
      </Flex>

      {open && (
        <Box maxH="320px" overflowY="auto" borderTop="1px solid" borderColor="border" px={2} py={2}>
          <ToolResultView name={name ?? ""} content={content} />
        </Box>
      )}
    </Box>
  );
}
