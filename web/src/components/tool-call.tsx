"use client";

import { Badge, Box, Flex, Text } from "@chakra-ui/react";
import { useState } from "react";
import { LuChevronRight, LuChevronDown } from "react-icons/lu";
import { getToolCallDisplay } from "@/lib/tool-display";
import { ToolCallView } from "./tool-views";

interface ToolCallProps {
  name: string;
  arguments?: Record<string, unknown>;
  sequenceNumber?: number;
  status?: string;
}

export function ToolCall({ name, arguments: toolArguments, sequenceNumber, status }: ToolCallProps) {
  const [open, setOpen] = useState(false);
  const hasArguments = !!toolArguments && Object.keys(toolArguments).length > 0;

  const { icon: Icon, iconColor, label } = getToolCallDisplay(name, toolArguments);

  return (
    <Box borderRadius="sm" overflow="hidden" bg="bg.subtle" border="1px solid" borderColor="border">
      <Flex
        align="center"
        gap={1.5}
        px={2}
        py={1.5}
        minH="8"
        cursor={hasArguments ? "pointer" : undefined}
        onClick={() => hasArguments && setOpen((current) => !current)}
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
        {status === "running" && (
          <Badge size="sm" variant="subtle" colorPalette="blue" borderRadius="sm">
            Running
          </Badge>
        )}
        {status === "completed" && (
          <Badge size="sm" variant="subtle" colorPalette="green" borderRadius="sm">
            Done
          </Badge>
        )}
        {hasArguments && (
          <Box color="fg.muted" fontSize="xs" ml="auto" flexShrink={0}>
            {open ? <LuChevronDown size={12} /> : <LuChevronRight size={12} />}
          </Box>
        )}
      </Flex>

      {hasArguments && open && (
        <Box px={2} py={2} borderTop="1px solid" borderColor="border" maxH="320px" overflowY="auto">
          <ToolCallView name={name} args={toolArguments} />
        </Box>
      )}
    </Box>
  );
}
