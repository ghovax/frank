"use client";

import { Box, Flex, Text } from "@chakra-ui/react";
import { useState } from "react";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";
import { LuChevronRight, LuChevronDown } from "react-icons/lu";
import { getToolCallDisplay } from "@/lib/tool-display";

interface ToolCallProps {
  name: string;
  arguments?: Record<string, unknown>;
  sequenceNumber?: number;
}

export function ToolCall({ name, arguments: toolArguments, sequenceNumber }: ToolCallProps) {
  const [open, setOpen] = useState(false);
  const formattedArguments = toolArguments
    ? JSON.stringify(toolArguments, null, 2)
    : null;

  const { icon: Icon, iconColor, label } = getToolCallDisplay(name, toolArguments);

  return (
    <Box borderRadius="sm" overflow="hidden" bg="bg.subtle" border="1px solid" borderColor="border">
      <Flex
        align="center"
        gap={1.5}
        px={2}
        py={1.5}
        minH="8"
        cursor={formattedArguments ? "pointer" : undefined}
        onClick={() => formattedArguments && setOpen((current) => !current)}
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
        {formattedArguments && (
          <Box color="fg.muted" fontSize="xs" ml="auto" flexShrink={0}>
            {open ? <LuChevronDown size={12} /> : <LuChevronRight size={12} />}
          </Box>
        )}
      </Flex>

      {formattedArguments && open && (
        <SyntaxHighlighter
          style={oneDark}
          language="json"
          PreTag="div"
          customStyle={{
            margin: 0,
            borderRadius: 0,
            fontSize: "0.75em",
            maxHeight: "200px",
          }}
        >
          {formattedArguments}
        </SyntaxHighlighter>
      )}
    </Box>
  );
}
