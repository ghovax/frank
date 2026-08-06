"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  cancelTurn,
  abortToolCall,
  attachSession,
  compactSession,
  messageParts,
  resolvePermission,
  resolveQuestion,
  fetchSessionTurns,
  fetchSessionTurnsPage,
  sessionCreate,
  sessionSend,
  CONTENT_BLOCK_METADATA_KEY,
  METADATA_KEY,
  partPayload,
  turnState,
  type A2AMessage,
  type A2APart,
  type A2ATurn as A2ATurnWire,
  type PermissionMode,
  type WorktreeStrategy,
} from "./api";
import { isSameToolEvent, type QuestionAnswer, type QuestionItem, type ToolEvent, type ToolEventStatus, type ToolPermission, type ToolQuestion } from "./tool-event";
import { toaster } from "@/components/ui/toaster";
import { swallowed } from "@/lib/swallowed";
import { useTranslations } from "next-intl";
import { asArray, asRecord } from "@/lib/coerce";
import type { PrefixDivergence, WireEvent } from "@shared/generated/events";
import { errorMessage } from "@/lib/errors";
import { clientIdentifier } from "@/lib/identifier";
import { Outbox, type Delivery, type OutboxHold, type OutboxMessage } from "@/lib/outbox";

// Re-export the A2A task shape so components can consume it from one place.
export type A2ATurn = A2ATurnWire;

export type TaskState =
  | "submitted"
  | "working"
  | "input-required"
  | "completed"
  | "canceled"
  | "failed"
  | "rejected"
  | "auth-required"
  | "unknown";

const TERMINAL_STATES: ReadonlySet<TaskState> = new Set([
  "completed",
  "canceled",
  "failed",
  "rejected",
]);

const HISTORY_PAGE_LIMIT = 500;

// A file the user attached to a turn — the metadata needed to render a chip and show it (name, on-disk path, mime, size).
export interface MessageAttachment {
  filename: string;
  path: string;
  mimeType: string;
  size: number;
}

// The per-message side data the reducers attach and the views read.
export interface MessageMeta {
  arguments?: Record<string, unknown>;
  toolCallId?: string;
  // Spans tool lifecycle and the compaction/thinking indicators, so it is the wider string set.
  status?: string;
  result?: unknown;
  permission?: ToolPermission;
  question?: ToolQuestion;
  error?: FriendlyError;
  warning?: { code: string; title: string; message: string };
  reason?: string;
  messagesBefore?: number;
  messagesAfter?: number;
  durationMs?: number;
  attachments?: MessageAttachment[];
  // On a `peer` message: which session sent it.
  peerSender?: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "peer" | "assistant" | "tool_call" | "thinking" | "error" | "warning" | "compaction";
  content: string;
  timestamp: string;
  meta?: MessageMeta;
  contentBlocks?: Array<{ identifier: string; content: string }>;
}

export interface ChatTask {
  identifier: string;
  description: string;
  status: string;
  dependencies: string[];
}

// Running token totals for the session, summed from the real per-call usage the model reports.
export interface TokenUsage {
  // Cumulative session totals — the running spend, shown only in the tooltip.
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
  cacheReadTokens: number;
  // What a cache could have returned this session — the denominator `cacheReadTokens` means something against.
  cacheReachableTokens: number;
  reasoningTokens: number;
  modelCalls: number;
  // Current context occupancy — the latest call's prompt (system + history + turn) plus the reply it generated (completion + reasoning).
  contextTokens: number; // contextInputTokens + contextOutputTokens
  contextInputTokens: number;
  contextOutputTokens: number;
  contextWindow: number;
  // What the latest call's cache actually did, and why.
  contextCacheReadTokens: number;
  reachableTokens: number;
  prefixIntact: boolean;
  divergence: PrefixDivergence | null;
}

// A turn's input: typed prose plus any structured payloads, which travel as DataParts so the agent receives them as JSON rather than as prose.
export type ChatInput = { kind: "text"; text: string; dataParts?: Record<string, unknown>[] };

// What the composer is still holding.
export type QueuedMessage = OutboxMessage;

// The explicit ToolStatus from the wire mapped to the UI lifecycle.
function statusFromWire(wireStatus: unknown): ToolEventStatus {
  switch (wireStatus) {
    case "ok":
      return "completed";
    case "error":
      return "failed";
    case "running":
      return "running";
    default:
      throw new Error(`Invalid tool result status: ${String(wireStatus)}`);
  }
}

function requiredContentBlockIdentifier(metadata: Record<string, unknown> | undefined): string {
  const extension = asRecord(metadata?.[CONTENT_BLOCK_METADATA_KEY]);
  const identifier = String(extension.id ?? "");
  if (!identifier) throw new Error("Assistant text is missing its content-block identity.");
  return identifier;
}

interface FriendlyError {
  code: string;
  title: string;
  message: string;
  status?: number;
}

function friendlyErrorFromData(data: Record<string, unknown>): FriendlyError {
  const code = String(data.code ?? "turn_failed");
  const status = typeof data.status === "number" ? data.status : undefined;
  const fallback = (() => {
    switch (code) {
      case "provider_unavailable":
        return { title: "Model temporarily unavailable", message: "The agent's model provider is temporarily unavailable. Try again in a moment or configure a different model for this agent." };
      case "rate_limited":
        return { title: "Model rate limit reached", message: "The agent's provider is rate limiting requests. Wait a bit or configure another model for this agent." };
      case "authentication_failed":
        return { title: "Provider credentials need attention", message: "The agent's provider rejected the configured credentials. Check the API key or configure another model for this agent." };
      case "connection_failed":
      case "network_error":
        return { title: "Connection interrupted", message: "The model connection dropped before the turn finished. Check the connection and retry." };
      case "server_error":
        return { title: "Server request failed", message: "Frank could not start the turn. Check the daemon log and try again." };
      // The daemon sends the numbers it has (the window, the model) in `message`, so this is only the fallback for when it could not name them.
      case "context_window_exceeded":
      case "request_too_large":
        return { title: "Conversation is too long for this model", message: "The request was larger than this model's context window. Compact the conversation, start a new one, or switch to a model with a larger window. A single tool result — a long file or a screen listing — is the usual cause." };
      case "request_rejected":
        return { title: "Model rejected the request", message: "The agent's model could not accept this turn. Adjust the request or configure a different model for this agent." };
      case "image_unsupported":
        return { title: "This model can't read images", message: "The agent's model rejected the attached image — it looks like a text-only model. Configure a vision-capable model for this agent and try again." };
      default:
        return { title: "Turn could not complete", message: "The turn stopped unexpectedly. The raw details were written to the server log." };
    }
  })();
  return {
    code,
    status,
    title: String(data.title ?? fallback.title),
    message: String(data.message ?? fallback.message),
  };
}

// A turn-level failure worth a toast.
function friendlyErrorFromPart(part: A2APart | undefined): FriendlyError | null {
  if (!part || part.kind !== "data") return null;
  const payload = partPayload(part.data);
  // A `tool_call_id` marks a failure that belongs to one tool card, which renders in place; only a turn-level error becomes a toast.
  if (payload.kind !== "error" || payload.tool_call_id) return null;
  return friendlyErrorFromData(payload);
}

function pushErrorMessage(state: ReduceState, error: FriendlyError, sourceId?: string): void {
  state.messages = [
    ...state.messages,
    {
      id: stableMessageId(state, "error", sourceId),
      role: "error",
      content: `${error.title} — ${error.message}`,
      timestamp: new Date().toISOString(),
      meta: { error },
    },
  ];
}

function streamedMcpResult(data: Record<string, unknown>): Record<string, unknown> {
  const event = asRecord(data.event);
  return {
    server: data.server ?? event.server,
    tool: data.tool ?? event.tool,
    event: event.event,
    payload: event.payload,
    progress: event.progress,
    total: event.total,
    message: event.message,
  };
}

