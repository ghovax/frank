"use client";

import { Box, Button, Flex, Separator, Span, Text } from "@chakra-ui/react";
import { useFormatter, useTranslations } from "next-intl";
import { memo, useEffect, useLayoutEffect, useRef, useState } from "react";
import { LuCheck, LuClock, LuCopy, LuFoldVertical, LuMessagesSquare, LuRotateCw, LuTrash2, LuTriangleAlert } from "react-icons/lu";
import { swallowed } from "@/lib/swallowed";
import type { ChatMessage, MessageAttachment } from "@/lib/use-chat";
import type { ToolEvent, ToolPermission, ToolQuestion } from "@/lib/tool-event";
import { toolStatus } from "@/lib/tool-event";
import { AttachmentChips } from "./attachment-chips";
import { MarkdownContent } from "./markdown-content";
import { ToolCall } from "./tool-call";
import { ToolGroup } from "./tool-group";
import { ActivityIcon, ActivitySpinner } from "./ui/activity-icon";

interface ChatMessageProps {
  message: ChatMessage;
  // Re-run the turn that produced a server error, wired only for error rows.
  onRetry?: () => void;
  // This is the last row of a live turn, which drives the newly-arrived-token animation.
  streaming?: boolean;
}

// The structured error category the server hands the interface, never the raw provider text.
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

// A server or turn error rendered as its own block rather than disguised as an assistant message.
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

function ToolMessageCard({ message }: ChatMessageProps) {
  return (
    <ToolCall
      name={message.content}
      arguments={message.meta?.arguments as Record<string, unknown> | undefined}
      result={message.meta?.result}
      status={toolStatus(message.meta?.status)}
      permission={message.meta?.permission as ToolPermission | undefined}
      question={message.meta?.question as ToolQuestion | undefined}
      toolCallId={message.meta?.toolCallId as string | undefined}
    />
  );
}

/** A message the session has not taken yet: what it is waiting behind, and what can be done about it. */
export interface QueuedMessageState {
  /** The wait in words, empty while the message is actually being handed over, which is not a wait. */
  status: string;
  /** The wait is a failure to reach the session rather than a place in a line. */
  failed?: boolean;
  onDelete: () => void;
  /** Offered only for the head of a queue that could not be delivered. */
  onRetry?: () => void;
  retryLabel?: string;
}

/** What is under a message you sent: when it was sent, and the small things you can do to it. */
function MessageFooter({ content, sentAt, queued }: { content: string; sentAt: string; queued?: QueuedMessageState }) {
  const translation = useTranslations("ChatMessage");
  const format = useFormatter();
  const [copied, setCopied] = useState(false);
  const copiedTimer = useRef<number | null>(null);

  useEffect(() => () => {
    if (copiedTimer.current !== null) window.clearTimeout(copiedTimer.current);
  }, []);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(content);
      setCopied(true);
      if (copiedTimer.current !== null) window.clearTimeout(copiedTimer.current);
      copiedTimer.current = window.setTimeout(() => setCopied(false), 1500);
    } catch (caught) {
      // A clipboard write the browser or the person declined, with nothing to recover.
      swallowed({ component: "chat-message", operation: "copy a message" }, caught);
    }
  };

  const sent = sentAt ? new Date(sentAt) : null;
  const dated = sent && !Number.isNaN(sent.getTime()) ? sent : null;

  return (
    <Flex
      className={queued ? undefined : "message-actions"}
      align="center"
      gap={1}
      color="fg.subtle"
      // The controls lay out from the right, so the nearest is the likeliest wanted and the row grows leftwards.
      justify="flex-end"
    >
      {queued ? (
        queued.status ? (
          <Flex align="center" gap={1.5} color={queued.failed ? "red.fg" : "fg.subtle"} pe={1}>
            <Span display="inline-flex" alignItems="center">
              {queued.failed ? <LuTriangleAlert size={11} /> : <LuClock size={11} />}
            </Span>
            <Text textStyle="fieldLabel" color={queued.failed ? "red.fg" : "fg.subtle"}>{queued.status}</Text>
          </Flex>
        ) : null
      ) : dated ? (
        // The time of day with the whole instant behind it, since a transcript is almost always read the same day.
        <Text
          textStyle="fieldLabel"
          pe={1}
          title={format.dateTime(dated, { year: "numeric", month: "long", day: "numeric", hour: "2-digit", minute: "2-digit" })}
        >
          {format.dateTime(dated, { hour: "numeric", minute: "numeric" })}
        </Text>
      ) : null}
      {queued?.onRetry && queued.retryLabel && (
        <Button size="2xs" variant="outline" onClick={queued.onRetry}>{queued.retryLabel}</Button>
      )}
      {content.trim() && (
        <Button size="2xs" variant="ghost" color="fg.subtle" onClick={copy}>
          {copied ? <LuCheck size={11} /> : <LuCopy size={11} />}
          {copied ? translation("copied") : translation("copy")}
        </Button>
      )}
      {queued && (
        <Button size="2xs" variant="ghost" colorPalette="red" onClick={queued.onDelete}>
          <LuTrash2 size={11} />
          {translation("deleteQueued")}
        </Button>
      )}
    </Flex>
  );
}

