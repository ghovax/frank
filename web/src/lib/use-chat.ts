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
  partPayload,
  turnState,
  type A2AMessage,
  type A2APart,
  type A2ATurn as A2ATurnWire,
  type PermissionMode,
  type WorkspaceStrategy,
} from "./api";
import { isSameToolEvent, type QuestionAnswer, type QuestionItem, type ToolEvent, type ToolEventStatus, type ToolPermission, type ToolQuestion } from "./tool-event";
import { toaster } from "@/components/ui/toaster";
import { swallowed } from "@/lib/swallowed";
import { asArray, asRecord } from "@/lib/coerce";
import type { WireEvent } from "@/lib/generated/events";

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

// A file the user attached to a turn — the metadata needed to render a chip and
// show it (name, on-disk path, mime, size). Carried on the
// user message's `meta.attachments`, both on the live send and on replay.
export interface MessageAttachment {
  filename: string;
  path: string;
  mimeType: string;
  size: number;
}

// The per-message side data the reducers attach and the views read. Every field is optional and
// role-specific (a tool_call carries arguments/status/result/permission/question; a compaction
// carries reason/messages*; a warning carries `warning`; …). Typed so a read like
// `message.meta?.permission` is a real `ToolPermission | undefined`, not `unknown`.
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
  // On a `peer` message: which session sent it. The transcript shows a report as coming
  // from somewhere, and "somewhere" has an id.
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

// Running token totals for the session, summed from the real per-call usage the
// model reports. Carried on `token_usage` parts and mirrored into hook state.
export interface TokenUsage {
  // Cumulative session totals — the running spend, shown only in the tooltip.
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
  cacheReadTokens: number;
  reasoningTokens: number;
  modelCalls: number;
  // Current context occupancy — the latest call's prompt (system + history + turn)
  // plus the reply it generated (completion + reasoning). This is what's actually in
  // the window right now, and what the indicator visualizes — not the cumulative sum.
  // contextWindow is the model's limit (0 when unknown).
  contextTokens: number; // contextInputTokens + contextOutputTokens
  contextInputTokens: number;
  contextOutputTokens: number;
  contextWindow: number;
}

// A turn's input: typed prose plus any structured payloads, which travel as DataParts
// so the agent receives them as JSON rather than as prose.
export type ChatInput = { kind: "text"; text: string; dataParts?: Record<string, unknown>[] };

export interface QueuedMessage {
  id: string;
  text: string;
  steering: boolean;
  dataParts?: Record<string, unknown>[];
}

// The explicit ToolStatus from the wire mapped to the UI lifecycle. `input_required`
// is a separate, UI-only state driven by permission/question events, never a result.
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
      case "request_too_large":
        return { title: "Request is too large", message: "The agent's model could not accept this much context. Start a smaller follow-up or configure a model with more capacity." };
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

// A turn-level failure worth a toast. A tool-scoped error (one carrying a tool call id)
// is already delivered to the model and rendered on that tool's card, so it must not
// raise a banner as well.
function friendlyErrorFromPart(part: A2APart | undefined): FriendlyError | null {
  if (!part || part.kind !== "data") return null;
  const payload = partPayload(part.data);
  // A `tool_call_id` marks a failure that belongs to one tool card, which renders in place;
  // only a turn-level error becomes a toast.
  if (payload.kind !== "error" || payload.tool_call_id) return null;
  return friendlyErrorFromData(payload);
}