// Each streamed notification is appended to `events` so the card shows the server's progress as it arrives, while the latest values stay at the top level.
function mergeMcpResult(existing: unknown, streamed: Record<string, unknown>): Record<string, unknown> {
  const current = asRecord(existing);
  const events = Array.isArray(current.events) ? current.events : [];
  return {
    ...current,
    ...streamed,
    events: [...events, streamed],
  };
}

// The tool's own return value replaces the streamed state, but keeps the notification log that only the stream carried.
function mergeMcpFinalResult(existing: unknown, finalResult: unknown): unknown {
  const current = asRecord(existing);
  const finalRecord = asRecord(finalResult);
  if (Object.keys(finalRecord).length === 0) return finalResult;
  if (!Array.isArray(current.events)) return finalResult;
  return { ...finalRecord, events: current.events };
}


// A2A stream reduction — turn agent message parts into chat UI state.

interface ReduceState {
  messages: ChatMessage[];
  tasks: ChatTask[];
  tokenUsage: TokenUsage | null; // latest cumulative token totals, if any reported
  // Per-source occurrence counter so every rendered message gets a key derived from the stable server messageId (not its array position).
  keyCounts: Map<string, number>;
}

// Whether a replayed transcript would render exactly what is already on screen.
function rendersIdentically(current: ChatMessage[], replayed: ChatMessage[]): boolean {
  if (current.length !== replayed.length) return false;
  for (let index = 0; index < current.length; index += 1) {
    if (current[index].role !== replayed[index].role) return false;
    if (current[index].content !== replayed[index].content) return false;
  }
  for (let index = 0; index < current.length; index += 1) {
    if (JSON.stringify(current[index].meta ?? null) !== JSON.stringify(replayed[index].meta ?? null)) return false;
  }
  return true;
}

function newReduceState(): ReduceState {
  return {
    messages: [],
    tasks: [],
    tokenUsage: null,
    keyCounts: new Map(),
  };
}

// A stable, position-independent id for a rendered message.
function toolCallMessageId(toolCallId: string | undefined): string {
  return `toolcall-${toolCallId || clientIdentifier()}`;
}

function stableMessageId(state: ReduceState, prefix: string, sourceId: string | undefined): string {
  if (!sourceId) {
    // A monotonic counter, not `state.messages.length`.
    const issued = state.keyCounts.get("") ?? 0;
    state.keyCounts.set("", issued + 1);
    return `${prefix}-anon-${issued}`;
  }
  // A message that has an id *is* that id, and the row is the same row every time it is reduced.
  return `${prefix}-${sourceId}`;
}

function upsertMessage(state: ReduceState, message: ChatMessage): void {
  // Replace in place when the row already exists, append when it does not.
  const index = state.messages.findIndex((existing) => existing.id === message.id);
  if (index === -1) {
    state.messages = [...state.messages, message];
    return;
  }
  state.messages = state.messages.map((existing, position) =>
    position === index ? { ...existing, ...message, meta: { ...existing.meta, ...message.meta } } : existing
  );
}

function asChatTask(value: unknown): ChatTask | null {
  const record = asRecord(value);
  const identifier = String(record.identifier ?? "");
  const description = String(record.description ?? "");
  if (!identifier || !description) return null;
  return {
    identifier,
    description,
    status: String(record.status ?? "pending"),
    dependencies: Array.isArray(record.dependencies) ? record.dependencies.map(String) : [],
  };
}

function mergeTasks(current: ChatTask[], updates: unknown[]): ChatTask[] {
  const next = [...current];
  for (const raw of updates) {
    const task = asChatTask(raw);
    if (!task) continue;
    const index = next.findIndex((item) => item.identifier === task.identifier);
    if (index === -1) next.push(task);
    else next[index] = { ...next[index], ...task };
  }
  return next;
}

function isRunningThinkingMessage(message: ChatMessage): boolean {
  return message.role === "thinking" && message.meta?.status === "running";
}

function finishRunningThinking(state: ReduceState): void {
  state.messages = state.messages.map((message) =>
    isRunningThinkingMessage(message)
      ? { ...message, meta: { ...message.meta, status: "done" } }
      : message
  );
}

// Close the in-flight thinking message and record the server-measured duration, so the indicator flips from "Thinking" to "Thought for Ns".
function finishRunningThinkingWithDuration(state: ReduceState, durationMs: number): void {
  state.messages = state.messages.map((message) =>
    isRunningThinkingMessage(message)
      ? { ...message, meta: { ...message.meta, status: "done", durationMs } }
      : message
  );
}

// Open the "Thinking" row the moment a turn is sent, before anything comes back.
function finishActiveTools(state: ReduceState): void {
  state.messages = state.messages.map((message) =>
    message.role === "tool_call" && message.meta?.status === "running"
      ? { ...message, meta: { ...message.meta, status: "completed" } }
      : message
  );
}

// The single path for the thinking signal — the iteration-start ping and any streamed reasoning.
function applyThinking(state: ReduceState, text: string): void {
  let index = state.messages.findLastIndex(isRunningThinkingMessage);
  if (index === -1) {
    state.messages = [
      ...state.messages,
      { id: `status-${state.messages.length}`, role: "thinking", content: "", timestamp: new Date().toISOString(), meta: { status: "running" } },
    ];
    index = state.messages.length - 1;
  }
  // The session is reasoning, so a row the client opened optimistically is now that reasoning phase and stops being provisional — even for a bare ping with no text yet.
  state.messages = state.messages.map((message, messageIndex) =>
    messageIndex === index
      ? { ...message, content: message.content + text }
      : message
  );
}

function hasAssistantTextAfterLastUser(state: ReduceState): boolean {
  const lastUserIndex = state.messages.findLastIndex((message) => message.role === "user");
  return state.messages
    .slice(lastUserIndex + 1)
    .some((message) => message.role === "assistant" && message.content.trim());
}

function toolEventFromMessage(message: ChatMessage): ToolEvent | null {
  if (message.role !== "tool_call") return null;
  const status = message.meta?.status;
  return {
    name: message.content,
    arguments: message.meta?.arguments as Record<string, unknown> | undefined,
    toolCallId: String(message.meta?.toolCallId ?? ""),
    result: message.meta?.result,
    status: status === "running" || status === "completed" || status === "done" || status === "failed" || status === "input_required" ? status : undefined,
    permission: message.meta?.permission as ToolEvent["permission"],
  };
}

function messageMatchesToolEvent(message: ChatMessage, name: string, toolCallId: string): boolean {
  const event = toolEventFromMessage(message);
  return event ? isSameToolEvent(event, name, toolCallId) : false;
}

function appendAssistantContentBlock(
  message: ChatMessage,
  text: string,
  blockIdentifier: string,
): ChatMessage {
  if (!message.contentBlocks) {
    throw new Error("Assistant messages require structured content blocks.");
  }
  const existingContentBlocks = message.contentBlocks;
  // Merged by identity, not by position.
  const existingIndex = existingContentBlocks.findIndex(
    (contentBlock) => contentBlock.identifier === blockIdentifier
  );
  const contentBlocks = existingIndex >= 0
    ? existingContentBlocks.map((contentBlock, contentBlockIndex) =>
        contentBlockIndex === existingIndex
          ? { ...contentBlock, content: contentBlock.content + text }
          : contentBlock
      )
    : [...existingContentBlocks, { identifier: blockIdentifier, content: text }];
  return { ...message, content: message.content + text, contentBlocks };
}

/** The message already holding this block of prose, if there is one. */
function messageHoldingBlock(state: ReduceState, blockIdentifier: string): string | null {
  const owner = state.messages.findLast(
    (message) => message.role === "assistant"
      && message.contentBlocks?.some((block) => block.identifier === blockIdentifier)
  );
  return owner?.id ?? null;
}

