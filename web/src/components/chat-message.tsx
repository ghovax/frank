"use client";

import { Box, Button, Code, Flex, HStack, Text } from "@chakra-ui/react";
import type { ChatMessage } from "@/lib/use-chat";
import { MarkdownContent } from "./markdown-content";
import { ToolCall } from "./tool-call";
import { ToolResult } from "./tool-result";
import { ThinkingIndicator } from "./thinking-indicator";

interface ChatMessageProps {
  message: ChatMessage;
  onPermission?: (requestId: string, decision: "allow" | "deny") => void;
}

export function ChatMessageItem({ message, onPermission }: ChatMessageProps) {
  switch (message.role) {
    case "user":
      return (
        <Box alignSelf="flex-end" bg="bg.muted" border="1px solid" borderColor="border" px={3} py={2} borderRadius="lg" maxW="80%">
          <MarkdownContent content={message.content} />
        </Box>
      );

    case "assistant":
      if (!message.content) {
        return <ThinkingIndicator />;
      }
      return (
        <Box alignSelf="flex-start" px={1}>
          <MarkdownContent content={message.content} />
        </Box>
      );

    case "thinking":
      return <ThinkingIndicator content={message.content} />;

    case "tool_call":
      return (
        <Box alignSelf="flex-start" w="100%">
          <ToolCall
            name={message.content}
            arguments={message.meta?.arguments as Record<string, unknown> | undefined}
            seq={message.meta?.seq as number | undefined}
          />
        </Box>
      );

    case "tool_result":
      return (
        <Box alignSelf="flex-start" w="100%">
          <ToolResult
            name={message.meta?.name as string | undefined}
            content={message.content}
            seq={message.meta?.seq as number | undefined}
          />
        </Box>
      );

    case "permission": {
      const resolved = message.meta?.resolved as string | undefined;
      return (
        <Box
          alignSelf="flex-start"
          border="1px solid"
          borderColor="yellow.muted"
          borderRadius="lg"
          overflow="hidden"
          maxW="90%"
        >
          <Flex align="center" gap={2} px={2} py={1.5} bg="yellow.subtle">
            <Text fontSize="xs" fontWeight="bold" color="yellow.fg">PERMISSION</Text>
            {message.meta?.risk && (
              <Code fontSize="xs" colorPalette="red">{String(message.meta.risk)}</Code>
            )}
          </Flex>
          <Box px={2} py={1.5}>
            <Code display="block" fontSize="xs" p={1.5} whiteSpace="pre-wrap" bg="bg.muted" borderRadius="lg">
              {message.content}
            </Code>
            {message.meta?.justification && (
              <Text fontSize="xs" color="fg.muted" mt={1}>{String(message.meta.justification)}</Text>
            )}
            <Box mt={1.5}>
              {resolved ? (
                <Text fontSize="xs" fontWeight="bold" color={resolved === "allow" ? "green.fg" : "red.fg"}>
                  {resolved === "allow" ? "Allowed" : "Denied"}
                </Text>
              ) : (
                <HStack gap={1.5}>
                  <Button size="xs" colorPalette="green" onClick={() => onPermission?.(String(message.meta?.request_id), "allow")}>Allow</Button>
                  <Button size="xs" colorPalette="red" variant="outline" onClick={() => onPermission?.(String(message.meta?.request_id), "deny")}>Deny</Button>
                </HStack>
              )}
            </Box>
          </Box>
        </Box>
      );
    }

    case "error":
      return (
        <Box alignSelf="flex-start" bg="red.subtle" border="1px solid" borderColor="red.muted" px={3} py={1.5} borderRadius="lg" maxW="80%">
          <Text fontSize="xs" color="red.fg">{message.content}</Text>
        </Box>
      );

    default:
      return null;
  }
}
