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

/**
 * A message the session has not taken yet: what it is waiting behind, and what can be done
 * about it.
 *
 * The words come from the panel rather than from here. A wait is a fact about the *queue* —
 * "waiting for your decision" is about a permission prompt three rows up, not about this
 * message — and the queue is the panel's to describe.
 */
export interface QueuedMessageState {
  /** The wait, in words. Empty while the message is actually being handed over, which is not a
   * wait: saying "queued" about the few milliseconds of an ordinary send reports a fault that
   * is not happening. */
  status: string;
  /** The wait is a failure to reach the session rather than a place in a line. */
  failed?: boolean;
  onDelete: () => void;
  /** Offered only for the head of a queue that could not be delivered. */
  onRetry?: () => void;
  retryLabel?: string;
}

/**
 * What is under a message you sent: when it was sent, and the small things you can do to it.
 *
 * These live here, on the message, rather than beside it. The delete control used to sit in the
 * gutter to the left of a *queued* message and nowhere else, so it took horizontal room that the
 * message did not have once it was accepted — every message you sent jumped sideways the moment
 * the session took it. Anything drawn beside a message is room the message loses; anything drawn
 * under it is not, which is the whole reason this is a footer.
 *
 * On a pointer, the buttons are revealed by hovering the message — a transcript of your own
 * words with a row of controls under every one of them reads as a form, not a conversation. They
 * are always there, so nothing moves when they appear. A queued message keeps them visible
 * regardless, because one of them deletes and an invisible destructive control is a trap.
 */
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
      // A clipboard write the browser or the person declined. There is nothing to recover and
      // nothing worth interrupting them about.
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
      // The controls are laid out from the right, so the one nearest the message is the one
      // most likely to be wanted, and the row grows leftwards into empty space.
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
        // The time of day, with the whole instant behind it. A transcript is read in one
        // sitting and the day is almost always today, so the date would be four words of noise
        // on every message to be right about the few that are not.
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

// A message addressed to this session: the person's own, or one a peer sent it.
//
// One card for both. They are the same thing structurally — an inbound message that starts a
// turn — and a peer's used to have a card of its own that had drifted into a poorer version of
// this one: no attachments, no collapse for a long report, and a left border where everything
// else in the transcript uses a bubble. What actually distinguishes them is authorship, and a
// banner says that in words, which is both more legible than a different shape and the only part
// a reader needs. The sending session's id is not shown: it identifies a process, and nobody
// reading a conversation is addressing one.
//
// A message still in the queue is this card too, and that is the point of `queued`. It used to
// be a second, poorer rendering — a dashed box with plain text where this one renders markdown,
// and a delete button in the gutter beside it — so a message visibly changed shape and position
// the moment the session accepted it. One card cannot do that: what an undelivered message says
// differently, it says in the footer and in its border, neither of which occupies different room.
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
          // Dashed and a shade back while it is still ours to withdraw. Same border width and
          // same padding as a delivered one, so the difference is only ever in the ink.
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
          <ToolMessageCard message={message} />
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
  keepOpen?: boolean;
}

export const ChatToolGroup = memo(function ChatToolGroup({ messages, keepOpen }: ChatToolGroupProps) {
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
      keepOpen={keepOpen}
    />
  );
});