function pushAssistantText(state: ReduceState, text: string, blockIdentifier: string, sourceId?: string): void {
  if (!text) return;
  if (!blockIdentifier) throw new Error("Assistant text requires a content-block identity.");
  finishRunningThinking(state);
  const owner = messageHoldingBlock(state, blockIdentifier);
  if (owner !== null) {
    state.messages = state.messages.map((message) =>
      message.id === owner ? appendAssistantContentBlock(message, text, blockIdentifier) : message
    );
    return;
  }
  state.messages = [
    ...state.messages,
    {
      id: stableMessageId(state, "asst", sourceId),
      role: "assistant",
      content: text,
      contentBlocks: [{ identifier: blockIdentifier, content: text }],
      timestamp: new Date().toISOString(),
    },
  ];
}

// Normalize the attachments carried by an `attachments` data payload into the lean shape the UI renders.
function attachmentsFromData(data: Record<string, unknown> | undefined): MessageAttachment[] {
  if (!data || data.kind !== "attachments") return [];
  const raw = Array.isArray(data.attachments) ? data.attachments : [];
  const attachments: MessageAttachment[] = [];
  for (const entry of raw) {
    const record = asRecord(entry);
    const path = String(record.path ?? "");
    const filename = String(record.filename ?? record.title ?? "");
    if (!path && !filename) continue;
    attachments.push({
      filename: filename || path.split("/").pop() || "attachment",
      path,
      mimeType: String(record.mime_type ?? ""),
      size: Number(record.size ?? 0),
    });
  }
  return attachments;
}

function attachmentsFromMessage(message: A2AMessage): MessageAttachment[] {
  const attachments: MessageAttachment[] = [];
  for (const part of message.parts ?? []) {
    if (part.kind === "data") attachments.push(...attachmentsFromData(partPayload(part.data)));
  }
  return attachments;
}

// When the session took a message, as it recorded it.
function receivedAt(message: A2AMessage): string {
  const extension = asRecord(message.metadata?.[METADATA_KEY]);
  const stamp = extension.receivedAt;
  return typeof stamp === "string" ? stamp : "";
}

// A message a session received from outside itself: the user typed it, or another session sent it.
function reduceInboundMessage(state: ReduceState, message: A2AMessage, peerSender = ""): void {
  const text = (message.parts ?? []).filter((part) => part.kind === "text").map((part) => part.text ?? "").join("");
  const attachments = attachmentsFromMessage(message);
  // A user-role message with no visible prose AND no attachments carries nothing to render.
  if (!text.trim() && attachments.length === 0) return;
  const meta = {
    ...(attachments.length > 0 ? { attachments } : {}),
    ...(peerSender ? { peerSender } : {}),
  };
  upsertMessage(state, {
    id: stableMessageId(state, peerSender ? "peer" : "user", message.messageId),
    role: peerSender ? "peer" : "user",
    content: text,
    // When the session took it, from the message itself.
    timestamp: receivedAt(message) || new Date().toISOString(),
    ...(Object.keys(meta).length > 0 ? { meta } : {}),
  });
}

// The single reduction of one part.
function reduceAgentPart(state: ReduceState, part: A2APart, sourceId?: string): void {
  if (part.kind === "text") {
    pushAssistantText(state, part.text ?? "", requiredContentBlockIdentifier(part.metadata), sourceId);
    return;
  }
  if (part.kind !== "data" || !part.data) return;
  reduceDataPart(state, partPayload(part.data), sourceId);
}

function reduceAgentMessage(state: ReduceState, message: A2AMessage): void {
  for (const part of message.parts ?? []) reduceAgentPart(state, part, message.messageId);
}


