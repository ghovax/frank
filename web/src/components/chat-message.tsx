"use client";

import { Box, Button, Flex, Separator, Text } from "@chakra-ui/react";
import { useTranslations } from "next-intl";
import { memo, useLayoutEffect, useRef, useState } from "react";
import { LuFoldVertical, LuRotateCw, LuTriangleAlert } from "react-icons/lu";
import type { ChatMessage, MessageAttachment } from "@/lib/use-chat";
import type { ArtifactAnnotationRecord } from "@/lib/artifact-annotations";
import type { ToolEvent, ToolPermission, ToolQuestion } from "@/lib/tool-event";
import { toolStatus } from "@/lib/tool-event";
import { AttachmentChips, ArtifactAnnotationChips } from "./attachment-chips";
import { MarkdownContent } from "./markdown-content";
import { ToolCall } from "./tool-call";
import { ToolGroup } from "./tool-group";
import { ActivityIcon, ActivitySpinner } from "./ui/activity-icon";

interface ChatMessageProps {
  message: ChatMessage;
  activeArtifactId?: string | null;
  onActivateArtifact?: (id: string) => void;
  // Re-run the turn that produced a server error (resends the last user message).
  // Only wired for error rows.
  onRetry?: () => void;
  // This is the last row and the turn is live, so its assistant text is still streaming
  // in — drives the newly-arrived-token animation. Only ever true for one row.
  streaming?: boolean;
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

interface FriendlyWarning {
  code: string;
  title: string;
  message: string;
}

// A server/turn error rendered as its own distinct block — not disguised as an
// assistant message. A bordered danger box with the alert triangle, a semibold title,
// the message below as rendered markdown, and a "Try again" action, so the user
// reads it as a system failure with a clear next step rather than model prose.
function ErrorMessageCard({ message, onRetry }: { message: ChatMessage; onRetry?: () => void }) {
  const translation = useTranslations("ChatMessage");
  const error = message.meta?.error as FriendlyError | undefined;
  const title = error?.title?.trim() || translation("errorTitle");
  const body = error?.message?.trim() || message.content;
  return (
    <Box
      w="100%"
      maxW="640px"
      border="1px solid"
      borderColor="red.muted"
      bg="red.subtle"
      borderRadius="md"
      px={2.5}
      py={2}
    >
      <Flex align="center" gap={2} color="red.fg">
        <Box display="flex" alignItems="center" flexShrink={0}>
          <LuTriangleAlert size={15} />
        </Box>
        <Text textStyle="panelTitle" lineHeight="1.3">
          {title}
        </Text>
      </Flex>
      <Box mt={1.5}>
        <MarkdownContent content={body} fontSize="sm" />
      </Box>
      {onRetry && (
        <Flex mt={2.5}>
          <Button
            variant="solid"
            colorPalette="red"
            fontWeight="medium"
            onClick={onRetry}
          >
            <LuRotateCw size={13} />
            {translation("tryAgain")}
          </Button>
        </Flex>
      )}
    </Box>
  );
}

function WarningMessageCard({ message }: { message: ChatMessage }) {
  const translation = useTranslations("ChatMessage");
  const warning = message.meta?.warning as FriendlyWarning | undefined;
  const title = warning?.title?.trim() || translation("warningTitle");
  const body = warning?.message?.trim() || message.content;
  return (
    <Box
      w="100%"
      maxW="640px"
      border="1px solid"
      borderColor="orange.muted"
      bg="orange.subtle"
      borderRadius="md"
      px={2.5}
      py={2}
    >
      <Flex align="center" gap={2} color="orange.fg">
        <Box display="flex" alignItems="center" flexShrink={0}>
          <LuTriangleAlert size={15} />
        </Box>
        <Text textStyle="panelTitle" lineHeight="1.3">
          {title}
        </Text>
      </Flex>
      <Box mt={1.5}>
        <MarkdownContent content={body} fontSize="sm" />
      </Box>
    </Box>
  );
}

function ToolMessageCard({ message, activeArtifactId, onActivateArtifact }: ChatMessageProps) {
  return (
    <ToolCall
      name={message.content}
      arguments={message.meta?.arguments as Record<string, unknown> | undefined}
      result={message.meta?.result}
      status={toolStatus(message.meta?.status)}
      permission={message.meta?.permission as ToolPermission | undefined}
      question={message.meta?.question as ToolQuestion | undefined}
      toolCallId={message.meta?.toolCallId as string | undefined}
      activeArtifactId={activeArtifactId}
      onActivateArtifact={onActivateArtifact}
    />
  );
}

function UserMessageCard({ message }: { message: ChatMessage }) {
  const translation = useTranslations("ChatMessage");
  const attachments = (message.meta?.attachments as MessageAttachment[] | undefined) ?? [];
  const artifactAnnotationRecords = (message.meta?.artifactAnnotationRecords as ArtifactAnnotationRecord[] | undefined) ?? [];
  const contentRef = useRef<HTMLDivElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [truncatable, setTruncatable] = useState(false);
  const COLLAPSE_HEIGHT = 200;

  useLayoutEffect(() => {
    const element = contentRef.current;
    if (!element) return;
    setTruncatable(element.scrollHeight > COLLAPSE_HEIGHT);
  }, [message.content]);

  return (
    <Flex direction="column" alignSelf="flex-end" align="flex-end" gap={1.5} maxW="80%">
      {attachments.length > 0 && <AttachmentChips attachments={attachments} />}
      {artifactAnnotationRecords.length > 0 && <ArtifactAnnotationChips records={artifactAnnotationRecords} />}
      {message.content.trim() && (
        <Box
          ref={contentRef}
          minW={0}
          position="relative"
          overflow="hidden"
          maxH={expanded ? "none" : `${COLLAPSE_HEIGHT}px`}
          bg="bg.muted"
          border="1px solid"
          borderColor="border"
          px={2.5}
          py={1.5}
          borderRadius="md"
          maxW="100%"
        >
          <MarkdownContent content={message.content} />
          {!expanded && truncatable && (
            <Box
              position="absolute"
              bottom={0}
              left={0}
              right={0}
              h={12}
              pointerEvents="none"
              css={{
                backgroundImage: "linear-gradient(to top, var(--chakra-colors-bg-muted), transparent)",
              }}
            />
          )}
        </Box>
      )}
      {truncatable && (
        <Button
          variant="ghost"
          colorPalette="blue"
          fontWeight="medium"
          onClick={() => setExpanded((current) => !current)}
        >
          {expanded ? translation("showLess") : translation("showMore")}
        </Button>
      )}
    </Flex>
  );
}

export const ChatMessageItem = memo(function ChatMessageItem({ message, activeArtifactId, onActivateArtifact, onRetry, streaming = false }: ChatMessageProps) {
  const translation = useTranslations("ChatMessage");
  switch (message.role) {
    case "user": {
      return <UserMessageCard message={message} />;
    }

    case "assistant": {
      if (!message.contentBlocks) {
        throw new Error("Assistant messages require structured content blocks.");
      }
      const contentBlocks = message.contentBlocks.filter((contentBlock) => contentBlock.content.trim());
      if (contentBlocks.length === 0) return null;
      return (
        // No horizontal inset: the assistant's prose shares the same left edge as the
        // tool-activity lines (which have none), so text and tools line up. A stray px
        // here pushed the markdown ~4px inward of them.
        <Box alignSelf="flex-start">
          <Flex direction="column" gap={3}>
            {contentBlocks.map((contentBlock, contentBlockIndex) => (
              <MarkdownContent
                key={contentBlock.identifier}
                content={contentBlock.content}
                animate={streaming && contentBlockIndex === contentBlocks.length - 1}
              />
            ))}
          </Flex>
        </Box>
      );
    }

    // "thinking" is never rendered as a transcript row — reasoning is surfaced as
    // a compact live status instead (see ChatPanel / ToolGroup). Any thinking
    // message that reaches here falls through to the default no-op.

    case "tool_call": {
      return (
        <Box alignSelf="flex-start" w="100%">
          <ToolMessageCard message={message} activeArtifactId={activeArtifactId} onActivateArtifact={onActivateArtifact} />
        </Box>
      );
    }

    case "error":
      // A turn failure is a system event, not the model's words — so it renders as
      // its own dedicated error box (danger triangle, semibold title, message, retry),
      // never as assistant-style prose with a hand-aligned inline icon.
      return (
        <Box alignSelf="flex-start" w="100%">
          <ErrorMessageCard message={message} onRetry={onRetry} />
        </Box>
      );

    case "warning":
      return (
        <Box alignSelf="flex-start" w="100%">
          <WarningMessageCard message={message} />
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
            <Separator flex={1} />
            <Flex
              align="center"
              gap={1.5}
              flexShrink={0}
              color={running ? "blue.fg" : undefined}
              title={running || !before ? undefined : translation("compactedTooltip", { before, after })}
            >
              <ActivityIcon>
                {running ? <ActivitySpinner /> : <LuFoldVertical />}
              </ActivityIcon>
              <Text textStyle="fieldLabel" className={running ? "running-title-shimmer" : undefined}>
                {running ? translation("compactingContext") : translation("contextCompacted")}
              </Text>
            </Flex>
            <Separator flex={1} />
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
  activeArtifactId?: string | null;
  onActivateArtifact?: (id: string) => void;
  keepOpen?: boolean;
}

export const ChatToolGroup = memo(function ChatToolGroup({ messages, activeArtifactId, onActivateArtifact, keepOpen }: ChatToolGroupProps) {
  // Map the persisted tool-call messages to the ToolEvent shape the shared
  // ToolGroup renders.
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
      activeArtifactId={activeArtifactId}
      onActivateArtifact={onActivateArtifact}
      keepOpen={keepOpen}
    />
  );
});