function pushErrorMessage(state: ReduceState, error: FriendlyError, sourceId?: string): void {
  state.lane = null;
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

// Each streamed notification is appended to `events` so the card shows the server's
// progress as it arrives, while the latest values stay at the top level.
function mergeMcpResult(existing: unknown, streamed: Record<string, unknown>): Record<string, unknown> {
  const current = asRecord(existing);
  const events = Array.isArray(current.events) ? current.events : [];
  return {
    ...current,
    ...streamed,
    events: [...events, streamed],
  };
}

// The tool's own return value replaces the streamed state, but keeps the notification
// log that only the stream carried.
function mergeMcpFinalResult(existing: unknown, finalResult: unknown): unknown {
  const current = asRecord(existing);
  const finalRecord = asRecord(finalResult);
  if (Object.keys(finalRecord).length === 0) return finalResult;
  if (!Array.isArray(current.events)) return finalResult;
  return { ...finalRecord, events: current.events };
}


// A2A stream reduction — turn agent message parts into chat UI state. Shared by the
// attach stream and the replay path so both render identically.

interface ReduceState {
  messages: ChatMessage[];
  tasks: ChatTask[];
  lane: string | null; // id of the open assistant prose block, if any
  tokenUsage: TokenUsage | null; // latest cumulative token totals, if any reported
  // Per-source occurrence counter so every rendered message gets a key derived
  // from the stable server messageId (not its array position). This is what lets
  // older pages prepend without re-keying — and thus without remounting/re-animating
  // — the messages already on screen.
  keyCounts: Map<string, number>;
}

function newReduceState(): ReduceState {
  return {
    messages: [],
    tasks: [],
    lane: null,
    tokenUsage: null,
    keyCounts: new Map(),
  };
}

// A stable, position-independent id for a rendered message. Derived from the
// originating A2A messageId plus an occurrence counter (a single message can open
// more than one prose lane, e.g. text → tool call → text), so re-replaying the
// transcript with older pages prepended keeps every existing key byte-identical.
// Falls back to a position-based id only when no messageId is available.
function stableMessageId(state: ReduceState, prefix: string, sourceId: string | undefined): string {
  if (!sourceId) return `${prefix}-pos-${state.messages.length}`;
  const seen = state.keyCounts.get(sourceId) ?? 0;
  state.keyCounts.set(sourceId, seen + 1);
  return `${prefix}-${sourceId}-${seen}`;
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

// Close the in-flight thinking message and record the server-measured duration,
// so the indicator flips from "Thinking" to "Thought for Ns".
function finishRunningThinkingWithDuration(state: ReduceState, durationMs: number): void {
  state.messages = state.messages.map((message) =>
    isRunningThinkingMessage(message)
      ? { ...message, meta: { ...message.meta, status: "done", durationMs } }
      : message
  );
}

function finishActiveTools(state: ReduceState): void {
  state.messages = state.messages.map((message) =>
    message.role === "tool_call" && (message.meta?.status === "running" || message.meta?.status === "input_required")
      ? { ...message, meta: { ...message.meta, status: "completed" } }
      : message
  );
}

// The single path for the thinking signal — the iteration-start ping and any
// streamed reasoning. Ensures a running thinking message exists, then appends
// the reasoning text. A bare ping just keeps it alive (no body, no second
// placeholder stacked).
function applyThinking(state: ReduceState, text: string): void {
  let index = state.messages.findLastIndex(isRunningThinkingMessage);
  if (index === -1) {
    state.messages = [
      ...state.messages,
      { id: `status-${state.messages.length}`, role: "thinking", content: "", timestamp: new Date().toISOString(), meta: { status: "running" } },
    ];
    index = state.messages.length - 1;
  }
  if (!text) return;
  state.messages = state.messages.map((message, messageIndex) =>
    messageIndex === index ? { ...message, content: message.content + text } : message
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
  const lastContentBlockIndex = existingContentBlocks.length - 1;
  const lastContentBlock = existingContentBlocks[lastContentBlockIndex];
  const contentBlocks = lastContentBlock?.identifier === blockIdentifier
    ? existingContentBlocks.map((contentBlock, contentBlockIndex) =>
        contentBlockIndex === lastContentBlockIndex
          ? { ...contentBlock, content: contentBlock.content + text }
          : contentBlock
      )
    : [...existingContentBlocks, { identifier: blockIdentifier, content: text }];
  return { ...message, content: message.content + text, contentBlocks };
}

function pushAssistantText(state: ReduceState, text: string, blockIdentifier: string, sourceId?: string): void {
  if (!text) return;
  if (!blockIdentifier) throw new Error("Assistant text requires a content-block identity.");
  finishRunningThinking(state);
  if (state.lane === null) {
    const id = stableMessageId(state, "asst", sourceId);
    state.lane = id;
    state.messages = [
      ...state.messages,
      {
        id,
        role: "assistant",
        content: text,
        contentBlocks: [{ identifier: blockIdentifier, content: text }],
        timestamp: new Date().toISOString(),
      },
    ];
  } else {
    const laneId = state.lane;
    state.messages = state.messages.map((message) =>
      message.id === laneId ? appendAssistantContentBlock(message, text, blockIdentifier) : message
    );
  }
}

// Normalize the attachments carried by an `attachments` data payload into the lean
// shape the UI renders. Shared by the live send path (the outgoing dataPart) and
// replay (the persisted user-message data part), so a chip looks identical whether it
// was just sent or reloaded from history.
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

// A message a session received from outside itself: the user typed it, or another session
// sent it. `peerSender` is set only in the second case, and telling them apart is not
// cosmetic — a peer's report rendered as a user message attributes words to a person who
// never wrote them.
function reduceInboundMessage(state: ReduceState, message: A2AMessage, peerSender = ""): void {
  const text = (message.parts ?? []).filter((part) => part.kind === "text").map((part) => part.text ?? "").join("");
  const attachments = attachmentsFromMessage(message);
  // A user-role message with no visible prose AND no attachments carries nothing to
  // render. The live path renders nothing for these, so replay must match — never a
  // blank bubble. (Autonomous background-resume wakes are agent-authored, not user
  // messages, so they never reach this path at all.) A typed user turn always carries
  // prose; an attachment-only turn carries chips.
  if (!text.trim() && attachments.length === 0) return;
  state.lane = null;
  const meta = {
    ...(attachments.length > 0 ? { attachments } : {}),
    ...(peerSender ? { peerSender } : {}),
  };
  state.messages = [
    ...state.messages,
    {
      id: stableMessageId(state, peerSender ? "peer" : "user", message.messageId),
      role: peerSender ? "peer" : "user",
      content: text,
      timestamp: new Date().toISOString(),
      ...(Object.keys(meta).length > 0 ? { meta } : {}),
    },
  ];
}

// The single reduction of one part. Replay walks a stored message's parts through this;
// the live tail hands it each part as the session emits it. Both must go through the same
// code or the transcript you watch being written differs from the one you reload.
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

function steeringTextFromPart(part: A2APart | undefined): string {
  if (!part || part.kind !== "data") return "";
  const payload = partPayload(part.data);
  return payload.kind === "steering" ? String(payload.text ?? "").trim() : "";
}

function reduceDataPart(state: ReduceState, data: Record<string, unknown>, sourceId?: string): void {
  // Every event on this stream belongs to this session. A peer is a session of its own
  // with its own stream, so there is no longer a foreign lane to route away.
  // The one typed reader of a root wire event: switch on the generated union's
  // discriminant so a renamed kind or field is a compile error, not a silent "".
  // `data` stays in scope for the few helpers that take a raw record.
  const event = data as unknown as WireEvent;
  switch (event.kind) {
    case "steering": {
      const text = (event.text ?? "").trim();
      if (!text) break;
      state.lane = null;
      state.messages = [
        ...state.messages,
        { id: stableMessageId(state, "user", sourceId), role: "user", content: text, timestamp: new Date().toISOString() },
      ];
      break;
    }
    case "compaction": {
      // A context-compaction marker: "started" shows a live compacting indicator,
      // "done" turns it into the separator (or, when nothing was compacted, drops
      // it). Renders as a full-width divider — not an assistant/user bubble.
      state.lane = null;
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
      // The cumulative totals grow monotonically across the session, so the
      // latest part is authoritative on both the live stream and on replay.
      const cumulative = event.cumulative;
      // Per-call (latest) figures — this is the actual current context, not a sum.
      const contextInputTokens = event.input_tokens ?? 0;
      const contextOutputTokens = event.output_tokens ?? 0;
      state.tokenUsage = {
        inputTokens: cumulative?.input_tokens ?? 0,
        outputTokens: cumulative?.output_tokens ?? 0,
        totalTokens: cumulative?.total_tokens ?? 0,
        cacheReadTokens: cumulative?.cache_read_tokens ?? 0,
        reasoningTokens: cumulative?.reasoning_tokens ?? 0,
        modelCalls: cumulative?.model_calls ?? 0,
        contextInputTokens,
        contextOutputTokens,
        contextTokens: contextInputTokens + contextOutputTokens,
        contextWindow: event.context_window ?? 0,
      };
      break;
    }
    case "status": {
      // The model has finished thinking and is paused on tool execution. Tool
      // calls surface their own running/done status, so just close out any
      // in-flight thinking indicator. (The thinking ping itself is a `thinking`
      // event now, not a status — this only handles the wait edge.)
      if (event.code === "waiting_for_tools") finishRunningThinking(state);
      // A status (e.g. goal_check between answer attempts) ends the current prose
      // block, so the next text starts its own message instead of concatenating.
      state.lane = null;
      break;
    }
    case "thinking":
      // A new reasoning phase ends the current prose block — without this, text
      // emitted after a mid-turn think (or a control tool) merges into the prior
      // message and the thinking card lands out of order.
      state.lane = null;
      applyThinking(state, event.text ?? "");
      break;
    case "thinking_done":
      finishRunningThinkingWithDuration(state, event.duration_ms ?? 0);
      break;
    case "tool_call": {
      // Every tool call breaks the prose lane so surrounding text doesn't run together.
      state.lane = null;
      finishRunningThinking(state);
      const toolCallId = event.tool_call_id;
      state.messages = [
        ...state.messages,
        {
          id: `toolcall-${toolCallId || crypto.randomUUID()}`,
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
      state.lane = null;
      const toolName = event.tool_name;
      const toolCallId = event.tool_call_id;
      const currentMessage = state.messages.find((message) => messageMatchesToolEvent(message, toolName, toolCallId));
      const mergedResult = toolName === "call_mcp_tool" ? mergeMcpFinalResult(currentMessage?.meta?.result, event.display) : event.display;
      const resultStatus = statusFromWire(event.status);
      // set_tasks / update_tasks complete through this same universal path and carry
      // the authoritative task list for the side panel inside their result.
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
            id: `toolcall-${toolCallId || crypto.randomUUID()}`,
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
      // The approval lives on the tool call that triggered it — the card flips to
      // "input required" and shows the prompt inline, so the command (and later
      // its output) stay together. The event always carries the toolCallId.
      finishRunningThinking(state);
      const toolCallId = event.tool_call_id;
      const permission = {
        requestId: event.request_id,
        explanation: event.explanation || undefined,
        risk: event.risk || undefined,
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
        // No card to attach to. This used to be a `.map` that matched nothing and therefore
        // did nothing, so a permission request whose tool call had not been announced was
        // dropped in silence — the turn parked forever on a decision the person was never
        // shown. A prompt that needs an answer is never droppable: raise a card for it.
        const raised: ChatMessage = {
          id: stableMessageId(state, "tool", toolCallId),
          role: "tool_call",
          // The command is what is actually being asked for, and the event carries it, so
          // the card can say what it wants even with no tool call to name it.
          content: event.command ?? "",
          timestamp: new Date().toISOString(),
          meta: { toolCallId: toolCallId ?? "", status: "input_required", permission },
        };
        state.messages = [...state.messages, raised];
      }
      break;
    }
    case "question": {
      // An ask_user prompt attaches to the tool call that asked it, same lifecycle
      // as a permission: the card flips to "input required" and renders the
      // question inline; the tool result finalizes it once answered.
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
        // Mark the tool call failed with a generic result. The raw error text is
        // model-facing (the runtime already delivered it to the model via the tool
        // message) and must not leak to the UI — only a "Failed" indicator shows.
        let matched = false;
        state.messages = state.messages.map((message) =>
          messageMatchesToolEvent(message, toolName, toolCallId)
            ? (matched = true, { ...message, meta: { ...message.meta, status: "failed", result: { code: "tool_error" } } })
            : message
        );
        if (matched) break;
        // A tool-scoped error whose card we can't find is still model-facing (a
        // hidden envelope like `query`, or a card that never rendered). The
        // runtime already delivered the text to the model via the tool message,
        // so swallow it rather than raising a top-level red banner. Banners are
        // reserved for turn/system errors that carry no toolCallId.
        break;
      }
      pushErrorMessage(state, friendlyErrorFromData(data), sourceId);
      break;
    }
    case "warning": {
      finishRunningThinking(state);
      state.lane = null;
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
      // Streamed prose arrives as A2A text parts, not as a wire event, and a turn's end
      // is signalled by the attach stream's own `done` frame — neither is reduced here.
      break;
    default: {
      // Exhaustiveness: a new WireEvent kind that is not handled above is a compile error.
      const _exhaustive: never = event;
      void _exhaustive;
      break;
    }
  }
}

// Reconstruct messages from a session's persisted A2A tasks. The history arrives
// already compacted server-side (adjacent same-kind deltas merged), so it is reduced
// as-is — no client-side compaction pass. Tasks that reference another task are a
// peer's work, not this session's turns, and are filtered out: a peer is its own
// session with its own transcript, never a lane inside this one.
function replayTurns(turns: A2ATurn[]): {
  messages: ChatMessage[];
  tasks: ChatTask[];
  tokenUsage: TokenUsage | null;
  keyCounts: Map<string, number>;
} {
  const mainTurns = turns
    .filter((turn) => !(turnState(turn).referenceTurnIds ?? []).length)
    .sort((first, second) => String(first.status?.timestamp ?? "").localeCompare(String(second.status?.timestamp ?? "")));
  const state: ReduceState = newReduceState();
  for (const turn of mainTurns) {
    state.lane = null;
    // A turn's full message stream is its history PLUS its trailing status
    // message: the A2A TaskManager keeps the latest message on `status.message`
    // and only folds it into `history` on the *next* status update. A turn
    // suspended mid-flight (e.g. awaiting a permission) never gets that next
    // update, so its last message — the pending request — lives only there.
    // Reduce both so replay reproduces exactly what the live stream showed
    // instead of dropping the trailing message and leaving the card stuck.
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
  workspaceStrategy: WorkspaceStrategy = "none",
  permissionMode: PermissionMode = "default",
  // Whether a turn is currently running on this session (from the server-tracked
  // running set). Drives the live subscribe stream when we are viewing — but not
  // driving — it.
  sessionRunning: boolean = false,
  // The project this session belongs to; rides in the turn metadata so the server
  // resolves the project's locations for the agent to address per tool call.
  projectId: string = ""
) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [tasks, setTasks] = useState<ChatTask[]>([]);
  const [tokenUsage, setTokenUsage] = useState<TokenUsage | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(initialSessionId);
  const [isStreaming, setIsStreaming] = useState(false);
  const [isHistoryLoading, setIsHistoryLoading] = useState(!!initialSessionId);
  // Set when every attempt to load a session's transcript failed, so the panel can
  // offer a retry instead of showing a permanently blank conversation that only a
  // full page reload would recover.
  const [historyError, setHistoryError] = useState(false);
  const [hasOlderHistory, setHasOlderHistory] = useState(false);
  const [isOlderHistoryLoading, setIsOlderHistoryLoading] = useState(false);
  // Bumped to force the history-load effect to re-run (a manual retry).
  const [historyReloadNonce, setHistoryReloadNonce] = useState(0);
  const [queuedMessages, setQueuedMessages] = useState<QueuedMessage[]>([]);

  // This hook's own attach subscription while it is driving a turn. A turn is sent
  // and then observed here; closing it only drops the client end.
  const attachRef = useRef<{ abort: () => void } | null>(null);
  const stateRef = useRef<ReduceState>(newReduceState());
  const sessionIdRef = useRef<string | null>(initialSessionId);
  const historyLoadedForRef = useRef<string | null>(null);
  const historyFragmentsRef = useRef<A2ATurn[]>([]);
  const historyPageCursorRef = useRef<number | null>(null);
  const hasOlderHistoryRef = useRef(false);
  const isOlderHistoryLoadingRef = useRef(false);
  const queuedMessagesRef = useRef<QueuedMessage[]>([]);
  const isStreamingRef = useRef(false);
  // Set by a user Stop so the imminent stream-close does not auto-drain the queue
  // into a fresh turn — which read as "Stop didn't stop it". Queued messages are
  // left intact (not sent, not lost) for the user to send when they choose; the
  // flag is one-shot, reset as soon as the aborted stream closes.
  const abortedByUserRef = useRef(false);
  const errorToastKeysRef = useRef<Set<string>>(new Set());
  // Tracks whether this session was running, so we do a final refresh when its
  // turn finishes (the subscribe stream closes once it is no longer running).
  const wasRunningRef = useRef(false);
  // True once we have driven a turn in this mount. For such a session the live
  // SSE is authoritative, so we never subscribe to the read-only stream (it would
  // replace the live state with a replay and churn message ids).
  const streamedLocallyRef = useRef(false);
  const runStreamRef = useRef<(input: ChatInput) => void>(() => {});
  const flushFrameRef = useRef<number | null>(null);

  const setQueue = useCallback((next: QueuedMessage[]) => {
    queuedMessagesRef.current = next;
    setQueuedMessages(next);
  }, []);

  const acknowledgeSteering = useCallback((text: string) => {
    if (!text) return;
    const index = queuedMessagesRef.current.findIndex((message) => message.steering && message.text === text);
    if (index === -1) return;
    setQueue(queuedMessagesRef.current.filter((_, messageIndex) => messageIndex !== index));
  }, [setQueue]);

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
      lane: null,
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

  // On unmount (e.g. switching sessions), close this hook's SSE connection so it
  // does not leak — a stack of orphaned streams exhausts the browser's per-host
  // connection pool and hangs later fetches. This only drops the client end; the
  // backend turn keeps running (the producer is not tied to the consumer), and a
  // sidebar spinner reflects that it is still active.
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
    // A running session we're not driving is loaded by the live subscribe stream
    // (snapshot catch-up + live tail). Skip the REST load here so the two paths
    // can't race and clobber each other's state at connect time.
    if (sessionRunning && !streamedLocallyRef.current) {
      historyLoadedForRef.current = initialSessionId;
      return;
    }
    historyLoadedForRef.current = initialSessionId;
    let cancelled = false;
    // Abort the in-flight fetch when the session switches or the panel unmounts —
    // an orphaned request would keep a connection occupied (rapid switching can
    // exhaust the browser's per-host pool and stall the next load) and could write
    // a stale transcript after we have moved on.
    const controller = new AbortController();
    setHistoryError(false);
    setHasOlderHistory(false);
    setIsHistoryLoading(true);

    // A dropped fetch (a momentary connection-pool exhaustion from switching
    // sessions quickly, a transient 5xx) used to leave the transcript blank with no
    // recovery but a manual reload. Retry a few times with a short backoff so the
    // common transient case heals itself, and only surface an error if every
    // attempt fails.
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
            swallowed("could not load the transcript after retrying", caught);
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

  // Transparent history fill. Once the newest page is on screen, pull every
  // remaining older page back-to-back — no artificial delay — accumulating them in
  // a ref WITHOUT re-rendering. A single prepend at the very end drops the whole
  // history in at once, above the (bottom-pinned) viewport, so it lands instantly
  // and invisibly instead of streaming page-by-page. Stable message keys (see
  // stableMessageId) mean that one prepend leaves every on-screen message untouched
  // — no remount, no flash, no layout shift.
  const drainOlderHistory = useCallback(async () => {
    const ctx = sessionIdRef.current;
    if (!ctx || isOlderHistoryLoadingRef.current) return;
    if (historyPageCursorRef.current == null || !hasOlderHistoryRef.current) return;
    // Never fill history over the top of a live turn: applying replayed history would
    // rebuild state from the server transcript and drop the just-sent (not-yet-
    // persisted) turn from view. Once the user drives this session, we leave older
    // pages unloaded for the mount — the newest page they care about is already in.
    if (streamedLocallyRef.current || isStreamingRef.current) return;
    isOlderHistoryLoadingRef.current = true;
    setIsOlderHistoryLoading(true);
    let fetchedAny = false;
    try {
      let cursor: number | null = historyPageCursorRef.current;
      while (cursor != null && hasOlderHistoryRef.current) {
        if (streamedLocallyRef.current || isStreamingRef.current) return;
        const page = await fetchSessionTurnsPage(ctx, cursor, undefined, HISTORY_PAGE_LIMIT);
        if (sessionIdRef.current !== ctx) return;
        historyFragmentsRef.current = [...page.turns, ...historyFragmentsRef.current];
        historyPageCursorRef.current = page.next_before_row_id;
        hasOlderHistoryRef.current = page.has_more;
        cursor = page.next_before_row_id;
        fetchedAny = true;
      }
    } catch (caught) {
      // Leave what we have loaded; the fill effect re-triggers and resumes from the
      // last cursor since hasOlderHistoryRef is still true. Still reported: resuming is the
      // recovery, not the reason, and a drain that fails every time looks identical to one
      // that simply reached the end.
      swallowed("could not load older history", caught);
    } finally {
      // Skip the apply if a local turn began mid-drain (guarded above too) — the
      // fetched fragments stay in the ref, unused, rather than clobbering live state.
      if (fetchedAny && sessionIdRef.current === ctx && !streamedLocallyRef.current && !isStreamingRef.current) {
        setHasOlderHistory(hasOlderHistoryRef.current);
        // One replay + render for the whole accumulated transcript.
        applyHistoryFragments();
      }
      isOlderHistoryLoadingRef.current = false;
      setIsOlderHistoryLoading(false);
    }
  }, [applyHistoryFragments]);

  // Kick the drain once the newest page is in and settled — but not while a turn is
  // streaming (see the clobber guard inside). Runs a single time per session (the
  // drain clears hasOlderHistory when fully loaded); if a fetch fails mid-drain it
  // re-triggers to resume.
  useEffect(() => {
    if (isHistoryLoading || isOlderHistoryLoading || isStreaming || !hasOlderHistory) return;
    void drainOlderHistory();
  }, [isHistoryLoading, isOlderHistoryLoading, isStreaming, hasOlderHistory, drainOlderHistory]);

  // Live updates for a session that is running but which this hook is not driving.
  // Attaches to the session: one compacted snapshot (catch-up) then a live tail of each
  // emitted part, applied as O(delta) updates through the same reducer the driver uses —
  // instead of polling and re-replaying the whole transcript every second. While this
  // hook drives a turn it holds its own attach, so this one stays closed rather than
  // opening a second stream onto the same session.
  useEffect(() => {
    if (!initialSessionId) return;

    let cancelled = false;
    let subscription: { abort: () => void } | null = null;
    const controller = new AbortController();

    const applySnapshot = (turns: A2ATurn[]) => {
      const replayed = replayTurns(turns);
      stateRef.current = {
        messages: replayed.messages,
        tasks: replayed.tasks,
        lane: null,
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
            // The turn ended, said by the session itself. The `sessionRunning` flip below
            // learns the same thing from a polled listing a moment later; this arrives on
            // the stream we are already holding, so the result artifact — written only as
            // the task closes, and therefore never on the live tail — lands immediately
            // instead of one poll interval after the work finished.
            void (async () => {
              try {
                const tasks = await fetchSessionTurns(initialSessionId, controller.signal);
                if (!cancelled && !isStreamingRef.current && !streamedLocallyRef.current) applySnapshot(tasks);
              } catch (caught) {
                // The `sessionRunning` path below captures the same terminal state a moment
                // later, so this is recoverable — but not silent, or a store that never
                // answers is indistinguishable from one that answers slowly.
                swallowed("could not read the finished turn", caught);
              }
            })();
          }
        },
        () => {
          // Stream closed (turn finished or dropped). The sessionRunning flip to
          // false re-runs this effect and the else branch captures final state.
        },
      );
    } else if (!sessionRunning && wasRunningRef.current) {
      // The turn just finished — capture its terminal state once (the live tail misses
      // the turn's result artifact, which is only written as the task completes), then
      // stop.
      wasRunningRef.current = false;
      void (async () => {
        try {
          const tasks = await fetchSessionTurns(initialSessionId, controller.signal);
          if (!cancelled && !isStreamingRef.current && !streamedLocallyRef.current) applySnapshot(tasks);
        } catch (caught) {
          // Leave the last live state in place; it is very nearly the terminal state.
          swallowed("could not read the session's final state", caught);
        }
      })();
    }

    return () => {
      cancelled = true;
      controller.abort();
      subscription?.abort();
    };
  }, [sessionRunning, initialSessionId, isStreaming, flushNow, flush]);

  // Manual retry after the transcript failed to load — re-run the history-load
  // effect from scratch (clearing the per-session guard so it actually re-fetches).
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

  const runStream = useCallback(
    (input: ChatInput) => {
      // Optimistic input message + reset the open prose lane.
      stateRef.current.lane = null;
      const userMessageId = crypto.randomUUID();
      const dataParts = input.dataParts ?? [];
      const attachments = dataParts.flatMap((dataPart) => attachmentsFromData(dataPart));
      const meta = attachments.length > 0 ? { attachments } : {};
      stateRef.current.messages = [
        ...stateRef.current.messages,
        {
          id: stableMessageId(stateRef.current, "user", userMessageId),
          role: "user",
          content: input.text,
          timestamp: new Date().toISOString(),
          ...(Object.keys(meta).length > 0 ? { meta } : {}),
        },
      ];
      flush();

      isStreamingRef.current = true;
      streamedLocallyRef.current = true;
      setIsStreaming(true);

      const text = input.text;

      // The turn is over, however it ended. Three things can end it, and they are not
      // ordered: the session says so with a `turn` frame (the normal case), the stream
      // closes under us (the session itself ended, or the connection dropped), or the
      // opening calls threw. Whichever lands first is the one that counts.
      //
      // This guard is load-bearing, not defensive: ending the turn closes our stream,
      // which fires the stream's own `onDone` straight back into here.
      let settled = false;
      const finishTurn = () => {
        if (settled) return;
        settled = true;
        // Stop watching. The attach stream belongs to the *session* and would otherwise
        // stay open across every following turn — one leaked connection per turn, against
        // a browser limit of six per host. The read-only effect below picks the session
        // back up if the harness wakes it on its own.
        attachRef.current?.abort();
        attachRef.current = null;
        stateRef.current.lane = null;
        // A clean turn already settled its cards from the events it emitted; but a Stop, or
        // a connection that drops mid-turn (daemon stall, network loss), ends it with no
        // terminal event, which would otherwise leave every in-flight tool/thinking card
        // spinning forever. Sweep here as the single catch-all so a card can never outlive
        // its turn.
        finishRunningThinking(stateRef.current);
        finishActiveTools(stateRef.current);
        // A message still flagged `steering` was already delivered by `send`: a message
        // to a live session is safe-point injected, so it is never dropped and must not
        // be sent a second time. Retire the chip — if the turn ended before the
        // injection landed, that same message simply starts the next turn and comes
        // back over attach.
        const pendingText = queuedMessagesRef.current.filter((message) => !message.steering);
        if (pendingText.length !== queuedMessagesRef.current.length) setQueue(pendingText);
        flush();
        // A user Stop ends the turn; do not immediately relaunch a queued
        // message as a new turn — that is exactly the "Stop didn't stop" symptom.
        // Consume the one-shot flag and fall through to idle, leaving the queue for
        // the user to send deliberately.
        const abortedByUser = abortedByUserRef.current;
        abortedByUserRef.current = false;
        if (!abortedByUser && pendingText.length > 0) {
          const next = pendingText[0];
          setQueue(queuedMessagesRef.current.filter((message) => message.id !== next.id));
          runStreamRef.current({ kind: "text", text: next.text, dataParts: next.dataParts });
        } else {
          isStreamingRef.current = false;
          setIsStreaming(false);
          // Our locally-driven turn is over. Return to viewer mode so that if the
          // harness later wakes this session on its own (an autonomous background
          // resume), the read-only attach picks the new turn up live instead of the
          // wake only appearing on a manual reload.
          streamedLocallyRef.current = false;
        }
      };

      const observe = (sessionIdentifier: string) => {
        attachRef.current = attachSession(
          sessionIdentifier,
          (frame) => {
            // The one frame that says the turn ended. Ignoring it left the client waiting
            // for the stream to close instead — which it does not do, because the stream
            // is the session's and the session goes idle many times over its life. The
            // turn then never ended as far as the interface was concerned: Stop stayed up,
            // the composer stayed in queue-a-message mode, and every later send was routed
            // as steering into a turn that had long since finished.
            if (frame.kind === "turn") {
              if (!frame.running) finishTurn();
              return;
            }
            // A snapshot is the attach stream's catch-up for a viewer joining mid-turn.
            // We are driving, so our own state already includes the message we just
            // sent and replacing it would drop it from view until it persists.
            if (frame.kind !== "live") return;
            acknowledgeSteering(steeringTextFromPart(frame.part));
            notifyTurnError(frame.part);
            reduceAgentPart(stateRef.current, frame.part);
            flush();
          },
          finishTurn,
        );
      };

      // A turn is two calls: make sure a session exists, then send it a message. The
      // session is where the agent, the directory, the workspace and the permission mode
      // are fixed — `send` carries none of them, and can therefore never change them.
      void (async () => {
        try {
          let sessionIdentifier = sessionIdRef.current;
          if (!sessionIdentifier) {
            const created = await sessionCreate({
              agent,
              workingDirectory,
              workspaceStrategy,
              permissionMode,
              projectId,
            });
            sessionIdentifier = created.id;
            sessionIdRef.current = created.id;
            setSessionId(created.id);
          }
          // Attach before sending: the worker starts emitting the moment it accepts the
          // message, and a subscription opened afterwards would miss the opening frames.
          observe(sessionIdentifier);
          await sessionSend(sessionIdentifier, messageParts(text, dataParts), { messageId: userMessageId });
        } catch (caught) {
          // The reason, not just the fact. This was a bare `catch {}`, so whatever actually
          // went wrong — which call, which status, which network failure — was discarded and
          // replaced with advice to read a daemon log that, for a failure on this side of the
          // wire, has nothing in it. That sent an investigation looking in the wrong process.
          const detail = caught instanceof Error ? caught.message : String(caught);
          console.error("[frank] could not start the turn:", caught);
          pushErrorMessage(stateRef.current, {
            code: "server_error",
            title: "Server request failed",
            message: `Frank could not start the turn: ${detail}`,
          });
          // One wind-down for every ending, so the queue is drained exactly once however
          // the turn failed. `finishTurn` closes the stream itself if one was opened.
          finishTurn();
        }
      })();
    },
    [agent, workingDirectory, workspaceStrategy, permissionMode, projectId, flush, setQueue, acknowledgeSteering, notifyTurnError]
  );

  useEffect(() => {
    runStreamRef.current = runStream;
  }, [runStream]);

  const send = useCallback(
    (text: string, dataParts: Record<string, unknown>[] = [], queueOnly = false) => {
      const trimmed = text.trim();
      if (!trimmed) return Promise.resolve();
      if (isStreamingRef.current) {
        const pending = { id: crypto.randomUUID(), text: trimmed, steering: false, dataParts };
        const ctx = sessionIdRef.current;
        // Steering is no longer a separate call: a message to a live session is sent the
        // same way as any other and injected at the next safe point. The chip says so
        // until the session echoes the steering event back.
        // While the turn is paused on a pending decision (a permission or question
        // prompt), a new message is plain-queued instead — it could not be injected
        // until the decision is resolved anyway, and a "Steering next opening" chip
        // would misrepresent that. It drains when the turn ends.
        if (ctx && !queueOnly && dataParts.length === 0) {
          setQueue([...queuedMessagesRef.current, { ...pending, steering: true }]);
          return sessionSend(ctx, messageParts(trimmed), { messageId: pending.id }).catch(() => {
            // The send never reached the daemon, so nothing was injected. Keep it as an
            // ordinary queued message so it drives its own turn instead of vanishing.
            setQueue(queuedMessagesRef.current.map((message) =>
              message.id === pending.id ? { ...message, steering: false } : message
            ));
          });
        }
        setQueue([...queuedMessagesRef.current, pending]);
        return Promise.resolve();
      }
      runStream({ kind: "text", text: trimmed, dataParts });
      return Promise.resolve();
    },
    [runStream, setQueue]
  );

  // Settle a stuck prompt card and tell the user when their decision/answer could
  // not be delivered — a dropped connection, or a request that already expired
  // because the turn moved on. Without this the card stays at "input required" and
  // the composer stays gated, with no hint that the click was lost.
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
      title: kind === "decision" ? "Couldn't submit your decision" : "Couldn't submit your answer",
      description: status === "network"
        ? "The server did not respond. Check the connection and try again."
        : "This request is no longer active — the turn may have already moved on.",
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

  // Allow-always is gone with the mid-session policy it used to write: a decision is
  // per call, and the mode it would have amended is fixed when the session is created.
  const handlePermission = useCallback(
    async (requestId: string, decision: "deny" | "allow_once") => {
      const ctx = sessionIdRef.current;
      if (!ctx) return;
      const result = await resolvePermission(ctx, requestId, decision);
      if (result.status === "stale" || result.status === "unknown") {
        settleInactivePrompt(requestId);
        return;
      }
      if (!result.ok) {
        notifyResolveFailure(requestId, "decision", result.status);
        return;
      }
      // Record the decision and hand the card back to its normal lifecycle: an
      // approval resumes as "running" (the result/error finalizes it), a denial
      // is settled by the error the backend emits for this tool call.
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
      const ctx = sessionIdRef.current;
      if (!ctx) return;
      const result = await resolveQuestion(ctx, requestId, answers);
      if (result.status === "stale" || result.status === "unknown") {
        settleInactivePrompt(requestId);
        return;
      }
      if (!result.ok) {
        notifyResolveFailure(requestId, "answer", result.status);
        return;
      }
      // Record the answer and hand the card back to its running lifecycle; the
      // tool_result finalizes it (same shape as a resolved permission).
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

  // The user dismissed the whole question prompt without answering. Report the
  // decline to the model (which stops the turn cleanly server-side) and settle the
  // card. Distinct from Stop: it does not tear the turn down, it hands the model a
  // "declined — stop and await" result and lets the turn end on its own.
  const declineQuestion = useCallback(
    async (requestId: string) => {
      const ctx = sessionIdRef.current;
      if (!ctx) return;
      const result = await resolveQuestion(ctx, requestId, [], true);
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
    const ctx = sessionIdRef.current;
    // Suppress the queue auto-drain that the imminent stream close would otherwise
    // trigger, so Stop halts everything instead of relaunching a queued follow-up.
    abortedByUserRef.current = true;
    if (!ctx) {
      // The session was never created (Stop landed while `create` was still in flight);
      // there is nothing to cancel, so just close whatever stream is open.
      attachRef.current?.abort();
      return Promise.resolve();
    }
    // A Stop while the turn is paused on a decision auto-settles that decision:
    // deny every pending permission and cancel every pending question, so the
    // request is answered rather than left hanging. Without this the tool cards
    // stay stuck at "input required" after the turn is torn down, and the backend
    // permission future is only abandoned (never cleanly rejected).
    let settledAny = false;
    for (const message of stateRef.current.messages) {
      if (message.role !== "tool_call" || message.meta?.status !== "input_required") continue;
      const permission = message.meta?.permission;
      const question = message.meta?.question;
      if (permission?.requestId) {
        settledAny = true;
        void resolvePermission(ctx, permission.requestId, "deny");
      } else if (question?.requestId) {
        settledAny = true;
        // Stop while a question is open settles it as a decline (not empty
        // answers): the model is told the user declined and the turn ends, which
        // also cleanly resolves the awaiting tool even if the context teardown races.
        void resolveQuestion(ctx, question.requestId, [], true);
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
    // Tell the user if the stop request never reached the server — the turn may still
    // be running, and silently doing nothing would leave them stuck expecting it to end.
    return cancelTurn(ctx).then((ok) => {
      if (!ok) {
        toaster.create({
          type: "error",
          title: "Couldn't stop the turn",
          description: "The server did not confirm the stop. It may still be running — check the connection and retry.",
          closable: true,
        });
      }
    });
  }, [flush]);

  // Kick off a manual context compaction. The compacting indicator and separator
  // arrive over the stream (live for the driver, via the subscribe stream for a
  // viewer), so there is nothing to render optimistically here.
  const compact = useCallback(() => {
    const ctx = sessionIdRef.current;
    if (!ctx) return;
    void compactSession(ctx).then((ok) => {
      if (!ok) {
        toaster.create({
          type: "error",
          title: "Couldn't compact the context",
          description: "The server did not start compaction. Check the connection and try again.",
          closable: true,
        });
      }
    });
  }, []);

  const abortTool = useCallback((toolCallId: string) => {
    const ctx = sessionIdRef.current;
    if (!ctx || !toolCallId) return;
    void abortToolCall(ctx, toolCallId).then((ok) => {
      if (!ok) {
        toaster.create({
          type: "error",
          title: "Couldn't stop that tool call",
          description: "The server did not confirm the cancellation. Check the connection and retry.",
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
    setQueue(queuedMessagesRef.current.filter((_, messageIndex) => messageIndex !== index));
  }, [setQueue]);

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
    handlePermission,
    handleQuestion,
    declineQuestion,
  };
}
