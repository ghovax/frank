"use client";

import { Box, Text } from "@chakra-ui/react";
import { memo } from "react";
import type { ChatMessage } from "@/lib/use-chat";
import type { PermissionDecision, QuestionAnswer, ToolEvent, ToolEventStatus, ToolPermission, ToolQuestion } from "@/lib/tool-event";
import { MarkdownContent } from "./markdown-content";
import { ToolCall } from "./tool-call";
import { ToolGroup } from "./tool-group";

interface ChatMessageProps {
  message: ChatMessage;
  onPermission?: (requestId: string, decision: PermissionDecision) => void;
  onQuestion?: (requestId: string, answers: QuestionAnswer[]) => void;
  agents?: { id: string; name: string }[];
  activePreviewId?: string | null;
  onActivatePreview?: (id: string) => void;
}

function toolStatus(status: unknown): ToolEventStatus | undefined {
  return status === "running" || status === "completed" || status === "done" || status === "failed" || status === "input_required" ? status : undefined;
}

function ToolMessageCard({ message, onPermission, onQuestion, agents = [], activePreviewId, onActivatePreview }: ChatMessageProps) {
  return (
    <ToolCall
      name={message.content}
      arguments={message.meta?.arguments as Record<string, unknown> | undefined}
      result={message.meta?.result}
      status={toolStatus(message.meta?.status)}
      permission={message.meta?.permission as ToolPermission | undefined}
      question={message.meta?.question as ToolQuestion | undefined}
      toolCallId={message.meta?.toolCallId as string | undefined}
      agents={agents}
      onPermission={onPermission}
      onQuestion={onQuestion}
      activePreviewId={activePreviewId}
      onActivatePreview={onActivatePreview}
    />
  );
}

export const ChatMessageItem = memo(function ChatMessageItem({ message, onPermission, onQuestion, agents = [], activePreviewId, onActivatePreview }: ChatMessageProps) {
  switch (message.role) {
    case "user":
      return (
        <Box alignSelf="flex-end" bg="bg.muted" border="1px solid" borderColor="border" px={2} py={1.5} borderRadius="sm" maxW="80%">
          <MarkdownContent content={message.content} />
        </Box>
      );

    case "assistant":
      if (!message.content) return null;
      return (
        <Box alignSelf="flex-start" px={1}>
          <MarkdownContent content={message.content} />
        </Box>
      );

    // "thinking" is never rendered as a transcript row — reasoning is surfaced as
    // a compact live status instead (see ChatPanel / ToolGroup). Any thinking
    // message that reaches here falls through to the default no-op.

    case "tool_call": {
      return (
        <Box alignSelf="flex-start" w="100%">
          <ToolMessageCard message={message} onPermission={onPermission} onQuestion={onQuestion} agents={agents} activePreviewId={activePreviewId} onActivatePreview={onActivatePreview} />
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
});

interface ChatToolGroupProps {
  messages: ChatMessage[];
  onPermission?: (requestId: string, decision: PermissionDecision) => void;
  onQuestion?: (requestId: string, answers: QuestionAnswer[]) => void;
  agents?: { id: string; name: string }[];
  activePreviewId?: string | null;
  onActivatePreview?: (id: string) => void;
  keepOpen?: boolean;
}

export const ChatToolGroup = memo(function ChatToolGroup({ messages, onPermission, onQuestion, agents = [], activePreviewId, onActivatePreview, keepOpen }: ChatToolGroupProps) {
  // Map the persisted tool-call messages to the ToolEvent shape the shared
  // ToolGroup renders, so the chat timeline and the agents panel stay in lockstep.
  const tools: ToolEvent[] = messages.map((message) => ({
    name: message.content,
    arguments: message.meta?.arguments as Record<string, unknown> | undefined,
    toolCallId: String(message.meta?.toolCallId ?? ""),
    result: message.meta?.result,
    status: toolStatus(message.meta?.status),
    permission: message.meta?.permission as ToolPermission | undefined,
    question: message.meta?.question as ToolQuestion | undefined,
  }));
  return (
    <ToolGroup
      tools={tools}
      agents={agents}
      onPermission={onPermission}
      onQuestion={onQuestion}
      activePreviewId={activePreviewId}
      onActivatePreview={onActivatePreview}
      keepOpen={keepOpen}
    />
  );
});
