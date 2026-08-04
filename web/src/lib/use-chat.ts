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
  // What a cache could have returned this session — the denominator `cacheReadTokens` means
  // something against. See the composer's usage tooltip.
  cacheReachableTokens: number;
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
  // What the latest call's cache actually did, and why. A running total cannot say which call
  // missed, and that is the whole question — a session reading 2% overall was one partial hit
  // and five outright misses. `prefixIntact` with `contextCacheReadTokens` at zero means the
  // request was byte-identical as far as it went and the provider missed anyway.
  contextCacheReadTokens: number;
  reachableTokens: number;
  prefixIntact: boolean;
  divergence: PrefixDivergence | null;
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
      // The daemon sends the numbers it has (the window, the model) in `message`, so this is only
      // the fallback for when it could not name them. It is its own case rather than folded into
      // the default: an overlong conversation is the one turn failure the user can fix directly,
      // and it used to arrive as "the turn stopped unexpectedly, see the server log".
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
  tokenUsage: TokenUsage | null; // latest cumulative token totals, if any reported
  // Per-source occurrence counter so every rendered message gets a key derived
  // from the stable server messageId (not its array position). This is what lets
  // older pages prepend without re-keying — and thus without remounting/re-animating
  // — the messages already on screen.
  keyCounts: Map<string, number>;
}

// Whether a replayed transcript would render exactly what is already on screen.
//
// This is the whole of the end-of-stream flash. A row built from the live tail is keyed
// `asst-anon-0` — the tail is part-granular, so the message id the store will file it under does
// not exist yet — while the same row replayed from the store is keyed `asst-<messageId>-0`. When
// a turn ends, the terminal state is fetched and applied, no key survives the swap, and React
// unmounts and remounts every row in the conversation. Nothing has changed on screen; everything
// is rebuilt anyway, and the repaint is the flash.
//
// The snapshot is still worth fetching: it carries the turn's result artifact, which is written
// only as the task closes and therefore never appears on the live tail. But that artifact is
// appended *only* when the tail produced no assistant text — so in the ordinary case, where the
// model answered, the replay is identical to what is displayed and applying it buys nothing at
// all. Comparing first means the common case costs one comparison and no render, and the case
// that genuinely differs still gets its update.
//
// Compared on what renders, not on identity: the ids differ by construction, which is the
// premise. Roles and text first because they are cheap and almost always settle it; `meta` (a
// tool's status and result, a permission, an attachment) only for a transcript that has already
// matched on everything else.
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

// A stable, position-independent id for a rendered message. Derived from the
// originating A2A messageId plus an occurrence counter (a single message can open
// more than one prose block, e.g. text, then a tool call, then text), so re-replaying the
// transcript with older pages prepended keeps every existing key byte-identical.
// Falls back to a position-based id only when no messageId is available.
function toolCallMessageId(toolCallId: string | undefined): string {
  return `toolcall-${toolCallId || clientIdentifier()}`;
}

function stableMessageId(state: ReduceState, prefix: string, sourceId: string | undefined): string {
  if (!sourceId) {
    // A monotonic counter, not `state.messages.length`. Keying on the length made a row's
    // identity depend on how many rows happened to precede it, so anything inserted earlier
    // in the transcript renumbered every row after it. React then saw those rows as new,
    // mounted fresh copies while the old ones were still fading out, and — because the
    // outgoing copies were still in the layout — the timeline briefly rendered both. That is
    // the shift: the conversation grows by the height of the duplicates, then snaps back.
    const issued = state.keyCounts.get("") ?? 0;
    state.keyCounts.set("", issued + 1);
    return `${prefix}-anon-${issued}`;
  }
  // A message that has an id *is* that id, and the row is the same row every time it is
  // reduced. The counter that used to be appended here made identity depend on how many
  // times a reducer happened to see the message: the copy rendered optimistically on send
  // and the copy that came back from the server carried the same `messageId` and still
  // landed on two different keys, so the transcript showed the message twice until a replay
  // rebuilt the state from scratch and it collapsed back to one.
  //
  // Deterministic here, and idempotent at the insert (see `upsertMessage`). Together those
  // two make a duplicate unrepresentable rather than merely unlikely — whichever path
  // reduces the message, and however many times.
  return `${prefix}-${sourceId}`;
}

