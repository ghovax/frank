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

// A file attached to a turn, as the metadata a chip renders from. Carried on the user message's meta.
export interface MessageAttachment {
  filename: string;
  path: string;
  mimeType: string;
  size: number;
}

// The per-message side data reducers attach and views read. Every field optional and role-specific.
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
  // On a peer message: which session sent it, since a report comes from somewhere with an id.
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

// Running token totals, summed from the per-call usage the model reports.
export interface TokenUsage {
  // Cumulative session totals — the running spend, shown only in the tooltip.
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
  cacheReadTokens: number;
  // What a cache could have returned this session — the denominator cacheReadTokens means something against.
  cacheReachableTokens: number;
  reasoningTokens: number;
  modelCalls: number;
  // Current occupancy: the latest call's prompt plus its reply, not the cumulative sum.
  contextTokens: number; // contextInputTokens + contextOutputTokens
  contextInputTokens: number;
  contextOutputTokens: number;
  contextWindow: number;
  // What the latest call's cache did and why, since a running total cannot say which call missed.
  contextCacheReadTokens: number;
  reachableTokens: number;
  prefixIntact: boolean;
  divergence: PrefixDivergence | null;
}

// A turn's input: prose plus structured payloads, which travel as DataParts so the agent gets JSON.
export type ChatInput = { kind: "text"; text: string; dataParts?: Record<string, unknown>[] };

// What the composer still holds: messages the session has not taken, and nothing else.
export type QueuedMessage = OutboxMessage;

// The wire's ToolStatus as a UI lifecycle. `input_required` is UI-only, driven by events rather than results.
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
    // The fallback for when the daemon could not name the window and the model. Its own case, since an overlong conversation is worth saying plainly.
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