function reduceDataPart(state: ReduceState, data: Record<string, unknown>, sourceId?: string): void {
  // Every event on this stream belongs to this session.
  const event = data as unknown as WireEvent;
  switch (event.kind) {
    case "steering": {
      const text = (event.text ?? "").trim();
      if (!text) break;
      // Delivered once, however many times it arrives.
      const steeringSender = (event.peer_sender ?? "").trim();
      const role = steeringSender ? "peer" : "user";
      const identifier = (event.message_id ?? "").trim();
      const alreadyShown = identifier
        ? state.messages.some((message) => message.id === `${role}-${identifier}`)
        : state.messages.some(
          (message) => message.role === role && message.content === text
            && !!sourceId && message.id.startsWith(`${role}-${sourceId}-`),
        );
      if (alreadyShown) break;
      state.messages = [
        ...state.messages,
        {
          id: identifier ? `${role}-${identifier}` : stableMessageId(state, role, sourceId),
          role,
          content: text,
          timestamp: new Date().toISOString(),
          ...(steeringSender ? { peerSender: steeringSender } : {}),
        },
      ];
      break;
    }
    case "compaction": {
      // A context-compaction marker: "started" shows a live compacting indicator, "done" turns it into the separator (or, when nothing was compacted, drops it).
      if (event.status === "started") {
        state.messages = [
          ...state.messages,
          {
            id: stableMessageId(state, "compaction", sourceId),
            role: "compaction",
            content: "",
            timestamp: new Date().toISOString(),
            meta: { status: "running", reason: event.reason ?? "" },
          },
        ];
        break;
      }
      if (event.status === "done") {
        const changed = event.ok !== false && (event.messages_after ?? 0) < (event.messages_before ?? 0);
        const runningIndex = state.messages.findLastIndex(
          (message) => message.role === "compaction" && message.meta?.status === "running"
        );
        if (!changed) {
          // Nothing was compacted — remove the running indicator so no separator lingers.
          if (runningIndex >= 0) state.messages = state.messages.filter((_, index) => index !== runningIndex);
          break;
        }
        const meta = {
          status: "done",
          reason: event.reason ?? "",
          messagesBefore: event.messages_before ?? 0,
          messagesAfter: event.messages_after ?? 0,
        };
        if (runningIndex >= 0) {
          state.messages = state.messages.map((message, index) =>
            index === runningIndex ? { ...message, meta } : message
          );
        } else {
          state.messages = [
            ...state.messages,
            { id: stableMessageId(state, "compaction", sourceId), role: "compaction", content: "", timestamp: new Date().toISOString(), meta },
          ];
        }
      }
      break;
    }
    case "token_usage": {
      // The cumulative totals grow monotonically across the session, so the latest part is authoritative on both the live stream and on replay.
      const cumulative = event.cumulative;
      // Per-call (latest) figures — this is the actual current context, not a sum.
      const contextInputTokens = event.input_tokens ?? 0;
      const contextOutputTokens = event.output_tokens ?? 0;
      state.tokenUsage = {
        inputTokens: cumulative?.input_tokens ?? 0,
        outputTokens: cumulative?.output_tokens ?? 0,
        totalTokens: cumulative?.total_tokens ?? 0,
        cacheReadTokens: cumulative?.cache_read_tokens ?? 0,
        cacheReachableTokens: cumulative?.reachable_tokens ?? 0,
        reasoningTokens: cumulative?.reasoning_tokens ?? 0,
        modelCalls: cumulative?.model_calls ?? 0,
        contextInputTokens,
        contextOutputTokens,
        contextTokens: contextInputTokens + contextOutputTokens,
        contextWindow: event.context_window ?? 0,
        contextCacheReadTokens: event.cache_read_tokens ?? 0,
        reachableTokens: event.reachable_tokens ?? 0,
        prefixIntact: event.prefix_intact ?? false,
        divergence: event.divergence ?? null,
      };
      break;
    }
    case "status": {
      // The model has finished thinking and is paused on tool execution.
      if (event.code === "waiting_for_tools") finishRunningThinking(state);
      // Anything else a status says is already shown by the row it belongs to; this arm exists so a status never falls through to the unknown-event path.
      break;
    }
    case "thinking":
      // A new reasoning phase ends the current prose block — without this, text emitted after a mid-turn think (or a control tool) merges into the prior message and the thinking card lands out of order.
      applyThinking(state, event.text ?? "");
      break;
    case "thinking_done":
      finishRunningThinkingWithDuration(state, event.duration_ms ?? 0);
      break;
    case "tool_call": {
      // Text either side of a tool call is separate prose, and says so: the model gives each block its own identity, so nothing here has to force the split.
      finishRunningThinking(state);
      const toolCallId = event.tool_call_id;
      // One row per tool call, always.
      const existing = state.messages.findIndex(
        (message) => message.role === "tool_call" && String(message.meta?.toolCallId ?? "") === toolCallId,
      );
      if (existing >= 0 && toolCallId) {
        state.messages = state.messages.map((message, index) =>
          index === existing
            ? {
                ...message,
                content: event.tool_name || message.content,
                // A call announced a second time is the same call running for real: keep whatever the prompt attached to it (its permission, its result) and let the arguments and status catch up.
                meta: { ...message.meta, arguments: event.arguments ?? message.meta?.arguments, status: "running" },
              }
            : message,
        );
        break;
      }
      state.messages = [
        ...state.messages,
        {
          id: toolCallMessageId(toolCallId),
          role: "tool_call",
          content: event.tool_name || "unknown",
          timestamp: new Date().toISOString(),
          meta: { arguments: event.arguments, toolCallId, status: "running" },
        },
      ];
      break;
    }
    case "tool_result": {
      finishRunningThinking(state);
      const toolName = event.tool_name;
      const toolCallId = event.tool_call_id;
      const currentMessage = state.messages.find((message) => messageMatchesToolEvent(message, toolName, toolCallId));
      const mergedResult = toolName === "call_mcp_tool" ? mergeMcpFinalResult(currentMessage?.meta?.result, event.display) : event.display;
      const resultStatus = statusFromWire(event.status);
      // set_tasks / update_tasks complete through this same universal path and carry the authoritative task list for the side panel inside their result.
      const resultTasks = asRecord(mergedResult).tasks;
      if (Array.isArray(resultTasks)) state.tasks = mergeTasks(state.tasks, resultTasks);
      let matched = false;
      state.messages = state.messages.map((message) =>
        messageMatchesToolEvent(message, toolName, toolCallId)
          ? (matched = true, { ...message, meta: { ...message.meta, status: resultStatus, result: mergedResult } })
          : message
      );
      if (!matched) {
        state.messages = [
          ...state.messages,
          {
            id: toolCallMessageId(toolCallId),
            role: "tool_call",
            content: toolName || "unknown",
            timestamp: new Date().toISOString(),
            meta: { toolCallId, status: resultStatus, result: mergedResult },
          },
        ];
      }
      break;
    }
    case "mcp_event": {
      const toolCallId = event.tool_call_id;
      const streamed = streamedMcpResult(data);
      const currentMessage = state.messages.find((message) => messageMatchesToolEvent(message, "call_mcp_tool", toolCallId));
      const mergedResult = mergeMcpResult(currentMessage?.meta?.result, streamed);
      state.messages = state.messages.map((message) =>
        messageMatchesToolEvent(message, "call_mcp_tool", toolCallId)
          ? { ...message, meta: { ...message.meta, status: "running", result: mergedResult } }
          : message
      );
      break;
    }
    case "permission_request": {
      // The approval lives on the tool call that triggered it — the card flips to "input required" and shows the prompt inline, so the command (and later its output) stay together.
      finishRunningThinking(state);
      const toolCallId = event.tool_call_id;
      const permission = {
        requestId: event.request_id,
        explanation: event.explanation || undefined,
        // The harness's own reason, as facts.
        reason: event.reason ?? undefined,
      };
      const attachedPermission = state.messages.some(
        (message) => message.role === "tool_call" && String(message.meta?.toolCallId ?? "") === toolCallId
      );
      if (attachedPermission) {
        state.messages = state.messages.map((message) =>
          message.role === "tool_call" && String(message.meta?.toolCallId ?? "") === toolCallId
            ? { ...message, meta: { ...message.meta, status: "input_required", permission } }
            : message
        );
      } else {
        // No card to attach to — and this is the *ordinary* case, not an edge one: approval is decided in preflight, before the batch runs, so the tool call has not been announced yet.
        const raised: ChatMessage = {
          id: toolCallMessageId(toolCallId),
          role: "tool_call",
          content: event.tool_name || "",
          timestamp: new Date().toISOString(),
          meta: {
            toolCallId: toolCallId ?? "",
            status: "input_required",
            permission,
            arguments: event.arguments ?? {},
          },
        };
        state.messages = [...state.messages, raised];
      }
      break;
    }
    case "question": {
      // An ask_user prompt attaches to the tool call that asked it, same lifecycle as a permission: the card flips to "input required" and renders the question inline; the tool result finalizes it once answered.
      finishRunningThinking(state);
      const toolCallId = event.tool_call_id;
      const question = {
        requestId: event.request_id,
        questions: (event.questions as unknown as QuestionItem[]) ?? [],
      };
      state.messages = state.messages.map((message) =>
        message.role === "tool_call" && String(message.meta?.toolCallId ?? "") === toolCallId
          ? { ...message, meta: { ...message.meta, status: "input_required", question } }
          : message
      );
      break;
    }
    case "error": {
      finishRunningThinking(state);
      const toolName = event.tool_name ?? "";
      const toolCallId = event.tool_call_id ?? "";
      if (toolCallId) {
        // Mark the tool call failed with a generic result.
        let matched = false;
        state.messages = state.messages.map((message) =>
          messageMatchesToolEvent(message, toolName, toolCallId)
            ? (matched = true, { ...message, meta: { ...message.meta, status: "failed", result: { code: "tool_error" } } })
            : message
        );
        if (matched) break;
        // A tool-scoped error whose card we can't find is still model-facing (a hidden envelope like `query`, or a card that never rendered).
        break;
      }
      pushErrorMessage(state, friendlyErrorFromData(data), sourceId);
      break;
    }
    case "warning": {
      finishRunningThinking(state);
      const title = event.title ?? "Warning";
      const message = event.message ?? "";
      state.messages = [
        ...state.messages,
        {
          id: stableMessageId(state, "warning", sourceId),
          role: "warning",
          content: message ? `${title} — ${message}` : title,
          timestamp: new Date().toISOString(),
          meta: { warning: { code: event.code ?? "", title, message } },
        },
      ];
      break;
    }
    case "text":
    case "done":
      // Streamed prose arrives as A2A text parts, not as a wire event, and a turn's end is signalled by the attach stream's own `done` frame — neither is reduced here.
      break;
    default: {
      // Exhaustiveness: a new WireEvent kind that is not handled above is a compile error.
      const _exhaustive: never = event;
      void _exhaustive;
      break;
    }
  }
}

