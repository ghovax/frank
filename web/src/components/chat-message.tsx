"use client";

import { Box, Button, Flex, Text } from "@chakra-ui/react";
import { memo } from "react";
import { LuFoldVertical, LuRotateCw, LuTriangleAlert } from "react-icons/lu";
import type { ChatMessage, MessageAttachment } from "@/lib/use-chat";
import type { PermissionDecision, QuestionAnswer, ToolEvent, ToolEventStatus, ToolPermission, ToolQuestion } from "@/lib/tool-event";
import { AttachmentChips } from "./attachment-chips";
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
  // Re-run the turn that produced a server error (resends the last user message).
  // Only wired for error rows.
  onRetry?: () => void;
}

// The structured, safe error category the server hands the UI for a failed turn
// (a rejected request, a rate limit, a provider outage). Never the raw provider
// text — only a title + user-actionable message.
interface FriendlyError {
  code: string;
  title: string;
  message: string;
  status?: number;
}

// A server/turn error rendered as its own distinct block — not disguised as an
// assistant message. A bordered danger box with the alert triangle, a bold title,
// the message below as rendered markdown, and a "Try again" action, so the user
// reads it as a system failure with a clear next step rather than model prose.
function ErrorMessageCard({ message, onRetry }: { message: ChatMessage; onRetry?: () => void }) {
  const error = message.meta?.error as FriendlyError | undefined;
  const title = error?.title?.trim() || "Something went wrong";
  const body = error?.message?.trim() || message.content;
  return (
    <Box
      w="100%"
      maxW="640px"
      border="1px solid"
      borderColor="red.muted"
      bg="red.subtle"
      borderRadius="md"
      px={3}
      py={2.5}
    >
      <Flex align="center" gap={2} color="red.fg">
        <Box display="flex" alignItems="center" flexShrink={0}>
          <LuTriangleAlert size={15} />
        </Box>
        <Text fontSize="sm" fontWeight="bold" lineHeight="1.3">
          {title}
        </Text>
      </Flex>
      <Box mt={1.5}>
        <MarkdownContent content={body} fontSize="sm" />
      </Box>
      {onRetry && (
        <Flex mt={2.5}>
          <Button
            size="xs"
            variant="solid"
            colorPalette="red"
            borderRadius="sm"
            fontWeight="medium"
            onClick={onRetry}
          >
            <LuRotateCw size={13} />
            Try again
          </Button>
        </Flex>
      )}
    </Box>
  );
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

export const ChatMessageItem = memo(function ChatMessageItem({ message, onPermission, onQuestion, agents = [], activePreviewId, onActivatePreview, onRetry }: ChatMessageProps) {
  switch (message.role) {
    case "user": {
      const attachments = (message.meta?.attachments as MessageAttachment[] | undefined) ?? [];
      return (
        <Flex direction="column" alignSelf="flex-end" align="flex-end" gap={1.5} maxW="80%">
          {attachments.length > 0 && <AttachmentChips attachments={attachments} />}
          {message.content.trim() && (
            <Box bg="bg.muted" border="1px solid" borderColor="border" px={2} py={1.5} borderRadius="sm" maxW="100%">
              <MarkdownContent content={message.content} />
            </Box>
          )}
        </Flex>
      );
    }

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
      // A turn failure is a system event, not the model's words — so it renders as
      // its own dedicated error box (danger triangle, bold title, message, retry),
      // never as assistant-style prose with a hand-aligned inline icon.
      return (
        <Box alignSelf="flex-start" w="100%">
          <ErrorMessageCard message={message} onRetry={onRetry} />
        </Box>
      );

    case "compaction": {
      // A full-width divider marking where the earlier context was summarized away.
      // "running" is the live "Compacting…" state; "done" is the settled separator.
      const running = message.meta?.status === "running";
      const before = Number(message.meta?.messagesBefore ?? 0);
      const after = Number(message.meta?.messagesAfter ?? 0);
      return (
        <Box alignSelf="stretch" w="100%">
          <Flex align="center" gap={3} py={1} color="fg.subtle">
            <Box flex={1} h="1px" bg="border" />
            <Flex
              align="center"
              gap={1.5}
              flexShrink={0}
              title={running || !before ? undefined : `Compacted ${before} messages down to ${after}`}
            >
              <Box display="flex" alignItems="center">
                <LuFoldVertical size={12} />
              </Box>
              <Text fontSize="xs" fontWeight="medium" className={running ? "running-title-shimmer" : undefined}>
                {running ? "Compacting context…" : "Context compacted"}
              </Text>
            </Flex>
            <Box flex={1} h="1px" bg="border" />
          </Flex>
        </Box>
      );
    }

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