// A turn-level failure worth a toast. A tool-scoped error renders on its own card instead.
function friendlyErrorFromPart(part: A2APart | undefined): FriendlyError | null {
  if (!part || part.kind !== "data") return null;
  const payload = partPayload(part.data);
  // A `tool_call_id` marks a failure belonging to one card, which renders in place.
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

// Each notification is appended to `events`, while the latest values stay at the top level.
function mergeMcpResult(existing: unknown, streamed: Record<string, unknown>): Record<string, unknown> {
  const current = asRecord(existing);
  const events = Array.isArray(current.events) ? current.events : [];
  return {
    ...current,
    ...streamed,
    events: [...events, streamed],
  };
}

// The return value replaces the streamed state but keeps the notification log only the stream carried.
function mergeMcpFinalResult(existing: unknown, finalResult: unknown): unknown {
  const current = asRecord(existing);
  const finalRecord = asRecord(finalResult);
  if (Object.keys(finalRecord).length === 0) return finalResult;
  if (!Array.isArray(current.events)) return finalResult;
  return { ...finalRecord, events: current.events };
}


// Turning A2A stream parts into chat state. Shared by attach and replay, so both render identically.

interface ReduceState {
  messages: ChatMessage[];
  tasks: ChatTask[];
  tokenUsage: TokenUsage | null; // latest cumulative token totals, if any reported
  // Per-source occurrence counter, so a key comes from the server messageId rather than array position.
  keyCounts: Map<string, number>;
}

// Whether a replayed transcript would render exactly what is on screen, which is the end-of-stream flash.
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

// A position-independent id: the A2A messageId plus an occurrence counter, so replay converges.
function toolCallMessageId(toolCallId: string | undefined): string {
  return `toolcall-${toolCallId || clientIdentifier()}`;
}

function stableMessageId(state: ReduceState, prefix: string, sourceId: string | undefined): string {
  if (!sourceId) {
  // A monotonic counter, not the array length, or inserting a row earlier renumbers every row after it.
    const issued = state.keyCounts.get("") ?? 0;
    state.keyCounts.set("", issued + 1);
    return `${prefix}-anon-${issued}`;
  }
  // A message that has an id is that id, so the optimistic copy and the echo are one row.
  return `${prefix}-${sourceId}`;
}

function upsertMessage(state: ReduceState, message: ChatMessage): void {
// Replace in place when the row exists, append when it does not, so a message arriving twice converges.
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

// Close the in-flight thinking row with its measured duration, so it reads "Thought for Ns".
function finishRunningThinkingWithDuration(state: ReduceState, durationMs: number): void {
  state.messages = state.messages.map((message) =>
    isRunningThinkingMessage(message)
      ? { ...message, meta: { ...message.meta, status: "done", durationMs } }
      : message
  );
}

// Open the Thinking row when a turn is sent, so the create/attach round trip is not invisible.
function finishActiveTools(state: ReduceState): void {
  state.messages = state.messages.map((message) =>
    message.role === "tool_call" && message.meta?.status === "running"
      ? { ...message, meta: { ...message.meta, status: "completed" } }
      : message
  );
}

// The one path for the thinking signal: ensure a running row exists, then append the reasoning.
function applyThinking(state: ReduceState, text: string): void {
  let index = state.messages.findLastIndex(isRunningThinkingMessage);
  if (index === -1) {
    state.messages = [
      ...state.messages,
      { id: `status-${state.messages.length}`, role: "thinking", content: "", timestamp: new Date().toISOString(), meta: { status: "running" } },
    ];
    index = state.messages.length - 1;
  }
    // The session is reasoning, so a row opened optimistically stops being provisional.
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
    // Merged by identity, not position: a later delta belongs in its block wherever that has ended up.
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

/** The message already holding this block of prose, grouped by the identity the model gives it. */
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

// Attachments into the lean shape the UI renders, shared by the live send and replay so chips match.
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

// When the session took a message, as it recorded it, so a conversation reloaded later keeps its clock.
function receivedAt(message: A2AMessage): string {
  const extension = asRecord(message.metadata?.[METADATA_KEY]);
  const stamp = extension.receivedAt;
  return typeof stamp === "string" ? stamp : "";
}

// A message from outside the session. `peerSender` tells a peer's report from the user's own words.
function reduceInboundMessage(state: ReduceState, message: A2AMessage, peerSender = ""): void {
  const text = (message.parts ?? []).filter((part) => part.kind === "text").map((part) => part.text ?? "").join("");
  const attachments = attachmentsFromMessage(message);
  // No prose and no attachments is nothing to render, and replay must match the live path exactly.
  if (!text.trim() && attachments.length === 0) return;
  const meta = {
    ...(attachments.length > 0 ? { attachments } : {}),
    ...(peerSender ? { peerSender } : {}),
  };
  upsertMessage(state, {
    id: stableMessageId(state, peerSender ? "peer" : "user", message.messageId),
    role: peerSender ? "peer" : "user",
    content: text,
    // When the session took it, from the message itself, or a reloaded transcript dates everything to now.
    timestamp: receivedAt(message) || new Date().toISOString(),
    ...(Object.keys(meta).length > 0 ? { meta } : {}),
  });
}

// The one reduction of a part, walked by replay and by the live tail alike.
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
    // Every event here belongs to this session, and is read through the generated union's discriminant.
  const event = data as unknown as WireEvent;
  switch (event.kind) {
    case "steering": {
      const text = (event.text ?? "").trim();
      if (!text) break;
      // Delivered once however often it arrives, since attaching replays turns the session already holds.
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
      // A compaction marker: a live indicator, then a separator, rendered as a divider rather than a bubble.
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
      // The cumulative totals grow monotonically, so the latest part is authoritative on either path.
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
      // Paused on tool execution: tools surface their own status, so just close the thinking indicator.
      if (event.code === "waiting_for_tools") finishRunningThinking(state);
      // Anything else a status says is shown by its own row; this arm stops it reaching the unknown path.
      break;
    }
    case "thinking":
      // A new reasoning phase ends the current prose block, or text after it merges into the prior message.
      applyThinking(state, event.text ?? "");
      break;
    case "thinking_done":
      finishRunningThinkingWithDuration(state, event.duration_ms ?? 0);
      break;
    case "tool_call": {
      // Text either side of a tool call is separate prose, and the model gives each block its own identity.
      finishRunningThinking(state);
      const toolCallId = event.tool_call_id;
      // One row per tool call: a call stopped for approval is announced twice, and both are the same call.
      const existing = state.messages.findIndex(
        (message) => message.role === "tool_call" && String(message.meta?.toolCallId ?? "") === toolCallId,
      );
      if (existing >= 0 && toolCallId) {
        state.messages = state.messages.map((message, index) =>
          index === existing
            ? {
                ...message,
                content: event.tool_name || message.content,
          // The second announcement is the call running for real: keep what the prompt attached to it.
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
      // The task tools complete through this same path, carrying the authoritative list in their result.
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
      // The approval lives on the tool call that triggered it, so the command and its output stay together.
      finishRunningThinking(state);
      const toolCallId = event.tool_call_id;
      const permission = {
        requestId: event.request_id,
        explanation: event.explanation || undefined,
        // The harness's own reason, as facts, written into a sentence by whoever draws the prompt.
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
      // No card yet, and that is ordinary: approval is decided in preflight, before the call is announced.
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
      // An ask_user prompt attaches to its tool call, same lifecycle as a permission.
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
      // Mark the call failed generically: the raw text is model-facing and must not leak to the UI.
        let matched = false;
        state.messages = state.messages.map((message) =>
          messageMatchesToolEvent(message, toolName, toolCallId)
            ? (matched = true, { ...message, meta: { ...message.meta, status: "failed", result: { code: "tool_error" } } })
            : message
        );
        if (matched) break;
        // A tool-scoped error with no card is still model-facing, so swallow it rather than raise a toast.
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
      // Streamed prose arrives as text parts and a turn's end as the stream's own frame; neither is reduced here.
      break;
    default: {
      // Exhaustiveness: a new WireEvent kind that is not handled above is a compile error.
      const _exhaustive: never = event;
      void _exhaustive;
      break;
    }
  }
}

// Rebuild messages from persisted tasks, already compacted server-side, so no client pass is needed.
function replayTurns(turns: A2ATurn[]): {
  messages: ChatMessage[];
  tasks: ChatTask[];
  tokenUsage: TokenUsage | null;
  keyCounts: Map<string, number>;
} {
  // Left in the order the server sent them, which is the order they began — the append order is the chronology.
  const mainTurns = turns.filter((turn) => !(turnState(turn).referenceTurnIds ?? []).length);
  const state: ReduceState = newReduceState();
  for (const turn of mainTurns) {
    // A turn's stream is its history plus its trailing status message, which A2A folds in only on the next update.
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
  // Whether a turn is running on this session, which drives the live stream when we are watching it.
  sessionRunning: boolean = false,
  // The workspace this session belongs to, so the server resolves the locations the agent may address.
  workspaceId: string = ""
) {
  // Every message this hook puts in front of a person goes through the catalogue.
  const translation = useTranslations("ChatErrors");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [tasks, setTasks] = useState<ChatTask[]>([]);
  const [tokenUsage, setTokenUsage] = useState<TokenUsage | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(initialSessionId);
  const [isStreaming, setIsStreaming] = useState(false);
  const [isHistoryLoading, setIsHistoryLoading] = useState(!!initialSessionId);
  // Set when every attempt to load the transcript failed, so the panel can offer a retry.
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

  // This hook's attach subscription while it drives a turn. Closing it only drops the client end.
  const attachRef = useRef<{ abort: () => void } | null>(null);
  const stateRef = useRef<ReduceState>(newReduceState());
  const sessionIdRef = useRef<string | null>(initialSessionId);
  const historyLoadedForRef = useRef<string | null>(null);
  const historyFragmentsRef = useRef<A2ATurn[]>([]);
  const historyPageCursorRef = useRef<number | null>(null);
  const hasOlderHistoryRef = useRef(false);
  const isOlderHistoryLoadingRef = useRef(false);
  const isStreamingRef = useRef(false);
  // Set by a Stop so the stream close does not drain the queue into a fresh turn. One-shot.
  const abortedByUserRef = useRef(false);
  const errorToastKeysRef = useRef<Set<string>>(new Set());
  // Whether this session was running, so a final refresh happens once its turn finishes.
  const wasRunningRef = useRef(false);
  // True once we have driven a turn: the live stream is then authoritative, so never subscribe as well.
  const streamedLocallyRef = useRef(false);
  const startTurnRef = useRef<(message: OutboxMessage) => Promise<Delivery>>(async () => "failed");
  const flushFrameRef = useRef<number | null>(null);

  // Whether the transcript holds a decision nobody has made: a parked session has stopped, not paused.
  const hasPendingDecision = useCallback(() => stateRef.current.messages.some(
    (message) => message.role === "tool_call" && message.meta?.status === "input_required"
  ), []);

  // A message the session would not take. Not an error: it is queued, visible, and goes out on the decision.
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

  // Close this hook's stream on unmount, or orphaned streams exhaust the browser's connection pool.
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
    // A running session we are not driving loads over the live stream, so skip REST and avoid the race.
    if (sessionRunning && !streamedLocallyRef.current) {
      historyLoadedForRef.current = initialSessionId;
      return;
    }
    historyLoadedForRef.current = initialSessionId;
    let cancelled = false;
    // Abort the fetch when the session switches, or it holds a connection and writes a stale transcript.
    const controller = new AbortController();
    setHistoryError(false);
    setHasOlderHistory(false);
    setIsHistoryLoading(true);

    // Retry a few times with a short backoff, so a transient failure heals rather than leaving a blank panel.
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

  // Pull every older page back-to-back into a ref, then prepend once, so the scroll never jumps.
  const drainOlderHistory = useCallback(async () => {
    const context = sessionIdRef.current;
    if (!context || isOlderHistoryLoadingRef.current) return;
    if (historyPageCursorRef.current == null || !hasOlderHistoryRef.current) return;
    // Never fill history over a live turn: replay would drop the just-sent turn from view.
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
      // Leave what loaded and resume from the last cursor. Still reported: a permanent failure looks the same otherwise.
      swallowed({ component: "transcript", operation: "load older history" }, caught);
    } finally {
    // Skip the apply if a local turn began mid-drain: the fragments stay unused rather than clobbering it.
      if (fetchedAny && sessionIdRef.current === context && !streamedLocallyRef.current && !isStreamingRef.current) {
        setHasOlderHistory(hasOlderHistoryRef.current);
        // One replay + render for the whole accumulated transcript.
        applyHistoryFragments();
      }
      isOlderHistoryLoadingRef.current = false;
      setIsOlderHistoryLoading(false);
    }
  }, [applyHistoryFragments]);

  // Kick the drain once the newest page has settled, and not while a turn is streaming.
  useEffect(() => {
    if (isHistoryLoading || isOlderHistoryLoading || isStreaming || !hasOlderHistory) return;
    void drainOlderHistory();
  }, [isHistoryLoading, isOlderHistoryLoading, isStreaming, hasOlderHistory, drainOlderHistory]);

  // Live updates for a running session this hook is not driving: a snapshot, then a tail, through one reducer.
  useEffect(() => {
    if (!initialSessionId) return;

    let cancelled = false;
    let subscription: { abort: () => void } | null = null;
    const controller = new AbortController();

    const applySnapshot = (turns: A2ATurn[]) => {
      const replayed = replayTurns(turns);
        // Already showing this: keeping the array reference means `setMessages` bails and nothing re-renders.
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
          // The turn ended, said by the session on the stream we already hold, before the polled listing learns it.
            void (async () => {
              try {
                const tasks = await fetchSessionTurns(initialSessionId, controller.signal);
                if (!cancelled && !isStreamingRef.current && !streamedLocallyRef.current) applySnapshot(tasks);
              } catch (caught) {
              // Recoverable, since the `sessionRunning` path captures the same state, but not silent.
                swallowed({ component: "transcript", operation: "read the finished turn" }, caught);
              }
            })();
          }
        },
        () => {
      // Stream closed. The `sessionRunning` flip re-runs this effect and captures the final state.
        },
      );
    } else if (!sessionRunning && wasRunningRef.current) {
    // The turn just finished: capture its terminal state once, which the live tail misses.
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

  // Deliberately no drain here or at a turn's end: both read state that delivery itself changed.

  // Manual retry after a failed load, clearing the per-session guard so it actually re-fetches.
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

  // Start a turn with one message and answer what became of it, so a refusal is not silently drawn.
  const startTurn = useCallback(
    (input: OutboxMessage): Promise<Delivery> => new Promise<Delivery>((settleDelivery) => {
      const dataParts = input.dataParts ?? [];
      const attachments = dataParts.flatMap((dataPart) => attachmentsFromData(dataPart));
      const meta = attachments.length > 0 ? { attachments } : {};
      // The id the composer gave this message, carried onto the wire, so the copy and the echo are one row.
      const userMessageId = input.id;
      const showOptimistically = () => {
        upsertMessage(stateRef.current, {
          id: stableMessageId(stateRef.current, "user", userMessageId),
          role: "user",
          content: input.text,
          timestamp: new Date().toISOString(),
          ...(Object.keys(meta).length > 0 ? { meta } : {}),
        });
        // No thinking row here: the session says when it is thinking, and until then it has not started.
        flushNow();
      };

      isStreamingRef.current = true;
      streamedLocallyRef.current = true;
      setIsStreaming(true);

      const text = input.text;

      // Three unordered things can end a turn, so this settles once whichever arrives first.
      let settled = false;
      const finishTurn = () => {
        if (settled) return;
        settled = true;
        // Stop watching: the stream belongs to the session and would leak one connection per turn.
        attachRef.current?.abort();
        attachRef.current = null;
        // A Stop or a dropped connection ends a turn with no terminal event, leaving cards spinning.
        finishRunningThinking(stateRef.current);
        finishActiveTools(stateRef.current);
        flush();
        // The queue is not touched here: a turn ending must not be both a trigger and a deletion.
        abortedByUserRef.current = false;
        isStreamingRef.current = false;
        setIsStreaming(false);
        // Back to viewer mode, so an autonomous wake is picked up live rather than on a reload.
        streamedLocallyRef.current = false;
      };

      const observe = (sessionIdentifier: string) => {
        attachRef.current = attachSession(
          sessionIdentifier,
          (frame) => {
          // The one frame that says the turn ended. The stream does not close: it belongs to the session.
            if (frame.kind === "turn") {
              if (!frame.running) finishTurn();
              return;
            }
          // A snapshot is catch-up for a viewer. We are driving, so replacing state would drop the sent message.
            if (frame.kind !== "live") return;
            notifyTurnError(frame.part);
            reduceAgentPart(stateRef.current, frame.part);
            flush();
          },
          finishTurn,
        );
      };

      // A turn is two calls: ensure a session, then send. `send` carries no settings and cannot change them.
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
          // The mode the session actually got, which a profile ceiling or a parent clamp may have tightened.
            if (created.permission_mode && created.permission_mode !== permissionMode) {
              setGrantedPermissionMode(created.permission_mode);
            }
          }
          // Attach before sending, or the opening frames are missed.
          observe(sessionIdentifier);
          const outcome = await sessionSend(sessionIdentifier, messageParts(text, dataParts), { messageId: userMessageId });
          // Refused because the session is parked: nothing was delivered, and the message stays in the queue.
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
          // The reason, not just the fact: which call and which failure, rather than advice to read a log.
          const detail = errorMessage(caught);
          console.error("[frank] could not start the turn:", caught);
          pushErrorMessage(stateRef.current, {
            code: "server_error",
            title: "Server request failed",
            message: `Frank could not start the turn: ${detail}`,
          });
          // One wind-down for every ending. `finishTurn` closes the stream if one was opened.
          finishTurn();
          // It never reached the session, so the message keeps its place and nothing retries it.
          settleDelivery("failed");
        }
      })();
    }),
    [agent, workingDirectory, worktreeStrategy, permissionMode, workspaceId, flush, flushNow, notifyTurnError, notifyHeldForDecision]
  );

  useEffect(() => {
    startTurnRef.current = startTurn;
  }, [startTurn]);

  // Hand one message to the session whatever state it is in, and answer what became of it.
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
      // Synchronously, so the chip and the transcript row hand over as a move rather than a flicker.
      flushNow();
      return "accepted";
    } catch {
      return "failed";
    }
  }, [notifyHeldForDecision, flushNow]);

  // The queue, and the only thing that empties it. A ref, because it outlives every render.
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

  // Which conversation the queue belongs to. `retarget`, not a cleanup that fires on every id change.
  useEffect(() => { outboxRef.current?.retarget(initialSessionId ?? ""); }, [initialSessionId]);

  const send = useCallback((text: string, dataParts: Record<string, unknown>[] = []) => {
    const trimmed = text.trim();
    if (!trimmed) return Promise.resolve();
    // Every message goes in the same way, whatever the session is doing. No branch at the keystroke.
    outboxRef.current?.add({ id: clientIdentifier(), text: trimmed, dataParts });
    return Promise.resolve();
  }, []);

  // The held decision was answered, by anyone. The only retry trigger, and one a delivery cannot cause.
  const decisionOpen = messages.some(
    (message) => message.role === "tool_call" && message.meta?.status === "input_required"
  );
  const decisionWasOpenRef = useRef(decisionOpen);
  useEffect(() => {
    const answered = decisionWasOpenRef.current && !decisionOpen;
    decisionWasOpenRef.current = decisionOpen;
    if (answered) outboxRef.current?.released();
  }, [decisionOpen]);

  // Settle a stuck card and say so when a decision could not be delivered, or the composer stays blocked.
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

  // A decision here is per call: widening what a session may do is the permission mode's job.
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
      // Record the decision and hand the card back to its lifecycle; the result or the error finalizes it.
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
      // Record the answer and hand the card back to its running lifecycle.
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

  // Dismissed without answering: tell the model, which stops the turn, and settle the card. Not a Stop.
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
    // Suppress the auto-drain the imminent close would trigger, so Stop does not relaunch a queued follow-up.
    abortedByUserRef.current = true;
    // Retire the steering chips now: a message in flight is a chip until the session injects it.
    if (!context) {
      // The session was never created, so there is nothing to cancel but the open stream.
      attachRef.current?.abort();
      return Promise.resolve();
    }
    // A Stop while parked settles the decision, so nothing is left hanging at "input required".
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
        // A Stop settles an open question as a decline, so the awaiting tool resolves cleanly.
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
      // Say so if the stop never reached the server: the turn may still be running.
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

  // Kick off a manual compaction; the indicator and separator arrive over the stream.
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
