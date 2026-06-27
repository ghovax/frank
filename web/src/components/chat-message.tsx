"use client";

import { Box, Button, Code, Flex, HStack, Text } from "@chakra-ui/react";
import { useEffect, useRef } from "react";
import { LuTerminal } from "react-icons/lu";
import type { ChatMessage } from "@/lib/use-chat";
import { MarkdownContent } from "./markdown-content";
import { ToolCall } from "./tool-call";
import { ThinkingIndicator } from "./thinking-indicator";

interface ChatMessageProps {
  message: ChatMessage;
  onPermission?: (requestId: string, decision: "allow" | "deny") => void;
  agents?: { id: string; name: string }[];
}

function PermissionBox({ message, onPermission }: { message: ChatMessage; onPermission?: ChatMessageProps["onPermission"] }) {
  const resolved = message.meta?.resolved as string | undefined;
  const requestId = message.meta?.request_id as string | undefined;
  const justification = message.meta?.justification as string | undefined;
  const risk = message.meta?.risk as string | undefined;
  const boxRef = useRef<HTMLDivElement>(null);

  const headerBg = risk === "high" ? "red.subtle" : risk === "medium" ? "yellow.subtle" : undefined;

  function handleKeyDown(event: React.KeyboardEvent) {
    if (event.key === "Enter") {
      event.preventDefault();
      onPermission?.(String(requestId), "allow");
    } else if (event.key === "Escape") {
      event.preventDefault();
      onPermission?.(String(requestId), "deny");
    }
  }

  useEffect(() => {
    if (!resolved && boxRef.current) {
      boxRef.current.focus();
    }
  }, [resolved]);

  return (
    <Box alignSelf="flex-start" w="100%">
      <Box
        ref={boxRef}
        tabIndex={resolved ? undefined : 0}
        onKeyDown={resolved ? undefined : handleKeyDown}
        borderRadius="sm"
        overflow="hidden"
        bg="bg.subtle"
        border="1px solid"
        borderColor="border"
        _focus={{ outline: "none", boxShadow: "none" }}
      >
        <Flex align="center" gap={1.5} px={2} py={1.5} minH="8" userSelect="none" bg={headerBg}>
          <Box color="green.fg" fontSize="sm" flexShrink={0}>
            <LuTerminal size={12} />
          </Box>
          <Text fontSize="xs" fontWeight="medium" truncate flex={1}>
            {justification || "Run command"}
          </Text>
        </Flex>
        <Box px={2} py={1.5} borderTop="1px solid" borderColor="border">
          <Code display="block" fontFamily="var(--app-font-mono)" fontSize="xs" p={1.5} whiteSpace="pre-wrap" bg="bg.muted" borderRadius="sm">
            {message.content}
          </Code>
        </Box>
        {resolved ? (
          <Box px={2} pb={2}>
            <Text fontSize="xs" fontWeight="bold" color={resolved === "allow" ? "green.fg" : "red.fg"}>
              {resolved === "allow" ? "Allowed" : resolved === "interrupted" ? "Interrupted" : "Denied"}
            </Text>
          </Box>
        ) : (
          <Box borderTop="1px solid" borderColor="border" px={2} py={2}>
            <HStack gap={2}>
              <Button size="xs" colorPalette="green" onClick={() => onPermission?.(String(requestId), "allow")}>Allow</Button>
              <Button size="xs" colorPalette="red" onClick={() => onPermission?.(String(requestId), "deny")}>Deny</Button>
            </HStack>
          </Box>
        )}
      </Box>
    </Box>
  );
}

export function ChatMessageItem({ message, onPermission, agents = [] }: ChatMessageProps) {
  switch (message.role) {
    case "user":
      return (
        <Box alignSelf="flex-end" bg="bg.muted" border="1px solid" borderColor="border" px={2} py={1.5} borderRadius="sm" maxW="80%">
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
      return <ThinkingIndicator content={message.content} focus={message.meta?.focus as string | undefined} icon={message.meta?.icon as string | undefined} status={message.meta?.status as string | undefined} />;

    case "tool_call": {
      return (
        <Box alignSelf="flex-start" w="100%">
          <ToolCall
            name={message.content}
            arguments={message.meta?.arguments as Record<string, unknown> | undefined}
            sequenceNumber={message.meta?.sequenceNumber as number | undefined}
            status={message.meta?.status as string | undefined}
            agents={agents}
          />
        </Box>
      );
    }

    case "permission":
      return <PermissionBox message={message} onPermission={onPermission} />;

    case "error":
      return (
        <Box alignSelf="flex-start" bg="red.subtle" border="1px solid" borderColor="red.muted" px={2} py={1.5} borderRadius="sm" maxW="80%">
          <Text fontSize="xs" color="red.fg">{message.content}</Text>
        </Box>
      );

    default:
      return null;
  }
}