// Reconstruct messages from a session's persisted A2A tasks.
function replayTurns(turns: A2ATurn[]): {
  messages: ChatMessage[];
  tasks: ChatTask[];
  tokenUsage: TokenUsage | null;
  keyCounts: Map<string, number>;
} {
  // Left in the order the server sent them, which is the order they *began* — it sorts each turn by where its first message landed in the append-only history, and that append order is the chronology.
  const mainTurns = turns.filter((turn) => !(turnState(turn).referenceTurnIds ?? []).length);
  const state: ReduceState = newReduceState();
  for (const turn of mainTurns) {
    // A turn's full message stream is its history PLUS its trailing status message: the A2A TaskManager keeps the latest message on `status.message` and only folds it into `history` on the *next* status update.
    const replayMessages = [...(turn.history ?? [])];
    const trailing = turn.status?.message;
    if (trailing && !replayMessages.some((message) => !!message.messageId && message.messageId === trailing.messageId)) {
      replayMessages.push(trailing);
    }
    // Stamped on the turn, not on the message, because it describes what opened the turn.
    const peerSender = turnState(turn).peerSender ?? "";
    for (const message of replayMessages) {
      if (message.role === "user") reduceInboundMessage(state, message, peerSender);
      else reduceAgentMessage(state, message);
    }
    if (!hasAssistantTextAfterLastUser(state)) {
      for (const artifact of turn.artifacts ?? []) {
        for (const part of artifact.parts ?? []) {
          if (part.kind !== "text" || !part.text?.trim()) continue;
          pushAssistantText(
            state,
            part.text,
            requiredContentBlockIdentifier(part.metadata),
            artifact.artifactId,
          );
        }
      }
    }
    if (TERMINAL_STATES.has(turn.status?.state as TaskState)) finishActiveTools(state);
  }
  return {
    messages: state.messages,
    tasks: state.tasks,
    tokenUsage: state.tokenUsage,
    keyCounts: state.keyCounts,
  };
}

