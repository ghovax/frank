"use client";

import { Badge, Box, Flex, Text } from "@chakra-ui/react";
import { useState } from "react";
import { LuChevronDown, LuChevronRight, LuWrench } from "react-icons/lu";
import type { ChatMessage } from "@/lib/use-chat";
import type { PermissionDecision, QuestionAnswer, ToolEventStatus, ToolPermission, ToolQuestion } from "@/lib/tool-event";
import { MarkdownContent } from "./markdown-content";
import { ToolCall } from "./tool-call";
import { ThinkingIndicator } from "./thinking-indicator";

interface ChatMessageProps {
  message: ChatMessage;
  onPermission?: (requestId: string, decision: PermissionDecision) => void;
  onQuestion?: (requestId: string, answers: QuestionAnswer[]) => void;
  agents?: { id: string; name: string }[];
}

function toolStatus(status: unknown): ToolEventStatus | undefined {
  return status === "running" || status === "completed" || status === "done" || status === "failed" || status === "input_required" ? status : undefined;
}

function ToolMessageCard({ message, onPermission, onQuestion, agents = [] }: ChatMessageProps) {
  return (
    <ToolCall
      name={message.content}
      arguments={message.meta?.arguments as Record<string, unknown> | undefined}
      result={message.meta?.result}
      sequenceNumber={message.meta?.sequenceNumber as number | undefined}
      status={toolStatus(message.meta?.status)}
      permission={message.meta?.permission as ToolPermission | undefined}
      question={message.meta?.question as ToolQuestion | undefined}
      agents={agents}
      onPermission={onPermission}
      onQuestion={onQuestion}
    />
  );
}

export function ChatMessageItem({ message, onPermission, onQuestion, agents = [] }: ChatMessageProps) {
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

    case "thinking": {
      const thinkingStatus = message.meta?.status as string | undefined;
      // Always render the reasoning phase, even when it captured no body text —
      // the card ("Thinking" → "Thought for Ns") is the persistent marker for
      // that phase. Models that don't stream reasoning_content still emit the
      // phase boundary, and dropping the bodyless card made the indicator
      // vanish the moment the phase finished. Never filter it out.
      return (
        <ThinkingIndicator
          content={message.content}
          status={thinkingStatus}
          durationMs={message.meta?.durationMs as number | undefined}
        />
      );
    }

    case "tool_call": {
      return (
        <Box alignSelf="flex-start" w="100%" className="timeline-item">
          <ToolMessageCard message={message} onPermission={onPermission} onQuestion={onQuestion} agents={agents} />
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

interface ChatToolGroupProps {
  messages: ChatMessage[];
  onPermission?: (requestId: string, decision: PermissionDecision) => void;
  onQuestion?: (requestId: string, answers: QuestionAnswer[]) => void;
  agents?: { id: string; name: string }[];
}

export function ChatToolGroup({ messages, onPermission, onQuestion, agents = [] }: ChatToolGroupProps) {
  const runningCount = messages.filter((message) => toolStatus(message.meta?.status) === "running").length;
  const inputRequired = messages.some((message) => toolStatus(message.meta?.status) === "input_required");
  const failedCount = messages.filter((message) => toolStatus(message.meta?.status) === "failed").length;
  const active = runningCount > 0 || inputRequired;
  const [open, setOpen] = useState(active);
  const bodyOpen = active || open;

  const badge = inputRequired
    ? { label: "Input required", colorPalette: "yellow" }
    : failedCount > 0
      ? { label: `${failedCount} failed`, colorPalette: "red" }
      : runningCount > 0
        ? { label: `${runningCount} running`, colorPalette: "blue" }
        : { label: "Completed", colorPalette: "green" };

  return (
    <Box alignSelf="flex-start" w="100%" className="timeline-item">
      <Box borderLeft="2px solid" borderColor={active ? "blue.muted" : "border"} pl={2}>
        <Flex
          as="button"
          align="center"
          gap={1.5}
          w="100%"
          px={1}
          py={1.5}
          color="fg.muted"
          textAlign="left"
          cursor="pointer"
          borderRadius="sm"
          _hover={{ bg: "bg.muted" }}
          onClick={() => setOpen((current) => !current)}
        >
          <Box color={active ? "blue.fg" : "fg.muted"} flexShrink={0}>
            <LuWrench size={13} />
          </Box>
          <Text fontSize="xs" fontWeight="medium" flex={1} truncate className={active ? "running-title-shimmer" : undefined}>
            {messages.length} tool calls
          </Text>
          <Badge size="sm" variant="subtle" colorPalette={badge.colorPalette} borderRadius="sm" flexShrink={0}>
            {badge.label}
          </Badge>
          <Box flexShrink={0}>
            {bodyOpen ? <LuChevronDown size={13} /> : <LuChevronRight size={13} />}
          </Box>
        </Flex>
        {bodyOpen && (
          <Flex direction="column" gap={1.5} pt={1} pb={1.5}>
            {messages.map((message) => (
              <ToolMessageCard
                key={message.id}
                message={message}
                onPermission={onPermission}
                onQuestion={onQuestion}
                agents={agents}
              />
            ))}
          </Flex>
        )}
      </Box>
    </Box>
  );
}
