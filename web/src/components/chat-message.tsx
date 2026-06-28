"use client";

import { Box, Text } from "@chakra-ui/react";
import type { ChatMessage } from "@/lib/use-chat";
import type { PermissionDecision, ToolEventStatus, ToolPermission } from "@/lib/tool-event";
import { MarkdownContent } from "./markdown-content";
import { ToolCall } from "./tool-call";
import { ThinkingIndicator } from "./thinking-indicator";

interface ChatMessageProps {
  message: ChatMessage;
  onPermission?: (requestId: string, decision: PermissionDecision) => void;
  agents?: { id: string; name: string }[];
}

export function ChatMessageItem({ message, onPermission, agents = [] }: ChatMessageProps) {
  const toolStatus = (status: unknown): ToolEventStatus | undefined =>
    status === "running" || status === "completed" || status === "done" || status === "failed" || status === "input_required" ? status : undefined;

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
      return (
        <ThinkingIndicator
          content={message.content}
          status={message.meta?.status as string | undefined}
          durationMs={message.meta?.durationMs as number | undefined}
        />
      );

    case "tool_call": {
      return (
        <Box alignSelf="flex-start" w="100%">
          <ToolCall
            name={message.content}
            arguments={message.meta?.arguments as Record<string, unknown> | undefined}
            result={message.meta?.result}
            sequenceNumber={message.meta?.sequenceNumber as number | undefined}
            status={toolStatus(message.meta?.status)}
            permission={message.meta?.permission as ToolPermission | undefined}
            agents={agents}
            onPermission={onPermission}
          />
        </Box>
      );
    }

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