export function useChat(
  agent: string,
  initialSessionId: string | null = null,
  workingDirectory?: string,
  worktreeStrategy: WorktreeStrategy = "none",
  permissionMode: PermissionMode = "ask",
  // Whether a turn is currently running on this session (from the server-tracked running set).
  sessionRunning: boolean = false,
  // The workspace this session belongs to; rides in the turn metadata so the server resolves the workspace's locations for the agent to address per tool call.
  workspaceId: string = ""
) {
  // Every message this hook can put in front of a person goes through here.
  const translation = useTranslations("ChatErrors");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [tasks, setTasks] = useState<ChatTask[]>([]);
  const [tokenUsage, setTokenUsage] = useState<TokenUsage | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(initialSessionId);
  const [isStreaming, setIsStreaming] = useState(false);
  const [isHistoryLoading, setIsHistoryLoading] = useState(!!initialSessionId);
  // Set when every attempt to load a session's transcript failed, so the panel can offer a retry instead of showing a permanently blank conversation that only a full page reload would recover.
  const [historyError, setHistoryError] = useState(false);
  const [hasOlderHistory, setHasOlderHistory] = useState(false);
  const [isOlderHistoryLoading, setIsOlderHistoryLoading] = useState(false);
  // Bumped to force the history-load effect to re-run (a manual retry).
  const [historyReloadNonce, setHistoryReloadNonce] = useState(0);
  const [queuedMessages, setQueuedMessages] = useState<QueuedMessage[]>([]);
  const [outboxHold, setOutboxHold] = useState<OutboxHold>(null);
  // Set when the session was created under a stricter mode than the one asked for.
  const [grantedPermissionMode, setGrantedPermissionMode] = useState<PermissionMode | null>(null);
  const [deliveringMessage, setDeliveringMessage] = useState<string | null>(null);

  // This hook's own attach subscription while it is driving a turn.
  const attachRef = useRef<{ abort: () => void } | null>(null);
  const stateRef = useRef<ReduceState>(newReduceState());
  const sessionIdRef = useRef<string | null>(initialSessionId);
  const historyLoadedForRef = useRef<string | null>(null);
  const historyFragmentsRef = useRef<A2ATurn[]>([]);
  const historyPageCursorRef = useRef<number | null>(null);
  const hasOlderHistoryRef = useRef(false);
  const isOlderHistoryLoadingRef = useRef(false);
  const isStreamingRef = useRef(false);
  // Set by a user Stop so the imminent stream-close does not auto-drain the queue into a fresh turn — which read as "Stop didn't stop it".
  const abortedByUserRef = useRef(false);
  const errorToastKeysRef = useRef<Set<string>>(new Set());
  // Tracks whether this session was running, so we do a final refresh when its turn finishes (the subscribe stream closes once it is no longer running).
  const wasRunningRef = useRef(false);
  // True once we have driven a turn in this mount.
  const streamedLocallyRef = useRef(false);
  const startTurnRef = useRef<(message: OutboxMessage) => Promise<Delivery>>(async () => "failed");
  const flushFrameRef = useRef<number | null>(null);

  // Whether the transcript is holding a decision nobody has made yet.
  const hasPendingDecision = useCallback(() => stateRef.current.messages.some(
    (message) => message.role === "tool_call" && message.meta?.status === "input_required"
  ), []);

  // A message the session would not take, and what it is waiting for instead.
  const notifyHeldForDecision = useCallback((waitingOn: string) => {
    toaster.create({
      type: "info",
      title: translation("heldForDecisionTitle"),
      description: translation("heldForDecisionBody", {
        waitingOn: waitingOn || translation("heldForDecisionFallback"),
      }),
      closable: true,
    });
  }, [translation]);

  const flushNow = useCallback(() => {
    if (flushFrameRef.current != null) {
      window.cancelAnimationFrame(flushFrameRef.current);
      flushFrameRef.current = null;
    }
    setMessages(stateRef.current.messages);
    setTasks(stateRef.current.tasks);
    setTokenUsage(stateRef.current.tokenUsage);
  }, []);

  const flush = useCallback(() => {
    if (typeof window === "undefined") {
      setMessages(stateRef.current.messages);
      setTasks(stateRef.current.tasks);
      setTokenUsage(stateRef.current.tokenUsage);
      return;
    }
    if (flushFrameRef.current != null) return;
    flushFrameRef.current = window.requestAnimationFrame(() => {
      flushFrameRef.current = null;
      setMessages(stateRef.current.messages);
      setTasks(stateRef.current.tasks);
      setTokenUsage(stateRef.current.tokenUsage);
    });
  }, []);

  const applyHistoryFragments = useCallback(() => {
    const replayed = replayTurns(historyFragmentsRef.current);
    stateRef.current = {
      messages: replayed.messages,
      tasks: replayed.tasks,
      tokenUsage: replayed.tokenUsage,
      keyCounts: replayed.keyCounts,
    };
    flushNow();
  }, [flushNow]);

  const notifyTurnError = useCallback((part: A2APart | undefined) => {
    const error = friendlyErrorFromPart(part);
    if (!error) return;
    const key = `${sessionIdRef.current || "turn"}:${error.code}:${error.status ?? ""}`;
    if (errorToastKeysRef.current.has(key)) return;
    errorToastKeysRef.current.add(key);
    toaster.create({
      type: "error",
      title: error.title,
      description: error.message,
      closable: true,
    });
  }, []);

  // On unmount (e.g. switching sessions), close this hook's SSE connection so it does not leak — a stack of orphaned streams exhausts the browser's per-host connection pool and hangs later fetches.
  useEffect(() => {
    return () => {
      if (flushFrameRef.current != null) {
        window.cancelAnimationFrame(flushFrameRef.current);
        flushFrameRef.current = null;
      }
      attachRef.current?.abort();
    };
  }, []);

  useEffect(() => {
    if (!initialSessionId) return;
    if (initialSessionId === sessionIdRef.current && stateRef.current.messages.length > 0) {
      setIsHistoryLoading(false);
      return;
    }
    if (initialSessionId === sessionIdRef.current && isStreamingRef.current) {
      setIsHistoryLoading(false);
      return;
    }
    if (historyLoadedForRef.current === initialSessionId) return;
    // A running session we're not driving is loaded by the live subscribe stream (snapshot catch-up + live tail).
    if (sessionRunning && !streamedLocallyRef.current) {
      historyLoadedForRef.current = initialSessionId;
      return;
    }
    historyLoadedForRef.current = initialSessionId;
    let cancelled = false;
    // Abort the in-flight fetch when the session switches or the panel unmounts — an orphaned request would keep a connection occupied (rapid switching can exhaust the browser's per-host pool and stall the next load) and could write a stale transcript after we have moved on.
    const controller = new AbortController();
    setHistoryError(false);
    setHasOlderHistory(false);
    setIsHistoryLoading(true);

    // A dropped fetch (a momentary connection-pool exhaustion from switching sessions quickly, a transient 5xx) used to leave the transcript blank with no recovery but a manual reload.
    const MAX_ATTEMPTS = 6;
    const loadHistory = async () => {
      for (let attempt = 0; attempt < MAX_ATTEMPTS; attempt += 1) {
        try {
          const page = await fetchSessionTurnsPage(initialSessionId, null, controller.signal, HISTORY_PAGE_LIMIT);
          if (cancelled) return;
          historyFragmentsRef.current = page.turns;
          historyPageCursorRef.current = page.next_before_row_id;
          hasOlderHistoryRef.current = page.has_more;
          setHasOlderHistory(page.has_more);
          setSessionId(initialSessionId);
          sessionIdRef.current = initialSessionId;
          applyHistoryFragments();
          setIsHistoryLoading(false);
          return;
        } catch (caught) {
          if (cancelled || controller.signal.aborted) return;
          if (attempt === MAX_ATTEMPTS - 1) {
            swallowed({ component: "transcript", operation: "load the transcript after retrying" }, caught);
            setHistoryError(true);
            setIsHistoryLoading(false);
            return;
          }
          await new Promise((resolve) => setTimeout(resolve, Math.min(4000, 300 * 2 ** attempt)));
          if (cancelled) return;
        }
      }
    };
    loadHistory();
    return () => {
      cancelled = true;
      controller.abort();
      historyLoadedForRef.current = null;
    };
  }, [initialSessionId, applyHistoryFragments, historyReloadNonce, sessionRunning]);

  // Transparent history fill.
  const drainOlderHistory = useCallback(async () => {
    const context = sessionIdRef.current;
    if (!context || isOlderHistoryLoadingRef.current) return;
    if (historyPageCursorRef.current == null || !hasOlderHistoryRef.current) return;
    // Never fill history over the top of a live turn: applying replayed history would rebuild state from the server transcript and drop the just-sent (not-yet- persisted) turn from view.
    if (streamedLocallyRef.current || isStreamingRef.current) return;
    isOlderHistoryLoadingRef.current = true;
    setIsOlderHistoryLoading(true);
    let fetchedAny = false;
    try {
      let cursor: number | null = historyPageCursorRef.current;
      while (cursor != null && hasOlderHistoryRef.current) {
        if (streamedLocallyRef.current || isStreamingRef.current) return;
        const page = await fetchSessionTurnsPage(context, cursor, undefined, HISTORY_PAGE_LIMIT);
        if (sessionIdRef.current !== context) return;
        historyFragmentsRef.current = [...page.turns, ...historyFragmentsRef.current];
        historyPageCursorRef.current = page.next_before_row_id;
        hasOlderHistoryRef.current = page.has_more;
        cursor = page.next_before_row_id;
        fetchedAny = true;
      }
    } catch (caught) {
      // Leave what we have loaded; the fill effect re-triggers and resumes from the last cursor since hasOlderHistoryRef is still true.
      swallowed({ component: "transcript", operation: "load older history" }, caught);
    } finally {
      // Skip the apply if a local turn began mid-drain (guarded above too) — the fetched fragments stay in the ref, unused, rather than clobbering live state.
      if (fetchedAny && sessionIdRef.current === context && !streamedLocallyRef.current && !isStreamingRef.current) {
        setHasOlderHistory(hasOlderHistoryRef.current);
        // One replay + render for the whole accumulated transcript.
        applyHistoryFragments();
      }
      isOlderHistoryLoadingRef.current = false;
      setIsOlderHistoryLoading(false);
    }
  }, [applyHistoryFragments]);

  // Kick the drain once the newest page is in and settled — but not while a turn is streaming (see the clobber guard inside).
  useEffect(() => {
    if (isHistoryLoading || isOlderHistoryLoading || isStreaming || !hasOlderHistory) return;
    void drainOlderHistory();
  }, [isHistoryLoading, isOlderHistoryLoading, isStreaming, hasOlderHistory, drainOlderHistory]);

  // Live updates for a session that is running but which this hook is not driving.
  useEffect(() => {
    if (!initialSessionId) return;

    let cancelled = false;
    let subscription: { abort: () => void } | null = null;
    const controller = new AbortController();

    const applySnapshot = (turns: A2ATurn[]) => {
      const replayed = replayTurns(turns);
      // Already showing exactly this.
      if (rendersIdentically(stateRef.current.messages, replayed.messages)) {
        stateRef.current.tasks = replayed.tasks;
        stateRef.current.tokenUsage = replayed.tokenUsage;
        sessionIdRef.current = initialSessionId;
        setSessionId(initialSessionId);
        flushNow();
        return;
      }
      stateRef.current = {
        messages: replayed.messages,
        tasks: replayed.tasks,
        tokenUsage: replayed.tokenUsage,
        keyCounts: replayed.keyCounts,
      };
      sessionIdRef.current = initialSessionId;
      setSessionId(initialSessionId);
      flushNow();
    };

    if (sessionRunning && !isStreamingRef.current && !streamedLocallyRef.current) {
      wasRunningRef.current = true;
      subscription = attachSession(
        initialSessionId,
        (frame) => {
          if (cancelled) return;
          if (frame.kind === "snapshot") {
            applySnapshot(frame.turns);
            setHistoryError(false);
            setIsHistoryLoading(false);
          } else if (frame.kind === "live") {
            reduceAgentPart(stateRef.current, frame.part);
            flush();
          } else if (frame.kind === "turn" && !frame.running) {
            // The turn ended, said by the session itself.
            void (async () => {
              try {
                const tasks = await fetchSessionTurns(initialSessionId, controller.signal);
                if (!cancelled && !isStreamingRef.current && !streamedLocallyRef.current) applySnapshot(tasks);
              } catch (caught) {
                // The `sessionRunning` path below captures the same terminal state a moment later, so this is recoverable — but not silent, or a store that never answers is indistinguishable from one that answers slowly.
                swallowed({ component: "transcript", operation: "read the finished turn" }, caught);
              }
            })();
          }
        },
        () => {
          // Stream closed (turn finished or dropped).
        },
      );
    } else if (!sessionRunning && wasRunningRef.current) {
      // The turn just finished — capture its terminal state once (the live tail misses the turn's result artifact, which is only written as the task completes), then stop.
      wasRunningRef.current = false;
      void (async () => {
        try {
          const tasks = await fetchSessionTurns(initialSessionId, controller.signal);
          if (!cancelled && !isStreamingRef.current && !streamedLocallyRef.current) applySnapshot(tasks);
        } catch (caught) {
          // Leave the last live state in place; it is very nearly the terminal state.
          swallowed({ component: "transcript", operation: "read the session's final state" }, caught);
        }
      })();
    }

    return () => {
      cancelled = true;
      controller.abort();
      subscription?.abort();
    };
  }, [sessionRunning, initialSessionId, isStreaming, flushNow, flush]);

  // There is deliberately no drain here, and none at the end of a turn either.

  // Manual retry after the transcript failed to load — re-run the history-load effect from scratch (clearing the per-session guard so it actually re-fetches).
  const reloadHistory = useCallback(() => {
    historyLoadedForRef.current = null;
    historyFragmentsRef.current = [];
    historyPageCursorRef.current = null;
    hasOlderHistoryRef.current = false;
    setHasOlderHistory(false);
    setHistoryError(false);
    setIsHistoryLoading(true);
    setHistoryReloadNonce((nonce) => nonce + 1);
  }, []);

  // Start a turn with one message, and answer what became of it.
  const startTurn = useCallback(
    (input: OutboxMessage): Promise<Delivery> => new Promise<Delivery>((settleDelivery) => {
      const dataParts = input.dataParts ?? [];
      const attachments = dataParts.flatMap((dataPart) => attachmentsFromData(dataPart));
      const meta = attachments.length > 0 ? { attachments } : {};
      // The id the composer gave this message when it was typed, carried onto the wire and used as the transcript key, so the optimistic copy and the session's echo are one row rather than two that read alike.
      const userMessageId = input.id;
      const showOptimistically = () => {
        upsertMessage(stateRef.current, {
          id: stableMessageId(stateRef.current, "user", userMessageId),
          role: "user",
          content: input.text,
          timestamp: new Date().toISOString(),
          ...(Object.keys(meta).length > 0 ? { meta } : {}),
        });
        // No thinking row is opened here.
        flushNow();
      };

      isStreamingRef.current = true;
      streamedLocallyRef.current = true;
      setIsStreaming(true);

      const text = input.text;

      // The turn is over, however it ended.
      let settled = false;
      const finishTurn = () => {
        if (settled) return;
        settled = true;
        // Stop watching.
        attachRef.current?.abort();
        attachRef.current = null;
        // A clean turn already settled its cards from the events it emitted; but a Stop, or a connection that drops mid-turn (daemon stall, network loss), ends it with no terminal event, which would otherwise leave every in-flight tool/thinking card spinning forever.
        finishRunningThinking(stateRef.current);
        finishActiveTools(stateRef.current);
        flush();
        // The queue is not touched here, and that is the change.
        abortedByUserRef.current = false;
        isStreamingRef.current = false;
        setIsStreaming(false);
        // Our locally-driven turn is over.
        streamedLocallyRef.current = false;
      };

      const observe = (sessionIdentifier: string) => {
        attachRef.current = attachSession(
          sessionIdentifier,
          (frame) => {
            // The one frame that says the turn ended.
            if (frame.kind === "turn") {
              if (!frame.running) finishTurn();
              return;
            }
            // A snapshot is the attach stream's catch-up for a viewer joining mid-turn.
            if (frame.kind !== "live") return;
            notifyTurnError(frame.part);
            reduceAgentPart(stateRef.current, frame.part);
            flush();
          },
          finishTurn,
        );
      };

      // A turn is two calls: make sure a session exists, then send it a message.
      void (async () => {
        try {
          let sessionIdentifier = sessionIdRef.current;
          if (!sessionIdentifier) {
            const created = await sessionCreate({
              agent,
              workingDirectory,
              worktreeStrategy,
              permissionMode,
              workspaceId,
            });
            sessionIdentifier = created.id;
            sessionIdRef.current = created.id;
            setSessionId(created.id);
            // The mode the session actually got, which is not always the one asked for: an agent profile carries a ceiling, and a child is clamped against its parent.
            if (created.permission_mode && created.permission_mode !== permissionMode) {
              setGrantedPermissionMode(created.permission_mode);
            }
          }
          // Attach before sending: the worker starts emitting the moment it accepts the message, and a subscription opened afterwards would miss the opening frames.
          observe(sessionIdentifier);
          const outcome = await sessionSend(sessionIdentifier, messageParts(text, dataParts), { messageId: userMessageId });
          // The session refused it: it is parked on a decision, and taking a message would mean discarding the parked turn.
          if (!outcome.accepted) {
            notifyHeldForDecision(outcome.waitingOn);
            finishTurn();
            settleDelivery("refused");
            return;
          }
          // Taken. Only now does it appear, because only now is it a message the session has.
          showOptimistically();
          settleDelivery("accepted");
        } catch (caught) {
          // The reason, not just the fact.
          const detail = errorMessage(caught);
          console.error("[frank] could not start the turn:", caught);
          pushErrorMessage(stateRef.current, {
            code: "server_error",
            title: "Server request failed",
            message: `Frank could not start the turn: ${detail}`,
          });
          // One wind-down for every ending. `finishTurn` closes the stream if one was opened.
          finishTurn();
          // It never reached the session, so the message keeps its place in the queue and the composer says why.
          settleDelivery("failed");
        }
      })();
    }),
    [agent, workingDirectory, worktreeStrategy, permissionMode, workspaceId, flush, flushNow, notifyTurnError, notifyHeldForDecision]
  );

  useEffect(() => {
    startTurnRef.current = startTurn;
  }, [startTurn]);

  // Hand one message to the session, whichever state it is in, and answer what became of it.
  const deliver = useCallback(async (message: OutboxMessage): Promise<Delivery> => {
    const context = sessionIdRef.current;
    if (!isStreamingRef.current || !context) return startTurnRef.current(message);
    try {
      const outcome = await sessionSend(
        context,
        messageParts(message.text, message.dataParts),
        { messageId: message.id },
      );
      if (!outcome.accepted) {
        notifyHeldForDecision(outcome.waitingOn);
        return "refused";
      }
      upsertMessage(stateRef.current, {
        id: stableMessageId(stateRef.current, "user", message.id),
        role: "user",
        content: message.text,
        timestamp: new Date().toISOString(),
      });
      // Synchronously, because the chip and the transcript row are two pieces of state showing one message: deferring one of them puts the message in both places for a frame, or in neither.
      flushNow();
      return "accepted";
    } catch {
      return "failed";
    }
  }, [notifyHeldForDecision, flushNow]);

  // The queue, and the only thing that empties it.
  const deliverRef = useRef(deliver);
  useEffect(() => { deliverRef.current = deliver; }, [deliver]);
  const parkedRef = useRef(hasPendingDecision);
  useEffect(() => { parkedRef.current = hasPendingDecision; }, [hasPendingDecision]);
  const outboxRef = useRef<Outbox | null>(null);
  useEffect(() => {
    if (outboxRef.current) return;
    outboxRef.current = new Outbox({
      deliver: (message) => deliverRef.current(message),
      parked: () => parkedRef.current(),
      changed: (state) => {
        setQueuedMessages(state.messages);
        setOutboxHold(state.hold);
        setDeliveringMessage(state.delivering);
      },
    });
  }, []);

  // Which conversation the queue belongs to.
  useEffect(() => { outboxRef.current?.retarget(initialSessionId ?? ""); }, [initialSessionId]);

  const send = useCallback((text: string, dataParts: Record<string, unknown>[] = []) => {
    const trimmed = text.trim();
    if (!trimmed) return Promise.resolve();
    // Every message goes the same way in, whatever the session is doing.
    outboxRef.current?.add({ id: clientIdentifier(), text: trimmed, dataParts });
    return Promise.resolve();
  }, []);

  // The decision that was holding the queue has been answered — by anyone, in any client.
  const decisionOpen = messages.some(
    (message) => message.role === "tool_call" && message.meta?.status === "input_required"
  );
  const decisionWasOpenRef = useRef(decisionOpen);
  useEffect(() => {
    const answered = decisionWasOpenRef.current && !decisionOpen;
    decisionWasOpenRef.current = decisionOpen;
    if (answered) outboxRef.current?.released();
  }, [decisionOpen]);

  // Settle a stuck prompt card and tell the user when their decision/answer could not be delivered — a dropped connection, or a request that already expired because the turn moved on.
  const notifyResolveFailure = useCallback((requestId: string, kind: "decision" | "answer", status: string) => {
    stateRef.current.messages = stateRef.current.messages.map((message) => {
      const permission = message.meta?.permission;
      const question = message.meta?.question;
      if (message.role !== "tool_call" || (permission?.requestId !== requestId && question?.requestId !== requestId)) return message;
      return { ...message, meta: { ...message.meta, status: "failed" } };
    });
    flush();
    toaster.create({
      type: "error",
      title: translation(kind === "decision" ? "decisionFailedTitle" : "answerFailedTitle"),
      description: translation(status === "network" ? "networkBody" : "inactiveBody"),
      closable: true,
    });
  }, [flush]);

  const settleInactivePrompt = useCallback((requestId: string) => {
    stateRef.current.messages = stateRef.current.messages.map((message) => {
      const permission = message.meta?.permission;
      const question = message.meta?.question;
      if (message.role !== "tool_call" || (permission?.requestId !== requestId && question?.requestId !== requestId)) return message;
      return { ...message, meta: { ...message.meta, status: "completed" } };
    });
    flush();
  }, [flush]);

  // Allow-always is gone: a decision here is per call.
  const handlePermission = useCallback(
    async (requestId: string, decision: "deny" | "allow_once") => {
      const context = sessionIdRef.current;
      if (!context) return;
      const result = await resolvePermission(context, requestId, decision);
      if (result.status === "stale" || result.status === "unknown") {
        settleInactivePrompt(requestId);
        return;
      }
      if (!result.ok) {
        notifyResolveFailure(requestId, "decision", result.status);
        return;
      }
      // Record the decision and hand the card back to its normal lifecycle: an approval resumes as "running" (the result/error finalizes it), a denial is settled by the error the backend emits for this tool call.
      stateRef.current.messages = stateRef.current.messages.map((message) => {
        const permission = message.meta?.permission;
        if (message.role !== "tool_call" || permission?.requestId !== requestId) return message;
        return {
          ...message,
          meta: { ...message.meta, status: "running", permission: { ...permission, decision } },
        };
      });
      flush();
    },
    [flush, notifyResolveFailure, settleInactivePrompt]
  );

  const handleQuestion = useCallback(
    async (requestId: string, answers: QuestionAnswer[]) => {
      const context = sessionIdRef.current;
      if (!context) return;
      const result = await resolveQuestion(context, requestId, answers);
      if (result.status === "stale" || result.status === "unknown") {
        settleInactivePrompt(requestId);
        return;
      }
      if (!result.ok) {
        notifyResolveFailure(requestId, "answer", result.status);
        return;
      }
      // Record the answer and hand the card back to its running lifecycle; the tool_result finalizes it (same shape as a resolved permission).
      stateRef.current.messages = stateRef.current.messages.map((message) => {
        const question = message.meta?.question;
        if (message.role !== "tool_call" || question?.requestId !== requestId) return message;
        return {
          ...message,
          meta: { ...message.meta, status: "running", question: { ...question, answers } },
        };
      });
      flush();
    },
    [flush, notifyResolveFailure, settleInactivePrompt]
  );

  // The user dismissed the whole question prompt without answering.
  const declineQuestion = useCallback(
    async (requestId: string) => {
      const context = sessionIdRef.current;
      if (!context) return;
      const result = await resolveQuestion(context, requestId, [], true);
      if (result.status === "stale" || result.status === "unknown") {
        settleInactivePrompt(requestId);
        return;
      }
      if (!result.ok) {
        notifyResolveFailure(requestId, "answer", result.status);
        return;
      }
      stateRef.current.messages = stateRef.current.messages.map((message) => {
        const question = message.meta?.question;
        if (message.role !== "tool_call" || question?.requestId !== requestId) return message;
        return { ...message, meta: { ...message.meta, status: "completed", question: { ...question, declined: true } } };
      });
      flush();
    },
    [flush, notifyResolveFailure, settleInactivePrompt]
  );

  const abort = useCallback(() => {
    const context = sessionIdRef.current;
    // Suppress the queue auto-drain that the imminent stream close would otherwise trigger, so Stop halts everything instead of relaunching a queued follow-up.
    abortedByUserRef.current = true;
    // Retire the steering chips now, not when the stream finally closes.
    if (!context) {
      // The session was never created (Stop landed while `create` was still in flight); there is nothing to cancel, so just close whatever stream is open.
      attachRef.current?.abort();
      return Promise.resolve();
    }
    // A Stop while the turn is paused on a decision auto-settles that decision: deny every pending permission and cancel every pending question, so the request is answered rather than left hanging.
    let settledAny = false;
    for (const message of stateRef.current.messages) {
      if (message.role !== "tool_call" || message.meta?.status !== "input_required") continue;
      const permission = message.meta?.permission;
      const question = message.meta?.question;
      if (permission?.requestId) {
        settledAny = true;
        void resolvePermission(context, permission.requestId, "deny");
      } else if (question?.requestId) {
        settledAny = true;
        // Stop while a question is open settles it as a decline (not empty answers): the model is told the user declined and the turn ends, which also cleanly resolves the awaiting tool even if the context teardown races.
        void resolveQuestion(context, question.requestId, [], true);
      }
    }
    if (settledAny) {
      stateRef.current.messages = stateRef.current.messages.map((message) =>
        message.role === "tool_call" && message.meta?.status === "input_required"
          ? { ...message, meta: { ...message.meta, status: "failed" } }
          : message
      );
      flush();
    }
    // Tell the user if the stop request never reached the server — the turn may still be running, and silently doing nothing would leave them stuck expecting it to end.
    return cancelTurn(context).then((ok) => {
      if (!ok) {
        toaster.create({
          type: "error",
          title: translation("stopFailedTitle"),
          description: translation("stopFailedBody"),
          closable: true,
        });
      }
    });
  }, [flush]);

  // Kick off a manual context compaction.
  const compact = useCallback(() => {
    const context = sessionIdRef.current;
    if (!context) return;
    void compactSession(context).then((ok) => {
      if (!ok) {
        toaster.create({
          type: "error",
          title: translation("compactFailedTitle"),
          description: translation("compactFailedBody"),
          closable: true,
        });
      }
    });
  }, []);

  const abortTool = useCallback((toolCallId: string) => {
    const context = sessionIdRef.current;
    if (!context || !toolCallId) return;
    void abortToolCall(context, toolCallId).then((ok) => {
      if (!ok) {
        toaster.create({
          type: "error",
          title: translation("cancelToolFailedTitle"),
          description: translation("cancelToolFailedBody"),
          closable: true,
        });
      }
    });
    stateRef.current.messages = stateRef.current.messages.map((message) => (
      message.role === "tool_call" && message.meta?.toolCallId === toolCallId
        ? { ...message, meta: { ...message.meta, status: "failed" } }
        : message
    ));
    flush();
  }, [flush]);

  const dequeueMessage = useCallback((index: number) => {
    const target = queuedMessages[index];
    if (target) outboxRef.current?.remove(target.id);
  }, [queuedMessages]);

  /** Ask again after a failure to reach the session. The person's own retry. */
  const retryOutbox = useCallback(() => outboxRef.current?.retry(), []);

  const reset = useCallback(() => {
    abort();
    stateRef.current = newReduceState();
    setMessages([]);
    setTasks([]);
    setTokenUsage(null);
    setSessionId(null);
    sessionIdRef.current = null;
    historyFragmentsRef.current = [];
    historyPageCursorRef.current = null;
    hasOlderHistoryRef.current = false;
    setHasOlderHistory(false);
  }, [abort]);

  return {
    messages,
    tasks,
    tokenUsage,
    queuedMessages,
    sessionId,
    isStreaming: isStreaming || sessionRunning,
    isHistoryLoading,
    historyError,
    hasOlderHistory,
    isOlderHistoryLoading,
    reloadHistory,
    send,
    abort,
    abortTool,
    compact,
    reset,
    dequeueMessage,
    outboxHold,
    deliveringMessage,
    grantedPermissionMode,
    retryOutbox,
    handlePermission,
    handleQuestion,
    declineQuestion,
  };
}
