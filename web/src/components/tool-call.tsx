"use client";

import { Box, Flex, Text } from "@chakra-ui/react";
import { useState } from "react";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";
import { LuChevronRight, LuChevronDown, LuWrench } from "react-icons/lu";

interface ToolCallProps {
  name: string;
  arguments?: Record<string, unknown>;
}

export function ToolCall({ name, arguments: toolArguments }: ToolCallProps) {
  const [open, setOpen] = useState(false);
  const formattedArguments = toolArguments
    ? JSON.stringify(toolArguments, null, 2)
    : null;

  const displayTitle =
    name === "bash" && typeof toolArguments?.command === "string"
      ? toolArguments.command
      : name;

  return (
    <Box borderRadius="lg" overflow="hidden" bg="bg.subtle">
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
        <Box color="fg.muted" fontSize="sm">
          <LuWrench size={12} />
        </Box>
        <Text fontSize="xs" fontFamily="monospace" fontWeight="medium" truncate flex={1}>
          {displayTitle}
        </Text>
        {formattedArguments && (
          <Box color="fg.muted" fontSize="xs" ml="auto">
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