// A message addressed to this session, whether the person's own or one a peer sent, as one card.
export function UserMessageCard({
  message,
  banner = "",
  queued,
}: { message: ChatMessage; banner?: string; queued?: QueuedMessageState }) {
  const translation = useTranslations("ChatMessage");
  const attachments = (message.meta?.attachments as MessageAttachment[] | undefined) ?? [];
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
    <Flex className="message-row" direction="column" alignSelf="flex-end" align="flex-end" gap={1.5} maxW="80%">
      {banner && (
        <Flex align="center" gap={1.5} color="fg.muted">
          <ActivityIcon><LuMessagesSquare /></ActivityIcon>
          <Text fontSize="xs" fontWeight="medium">{banner}</Text>
        </Flex>
      )}
      {attachments.length > 0 && <AttachmentChips attachments={attachments} />}
      {message.content.trim() && (
        <Box
          ref={contentRef}
          minW={0}
          position="relative"
          overflow="hidden"
          maxH={expanded ? "none" : `${COLLAPSE_HEIGHT}px`}
          // Dashed and a shade back while it is still ours to withdraw, differing only in the ink.
          bg={queued ? "bg.subtle" : "bg.muted"}
          border="1px solid"
          borderStyle={queued ? "dashed" : "solid"}
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
                backgroundImage: `linear-gradient(to top, var(--chakra-colors-${queued ? "bg-subtle" : "bg-muted"}), transparent)`,
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
      <MessageFooter content={message.content} sentAt={message.timestamp} queued={queued} />
    </Flex>
  );
}

export const ChatMessageItem = memo(function ChatMessageItem({ message, onRetry, streaming = false }: ChatMessageProps) {
  const translation = useTranslations("ChatMessage");
  switch (message.role) {
    case "user": {
      return <UserMessageCard message={message} />;
    }

    case "peer": {
      return <UserMessageCard message={message} banner={translation("fromPeerSession")} />;
    }

    case "assistant": {
      if (!message.contentBlocks) {
        throw new Error("Assistant messages require structured content blocks.");
      }
      const contentBlocks = message.contentBlocks.filter((contentBlock) => contentBlock.content.trim());
      if (contentBlocks.length === 0) return null;
      return (
        // No horizontal inset, so the prose shares its left edge with the tool-activity lines.
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

    // Reasoning is surfaced as a compact live status rather than as a transcript row.

    case "tool_call": {
      return (
        <Box alignSelf="flex-start" w="100%">
          <ToolMessageCard message={message} />
        </Box>
      );
    }

    case "error":
      // A turn failure is a system event rather than the model's words, so it renders as its own error box.
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
      // A full-width divider marking where earlier context was summarized away.
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
  keepOpen?: boolean;
}

export const ChatToolGroup = memo(function ChatToolGroup({ messages, keepOpen }: ChatToolGroupProps) {
  // Map the persisted tool-call messages to the shape the shared group renders.
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
      keepOpen={keepOpen}
    />
  );
});