function upsertMessage(state: ReduceState, message: ChatMessage): void {
  // Replace in place when the row already exists, append when it does not. The transcript is
  // a projection of messages the server owns, and the same message arriving twice — once
  // optimistically, once echoed — must converge on one row rather than accumulate.
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

// Close the in-flight thinking message and record the server-measured duration,
// so the indicator flips from "Thinking" to "Thought for Ns".
function finishRunningThinkingWithDuration(state: ReduceState, durationMs: number): void {
  state.messages = state.messages.map((message) =>
    isRunningThinkingMessage(message)
      ? { ...message, meta: { ...message.meta, status: "done", durationMs } }
      : message
  );
}

// Open the "Thinking" row the moment a turn is sent, before anything comes back.
//
// The wait it covers is real and was invisible: a turn spends a beat on the create/attach/send
// round trip and the session's own preamble before the model produces its first token, and for
// all of it the transcript sat exactly as it had been — so a send that was working looked like
// a send that had not registered. The row carries no claim about the model; it says the turn is
// under way, which is true from the instant it is sent. Reasoning that arrives flows into this
// same row, so nothing jumps when it does.
// Close the tool cards that were still spinning when the turn ended, so a card cannot
// outlive its turn.
//
// `input_required` is deliberately NOT swept, and that is the whole point of this function
// having a narrow rule. A suspension *is* how a turn ends: the turn parks, the stream closes,
// and this runs — so sweeping a pending decision to `completed` erased the prompt a person
// was being asked to answer, seconds after it appeared and before they could reach it. The
// gate stayed open on the daemon, so the session was then stuck on a question with no card,
// and only reopening the session brought it back, because replay rebuilds it from the
// persisted turn.
//
// A spinner and a question are not the same thing. A spinner has nobody left to finish it
// once the stream is gone; a question is waiting for a person and stays valid until they
// answer it or Stop settles it, which the Stop path does explicitly.
function finishActiveTools(state: ReduceState): void {
  state.messages = state.messages.map((message) =>
    message.role === "tool_call" && message.meta?.status === "running"
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
  // The session is reasoning, so a row the client opened optimistically is now that
  // reasoning phase and stops being provisional — even for a bare ping with no text yet.
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
  // Merged by identity, not by position. The identifier names one block the provider is
  // writing, and a later delta for it belongs in it wherever it has ended up — matching only
  // the *last* block meant a delta that arrived after any other block had opened was filed as
  // a second block under a name already taken.
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

/**
 * The message already holding this block of prose, if there is one.
 *
 * Prose is grouped by the identity the model gives it, and by nothing else. An identifier names
 * one block of text a model call is writing: every delta of it carries the same one, and no two
 * blocks share one. So a delta whose identifier is already on screen belongs in that message,
 * however many reasoning deltas, statuses or tool calls arrived in between.
 *
 * This replaced a mutable "is a prose message open" flag that ten different events cleared. It
 * was a guess about ordering standing in for an identity that was right there, and it guessed
 * wrong whenever the provider interleaved anything mid-sentence: the tail of the sentence became
 * a second bubble, landing on its own line or below the tool calls that followed it. Only a
 * reload put it right, because replay rebuilds prose from the finished artifact rather than from
 * the deltas — which is the clearest sign the deltas were being grouped by the wrong thing.
 */
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
  const meta = {
    ...(attachments.length > 0 ? { attachments } : {}),
    ...(peerSender ? { peerSender } : {}),
  };
  upsertMessage(state, {
    id: stableMessageId(state, peerSender ? "peer" : "user", message.messageId),
    role: peerSender ? "peer" : "user",
    content: text,
    timestamp: new Date().toISOString(),
    ...(Object.keys(meta).length > 0 ? { meta } : {}),
  });
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

function steeringFromPart(part: A2APart | undefined): { text: string; messageId: string } {
  const none = { text: "", messageId: "" };
  if (!part || part.kind !== "data") return none;
  const payload = partPayload(part.data);
  // Only the person's own steering, which is what the composer's queue holds. A peer's message
  // arriving mid-turn reads as steering on the wire and would otherwise retire a chip for a
  // message the person has not sent yet.
  if (payload.kind !== "steering" || String(payload.peer_sender ?? "")) return none;
  // The id travels with the event for exactly this purpose — see `SteeringEvent.message_id`.
  return { text: String(payload.text ?? "").trim(), messageId: String(payload.message_id ?? "").trim() };
}

function reduceDataPart(state: ReduceState, data: Record<string, unknown>, sourceId?: string): void {
  // Every event on this stream belongs to this session. A peer is a session of its own
  // with its own stream, so there is no longer a foreign transcript to route away.
  // The one typed reader of a root wire event: switch on the generated union's
  // discriminant so a renamed kind or field is a compile error, not a silent "".
  // `data` stays in scope for the few helpers that take a raw record.
  const event = data as unknown as WireEvent;
  switch (event.kind) {
    case "steering": {
      const text = (event.text ?? "").trim();
      if (!text) break;
      // Delivered once, however many times it arrives.
      //
      // A steering message reaches the transcript through an event on the *agent* stream, and
      // that stream is re-read: attaching to a session replays the turns it already holds, so
      // the same event is reduced again when the next turn opens. Every other event survives
      // that — a tool row replaces the row with its id, assistant text is rebuilt from the
      // artifact — but this one appended, so the message a person had steered with appeared a
      // second time under a fresh key, one turn later.
      //
      // Keyed on the id this client gave the message when it sent it, which the session now
      // echoes back. That is what makes the live copy and the persisted copy one message: a
      // replay of the turn rebuilds the list from the server's own record, and the entry it
      // produces lands on the same key rather than beside it. Keying on the text meant the two
      // were different messages that happened to read alike, so both were on screen until the
      // rebuild dropped one — visible as a duplicate that flickered away.
      //
      // The fallback is the old rule, for a steering message this client did not send: keyed on
      // the arrival *and* the text, since two drained into one opening are genuinely two while a
      // replay repeats both together.
      // Who sent it. A message that reaches a session mid-turn is injected into the running
      // turn, and everything that arrives that way used to be drawn as the person's own words —
      // a peer's report, and the daemon's notice that a session this one created had died. The
      // transcript showed the user saying things they had never written.
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
      // A context-compaction marker: "started" shows a live compacting indicator,
      // "done" turns it into the separator (or, when nothing was compacted, drops
      // it). Renders as a full-width divider — not an assistant/user bubble.
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
      // The model has finished thinking and is paused on tool execution. Tool
      // calls surface their own running/done status, so just close out any
      // in-flight thinking indicator. (The thinking ping itself is a `thinking`
      // event now, not a status — this only handles the wait edge.)
      if (event.code === "waiting_for_tools") finishRunningThinking(state);
      // A status (e.g. goal_check between answer attempts) ends the current prose
      // block, so the next text starts its own message instead of concatenating.
      break;
    }
    case "thinking":
      // A new reasoning phase ends the current prose block — without this, text
      // emitted after a mid-turn think (or a control tool) merges into the prior
      // message and the thinking card lands out of order.
      applyThinking(state, event.text ?? "");
      break;
    case "thinking_done":
      finishRunningThinkingWithDuration(state, event.duration_ms ?? 0);
      break;
    case "tool_call": {
      // Text either side of a tool call is separate prose, and says so: the model gives
      // each block its own identity, so nothing here has to force the split.
      finishRunningThinking(state);
      const toolCallId = event.tool_call_id;
      // One row per tool call, always. This used to append unconditionally, which is only
      // correct if a call is announced exactly once — and it is not. A turn that stops for
      // approval announces the call to raise the prompt, then announces it again when it
      // actually runs, so the transcript ended up holding the same call twice under the same
      // id. React says what that costs out loud: "Encountered two children with the same key
      // … may cause children to be duplicated and/or omitted". The duplicates are what made
      // the transcript grow and snap back, and what made a group of two failures count four.
      const existing = state.messages.findIndex(
        (message) => message.role === "tool_call" && String(message.meta?.toolCallId ?? "") === toolCallId,
      );
      if (existing >= 0 && toolCallId) {
        state.messages = state.messages.map((message, index) =>
          index === existing
            ? {
                ...message,
                content: event.tool_name || message.content,
                // A call announced a second time is the same call running for real: keep
                // whatever the prompt attached to it (its permission, its result) and let the
                // arguments and status catch up.
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
      // The approval lives on the tool call that triggered it — the card flips to
      // "input required" and shows the prompt inline, so the command (and later
      // its output) stay together. The event always carries the toolCallId.
      finishRunningThinking(state);
      const toolCallId = event.tool_call_id;
      const permission = {
        requestId: event.request_id,
        explanation: event.explanation || undefined,
        // The harness's own reason, as facts. Rendered into a sentence by whoever draws the
        // prompt, in the language they are drawing it in.
        reason: event.reason ?? undefined,
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
        // No card to attach to — and this is the *ordinary* case, not an edge one: approval is
        // decided in preflight, before the batch runs, so the tool call has not been announced
        // yet. The card raised here is therefore what a person actually reads, and it is built
        // like any announced call: `content` is the tool name and `meta.arguments` the model's
        // arguments. It used to put the command in `content` — the field every other tool call
        // uses for the name — so nothing downstream recognised the tool, and the model's own
        // `explanation` of why it wanted the call was nowhere to be found.
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
// session with its own transcript, never a branch inside this one.
function replayTurns(turns: A2ATurn[]): {
  messages: ChatMessage[];
  tasks: ChatTask[];
  tokenUsage: TokenUsage | null;
  keyCounts: Map<string, number>;
} {
  // Left in the order the server sent them, which is the order they *began* — it sorts each
  // turn by where its first message landed in the append-only history, and that append order is
  // the chronology. Re-sorting here by `status.timestamp` sorted by when each turn *ended*
  // instead, and the two disagree whenever turns overlap: a short turn that starts later can
  // finish first, and did — a session was replayed with its final answer at the top and every
  // tool call after it, because the turn holding them took nineteen seconds longer to close.
  const mainTurns = turns.filter((turn) => !(turnState(turn).referenceTurnIds ?? []).length);
  const state: ReduceState = newReduceState();
  for (const turn of mainTurns) {
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
  worktreeStrategy: WorktreeStrategy = "none",
  permissionMode: PermissionMode = "default",
  // Whether a turn is currently running on this session (from the server-tracked
  // running set). Drives the live subscribe stream when we are viewing — but not
  // driving — it.
  sessionRunning: boolean = false,
  // The workspace this session belongs to; rides in the turn metadata so the server
  // resolves the workspace's locations for the agent to address per tool call.
  workspaceId: string = ""
) {
  // Every message this hook can put in front of a person goes through here. They used to be
  // English string literals beside the `toaster.create` that raised them — including the one
  // that has been telling people their approval failed.
  const translation = useTranslations("ChatErrors");
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

  // Retire the chip for a steered message the session has now echoed back.
  //
  // By the id this client gave it when it sent it, falling back to the text for an echo that
  // carries none. The transcript reducer has keyed on that id for a while — it is what makes
  // the optimistic copy and the persisted one a single message — and matching by text here
  // meant the same event was identified two different ways: send "yes" twice and the first
  // echo retires whichever chip happens to read alike, not the one that was delivered.
  const acknowledgeSteering = useCallback((steered: { text: string; messageId: string }) => {
    if (!steered.text) return;
    const index = queuedMessagesRef.current.findIndex((message) => message.steering && (
      steered.messageId ? message.id === steered.messageId : message.text === steered.text
    ));
    if (index === -1) return;
    setQueue(queuedMessagesRef.current.filter((_, messageIndex) => messageIndex !== index));
  }, [setQueue]);

  // Whether the transcript is holding a decision nobody has made yet.
  //
  // This is the state that made a message disappear. A session parked on a permission prompt
  // has *stopped* — the daemon checkpoints the turn and sleeps it rather than hold an
  // interpreter open waiting for a person — so the client is not streaming, and every path
  // that asked "are we streaming?" concluded the session was idle and ready for a new turn. It
  // is not: `session.send` answers `{"accepted": false, "awaiting_input": true}` and delivers
  // nothing. Read from the live reducer state rather than from rendered props, because the
  // answer has to be current at the moment of sending, not as of the last commit.
  const hasPendingDecision = useCallback(() => stateRef.current.messages.some(
    (message) => message.role === "tool_call" && message.meta?.status === "input_required"
  ), []);

  // Hand the session the next message it is owed, if it can take one.
  //
  // One rule for the whole queue: it drains whenever the session is not parked. Three moments
  // satisfy that and they are all the same moment as far as this is concerned — a turn we drove
  // ended, a turn we were only watching ended, or a decision was made and the session went back
  // to work. The last is the one that matters for a message typed under a permission prompt:
  // the session is running again, so the message is injected into that turn rather than waiting
  // for it to finish, which is what a person means by typing while they watch it work.
  //
  // How it gets there is not decided here. `send` asks, and the session answers whether it
  // injected the message or started a turn with it — the client used to assume, and assuming
  // is what left a message flagged as delivered that had never been delivered at all.
  const drainQueueRef = useRef<() => void>(() => {});
  const drainQueue = useCallback(() => {
    if (isStreamingRef.current || hasPendingDecision()) return;
    const next = queuedMessagesRef.current.find((message) => !message.steering);
    if (!next) return;
    setQueue(queuedMessagesRef.current.filter((message) => message.id !== next.id));
    runStreamRef.current({ kind: "text", text: next.text, dataParts: next.dataParts });
  }, [hasPendingDecision, setQueue]);
  useEffect(() => {
    drainQueueRef.current = drainQueue;
  }, [drainQueue]);

  // A message the session would not take, and what it is waiting for instead. Deliberately not
  // an error: nothing is broken and nothing is lost — the message is in the queue, visible, and
  // goes out the moment the decision is made. What a person needs to know is that the thing to
  // do next is decide.
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

  // Transparent history fill. Once the newest page is on screen, pull every
  // remaining older page back-to-back — no artificial delay — accumulating them in
  // a ref WITHOUT re-rendering. A single prepend at the very end drops the whole
  // history in at once, above the (bottom-pinned) viewport, so it lands instantly
  // and invisibly instead of streaming page-by-page. Stable message keys (see
  // stableMessageId) mean that one prepend leaves every on-screen message untouched
  // — no remount, no flash, no layout shift.
  const drainOlderHistory = useCallback(async () => {
    const context = sessionIdRef.current;
    if (!context || isOlderHistoryLoadingRef.current) return;
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
        const page = await fetchSessionTurnsPage(context, cursor, undefined, HISTORY_PAGE_LIMIT);
        if (sessionIdRef.current !== context) return;
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
      swallowed({ component: "transcript", operation: "load older history" }, caught);
    } finally {
      // Skip the apply if a local turn began mid-drain (guarded above too) — the
      // fetched fragments stay in the ref, unused, rather than clobbering live state.
      if (fetchedAny && sessionIdRef.current === context && !streamedLocallyRef.current && !isStreamingRef.current) {
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
      // Already showing exactly this. Keeping the current messages keeps their identity *and*
      // their array reference, so `setMessages` bails out and not one row re-renders — where
      // replacing them would have rebuilt every row under a new key.
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
                swallowed({ component: "transcript", operation: "read the finished turn" }, caught);
              }
              // The other end of the wait. A message typed while the session was parked is
              // held rather than sent, and this is the moment it can go: the decision was
              // made, the turn it unblocked has ended, and the session will take a message
              // again. Without this the queue had no owner — the local drain belongs to a
              // turn *we* drove, and this session finished its own without us.
              if (!cancelled) drainQueueRef.current();
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
          swallowed({ component: "transcript", operation: "read the session's final state" }, caught);
        }
      })();
      drainQueueRef.current();
    }

    return () => {
      cancelled = true;
      controller.abort();
      subscription?.abort();
    };
  }, [sessionRunning, initialSessionId, isStreaming, flushNow, flush]);

  // The session went back to work — which, for a session that was parked, means the decision
  // has been made. Anything held for it goes now, into the turn that just resumed rather than
  // after it: a message typed while watching a turn belongs to that turn. `drainQueue` declines
  // if this client is the one driving, or if another decision is already outstanding.
  const sessionWasRunningRef = useRef(sessionRunning);
  useEffect(() => {
    const resumed = sessionRunning && !sessionWasRunningRef.current;
    sessionWasRunningRef.current = sessionRunning;
    if (resumed) drainQueueRef.current();
  }, [sessionRunning]);

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
      // Optimistic input message.
      const userMessageId = clientIdentifier();
      const dataParts = input.dataParts ?? [];
      const attachments = dataParts.flatMap((dataPart) => attachmentsFromData(dataPart));
      const meta = attachments.length > 0 ? { attachments } : {};
      // Keyed by the id this send will carry, so the echo lands on this row rather than
      // beside it. The optimistic copy is a *preview* of a message the server owns, not a
      // second message, and giving it the server's identity is what makes it one.
      const optimisticMessageId = stableMessageId(stateRef.current, "user", userMessageId);
      upsertMessage(stateRef.current, {
        id: optimisticMessageId,
        role: "user",
        content: input.text,
        timestamp: new Date().toISOString(),
        ...(Object.keys(meta).length > 0 ? { meta } : {}),
      });
      // No thinking row is opened here. The session says when it is thinking, and until it
      // does, the honest answer is that it has not started. A row invented from the keystroke
      // made the interface claim reasoning that no model had begun — and on a turn that
      // opened with a tool call rather than reasoning, it claimed something that never
      // happened at all. Stop and the composer already say a turn is under way.
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
        // A clean turn already settled its cards from the events it emitted; but a Stop, or
        // a connection that drops mid-turn (daemon stall, network loss), ends it with no
        // terminal event, which would otherwise leave every in-flight tool/thinking card
        // spinning forever. Sweep here as the single catch-all so a card can never outlive
        // its turn.
        finishRunningThinking(stateRef.current);
        finishActiveTools(stateRef.current);
        // A message still flagged `steering` was delivered, and the flag now means that
        // rather than assuming it: `send` clears it on anything the session did not take, so
        // what is left here is what the session said it injected. Retire those chips — the
        // message is in the turn, and sending it again would be sending it twice.
        const pendingText = queuedMessagesRef.current.filter((message) => !message.steering);
        if (pendingText.length !== queuedMessagesRef.current.length) setQueue(pendingText);
        flush();
        // A user Stop ends the turn; do not immediately relaunch a queued
        // message as a new turn — that is exactly the "Stop didn't stop" symptom.
        // Consume the one-shot flag and fall through to idle, leaving the queue for
        // the user to send deliberately.
        const abortedByUser = abortedByUserRef.current;
        abortedByUserRef.current = false;
        // Nor while a decision is outstanding, which is the other way a turn stops. Parking on
        // a permission prompt ends the turn exactly like finishing one does — same frame, same
        // handler — so this drain fired *into* the park: the queued message was handed to a
        // session that answers `accepted: false` to everything until a person decides. It ran
        // as a turn that never started, and the message was gone. The queue waits; the drain
        // below picks it up when the decision has been made and the resumed turn ends.
        if (!abortedByUser && !hasPendingDecision() && pendingText.length > 0) {
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
            const steered = steeringFromPart(frame.part);
            acknowledgeSteering(steered);
            notifyTurnError(frame.part);
            reduceAgentPart(stateRef.current, frame.part);
            // A steered message moves from the composer's queue into the transcript, and those
            // are two pieces of state. The ordinary flush defers to the next animation frame,
            // so the chip vanished in one commit and the message appeared in the next — one
            // frame in which the message was in both places, or in neither. Flushing this one
            // synchronously puts both updates in the same commit, which is what makes the
            // handoff a move rather than a flicker.
            if (steered.text) flushNow(); else flush();
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
              worktreeStrategy,
              permissionMode,
              workspaceId,
            });
            sessionIdentifier = created.id;
            sessionIdRef.current = created.id;
            setSessionId(created.id);
          }
          // Attach before sending: the worker starts emitting the moment it accepts the
          // message, and a subscription opened afterwards would miss the opening frames.
          observe(sessionIdentifier);
          const outcome = await sessionSend(sessionIdentifier, messageParts(text, dataParts), { messageId: userMessageId });
          // The session refused it. Not a failure — it is parked on a decision and taking a
          // message would mean discarding the parked turn — but nothing was delivered and no
          // turn is coming, so the preview on screen is a message the server has never heard
          // of. Take it back off the transcript and put it in the queue, which is the one place
          // that says "not sent yet" honestly and can still send it.
          if (!outcome.accepted) {
            stateRef.current.messages = stateRef.current.messages.filter(
              (message) => message.id !== optimisticMessageId
            );
            setQueue([...queuedMessagesRef.current, {
              id: clientIdentifier(), text, steering: false, dataParts,
            }]);
            notifyHeldForDecision(outcome.waitingOn);
            flush();
            finishTurn();
          }
        } catch (caught) {
          // The reason, not just the fact. This was a bare `catch {}`, so whatever actually
          // went wrong — which call, which status, which network failure — was discarded and
          // replaced with advice to read a daemon log that, for a failure on this side of the
          // wire, has nothing in it. That sent an investigation looking in the wrong process.
          const detail = errorMessage(caught);
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
    [agent, workingDirectory, worktreeStrategy, permissionMode, workspaceId, flush, flushNow, setQueue, acknowledgeSteering, notifyTurnError, hasPendingDecision, notifyHeldForDecision]
  );

  useEffect(() => {
    runStreamRef.current = runStream;
  }, [runStream]);

  const send = useCallback(
    (text: string, dataParts: Record<string, unknown>[] = []) => {
      const trimmed = text.trim();
      if (!trimmed) return Promise.resolve();
      // Three states, and the session is in exactly one of them.
      //
      // Parked on a decision: it takes nothing, so the message is held. This is the case that
      // was missing. The composer used to pass a `queueOnly` flag for it, but the check that
      // read the flag lived inside `if (isStreaming)` — and a parked session is not streaming,
      // because parking *ends* the turn. So the flag was computed, passed, and never reached:
      // the message went out to a session that answered `accepted: false`, was drawn on screen
      // as though it had been sent, and vanished at the next rebuild. The state is read here
      // now rather than passed in, so no caller can fail to pass it.
      if (hasPendingDecision()) {
        setQueue([...queuedMessagesRef.current, { id: clientIdentifier(), text: trimmed, steering: false, dataParts }]);
        return Promise.resolve();
      }
      // Running: sent now, and the session injects it at the next safe point. The chip says
      // "steering" until the session echoes it back.
      if (isStreamingRef.current) {
        const pending = { id: clientIdentifier(), text: trimmed, steering: false, dataParts };
        const context = sessionIdRef.current;
        if (context && dataParts.length === 0) {
          setQueue([...queuedMessagesRef.current, { ...pending, steering: true }]);
          // Whether it was injected is the session's answer, not this client's assumption. The
          // chip is a claim about what happened to the message, and the turn-end sweep retires
          // every chip still marked steering on the grounds that it was delivered — so a wrong
          // assumption here is a message deleted rather than a chip mislabelled.
          const keepQueued = () => setQueue(queuedMessagesRef.current.map((message) =>
            message.id === pending.id ? { ...message, steering: false } : message
          ));
          return sessionSend(context, messageParts(trimmed), { messageId: pending.id })
            .then((outcome) => {
              // Refused — the turn parked between the keystroke and the send. Nothing was
              // delivered, so it stays in the queue and goes out when the decision is made.
              if (!outcome.accepted) {
                keepQueued();
                notifyHeldForDecision(outcome.waitingOn);
                return;
              }
              // Taken, but as a fresh turn rather than an injection: the turn ended underneath
              // us. It is delivered and arrives over attach, so the chip has nothing left to
              // say and no echo is coming to retire it.
              if (!outcome.injected) {
                setQueue(queuedMessagesRef.current.filter((message) => message.id !== pending.id));
              }
            })
            // The send never reached the daemon, so nothing was injected.
            .catch(keepQueued);
        }
        setQueue([...queuedMessagesRef.current, pending]);
        return Promise.resolve();
      }
      // Idle: this message starts the turn.
      runStream({ kind: "text", text: trimmed, dataParts });
      return Promise.resolve();
    },
    [runStream, setQueue, hasPendingDecision, notifyHeldForDecision]
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

  // Allow-always is gone: a decision here is per call. Widening what the session may do
  // without asking is the permission mode's job, changed deliberately from the composer
  // rather than as a side effect of answering one prompt.
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
    // Suppress the queue auto-drain that the imminent stream close would otherwise
    // trigger, so Stop halts everything instead of relaunching a queued follow-up.
    abortedByUserRef.current = true;
    // Retire the steering chips now, not when the stream finally closes.
    //
    // A steering message has two representations while it is in flight: the chip, which says
    // "this will go in at the next opening", and the message itself once the session injects it
    // and echoes it back into the transcript. The chip is meant to disappear the moment that
    // echo arrives. Stop breaks that: the turn is torn down, the echo may never come, and the
    // sweep that retires the chip only runs when the stream closes — so the message sat on
    // screen *twice*, once as a chip and once as itself, for as long as the teardown took, and
    // then one of them vanished with no explanation. Nothing more can be injected after a Stop,
    // which is precisely what makes the chip's claim false the instant it is pressed.
    const notSteering = queuedMessagesRef.current.filter((message) => !message.steering);
    if (notSteering.length !== queuedMessagesRef.current.length) setQueue(notSteering);
    if (!context) {
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
        void resolvePermission(context, permission.requestId, "deny");
      } else if (question?.requestId) {
        settledAny = true;
        // Stop while a question is open settles it as a decline (not empty
        // answers): the model is told the user declined and the turn ends, which
        // also cleanly resolves the awaiting tool even if the context teardown races.
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
    // Tell the user if the stop request never reached the server — the turn may still
    // be running, and silently doing nothing would leave them stuck expecting it to end.
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
  }, [flush, setQueue]);

  // Kick off a manual context compaction. The compacting indicator and separator
  // arrive over the stream (live for the driver, via the subscribe stream for a
  // viewer), so there is nothing to render optimistically here.
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
