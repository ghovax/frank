"use client";

import { Box, Flex, Text } from "@chakra-ui/react";
import { useState } from "react";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";
import { LuChevronRight, LuChevronDown } from "react-icons/lu";
import { MarkdownContent } from "./markdown-content";
import { getToolResultDisplay } from "@/lib/tool-display";

interface ToolResultProps {
  name?: string;
  content: string;
  seq?: number;
}

function isJson(text: string): boolean {
  try {
    const parsed = JSON.parse(text);
    return typeof parsed === "object" && parsed !== null;
  } catch {
    return false;
  }
}

export function ToolResult({ name, content, seq }: ToolResultProps) {
  const [open, setOpen] = useState(false);
  const contentIsJson = isJson(content);
  const formattedContent = contentIsJson
    ? JSON.stringify(JSON.parse(content), null, 2)
    : content;

  const { icon: Icon, iconColor, label } = getToolResultDisplay(name, content);

  return (
    <Box borderRadius="lg" overflow="hidden" bg="bg.subtle">
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
        {seq != null && (
          <Text fontSize="xs" color="fg.subtle" fontWeight="medium" flexShrink={0}>
            #{seq}
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
        <Box maxH="250px" overflowY="auto" borderTop="1px solid" borderColor="border">
          {contentIsJson ? (
            <SyntaxHighlighter
              style={oneDark}
              language="json"
              PreTag="div"
              customStyle={{
                margin: 0,
                borderRadius: 0,
                fontSize: "0.75em",
              }}
            >
              {formattedContent}
            </SyntaxHighlighter>
          ) : (
            <Box px={2} py={1.5} fontSize="sm">
              <MarkdownContent content={formattedContent} />
            </Box>
          )}
        </Box>
      )}
    </Box>
  );
}
