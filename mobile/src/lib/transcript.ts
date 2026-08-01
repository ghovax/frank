/**
 * A transcript, folded out of the parts a session emits.
 *
 * The daemon does not send messages; it sends *parts*, one per thing that happened, and the same
 * reducer has to make sense of them whether they arrive one at a time down the live stream or in
 * a block when a conversation is opened from history. That is the reason this is a plain
 * function over a mutable state object rather than anything React knows about: replay and live
 * are the same code, and the difference between them is only how fast the parts arrive.
 *
 * The event union is generated from the harness's own Pydantic models
 * (`web/src/lib/generated/events.schema.json`), and the switch below is exhaustive against it —
 * `assertNever` at the bottom means adding an event kind on the Python side fails this file's
 * type-check rather than being silently dropped on a phone.
 */

import { partPayload, turnState, type A2AMessage, type A2APart, type A2ATurn } from "./api";
import type { ToolStatus, WireEvent } from "@shared/generated/events";

export interface ToolCallMessage {
  key: string;
  role: "tool";
  toolCallId: string;
  name: string;
  arguments: Record<string, unknown>;
  status: ToolStatus;
  display: unknown;
  durationMs: number | null;
}

export type ChatMessage =
  | { key: string; role: "user"; text: string }
  | { key: string; role: "peer"; sender: string; text: string }
  | { key: string; role: "assistant"; text: string }
  | { key: string; role: "thinking"; text: string; running: boolean }
  | ToolCallMessage
  | { key: string; role: "error"; title: string; message: string; code: string }
  | { key: string; role: "warning"; title: string; message: string }
  | { key: string; role: "compaction"; status: "started" | "done" }
  | { key: string; role: "steering"; text: string };

export interface PendingPermission {
  requestId: string;
  toolName: string;
  command: string;
  explanation: string;
  risk: string;
  arguments: Record<string, unknown>;
}

export interface PendingQuestion {
  requestId: string;
  questions: Record<string, unknown>[];
}

export interface TokenUsage {
  inputTokens: number;
  outputTokens: number;
  contextWindow: number;
  cumulativeTotal: number;
}

export interface ReduceState {
  messages: ChatMessage[];
  /** Where the open assistant paragraph sits, which successive `text` events extend. */
  lane: number | null;
  thinking: number | null;
  /** Where each live tool call sits, so its result can find it without a scan. */
  tools: Map<string, number>;
  usage: TokenUsage | null;
  permission: PendingPermission | null;
  question: PendingQuestion | null;
  counter: number;
}

export function newReduceState(): ReduceState {
  return {
    messages: [], lane: null, thinking: null, tools: new Map(),
    usage: null, permission: null, question: null, counter: 0,
  };
}

function nextKey(state: ReduceState, prefix: string): string {
  state.counter += 1;
  return `${prefix}-${state.counter}`;
}

/** Anything that is not more prose ends the open paragraph. */
function closeLane(state: ReduceState): void {
  state.lane = null;
}

/**
 * Change a message by replacing it, never by writing through to it.
 *
 * The array is mutable on purpose — a turn emits one part per token, and rebuilding it each time
 * would be the cost this design exists to avoid. The *messages* are not, and that distinction is
 * load-bearing: a row is memoised on the identity of its message, so a field written in place is
 * a change no component can see. It is a silent one, too — the state is correct, the render is
 * stale, and nothing anywhere reports a problem. A spinner that never stops is what it looks
 * like.
 */
function replace(state: ReduceState, index: number, message: ChatMessage): void {
  state.messages = [...state.messages.slice(0, index), message, ...state.messages.slice(index + 1)];
}

function append(state: ReduceState, message: ChatMessage): number {
  state.messages = [...state.messages, message];
  return state.messages.length - 1;
}

/** One part from the live tail, or one replayed from history. */
export function reducePart(state: ReduceState, part: A2APart): void {
  if (part.kind === "text") {
    if (part.text) pushAssistantText(state, part.text);
    return;
  }
  if (part.kind !== "data") return;
  const payload = partPayload(part.data) as WireEvent | Record<string, never>;
  if (!("kind" in payload)) return;
  reduceEvent(state, payload as WireEvent);
}

function pushAssistantText(state: ReduceState, text: string): void {
  const open = state.lane === null ? undefined : state.messages[state.lane];
  if (open?.role === "assistant") {
    replace(state, state.lane as number, { ...open, text: open.text + text });
    return;
  }
  state.lane = append(state, { key: nextKey(state, "assistant"), role: "assistant", text });
}

function reduceEvent(state: ReduceState, event: WireEvent): void {
  switch (event.kind) {
    case "text": {
      if (event.text) pushAssistantText(state, event.text);
      return;
    }
    case "thinking": {
      const open = state.thinking === null ? undefined : state.messages[state.thinking];
      // A thinking event with no text is a block boundary, not a thought. Treating it as one
      // closes the open paragraph and starts a new one, which is why a single sentence of prose
      // arrived on screen as two — split at whatever word the model happened to be at when the
      // reasoning block changed.
      if (!event.text && open === undefined) return;
      closeLane(state);
      if (open?.role === "thinking" && open.running) {
        replace(state, state.thinking as number, { ...open, text: open.text + (event.text ?? "") });
        return;
      }
      state.thinking = append(state, {
        key: nextKey(state, "thinking"), role: "thinking", text: event.text ?? "", running: true,
      });
      return;
    }
    case "thinking_done": {
      const open = state.thinking === null ? undefined : state.messages[state.thinking];
      if (open?.role === "thinking") replace(state, state.thinking as number, { ...open, running: false });
      state.thinking = null;
      return;
    }
    case "tool_call": {
      closeLane(state);
      state.thinking = null;
      if (state.tools.has(event.tool_call_id)) return;
      const index = append(state, {
        key: nextKey(state, "tool"),
        role: "tool",
        toolCallId: event.tool_call_id,
        name: event.tool_name,
        arguments: event.arguments ?? {},
        status: "running",
        display: undefined,
        durationMs: null,
      });
      state.tools.set(event.tool_call_id, index);
      return;
    }
    case "tool_result": {
      const index = state.tools.get(event.tool_call_id);
      const target = index === undefined ? undefined : state.messages[index];
      if (target?.role !== "tool") return;
      replace(state, index as number, {
        ...target,
        status: event.status,
        display: event.display,
        durationMs: event.metadata?.duration_ms ?? null,
      });
      return;
    }
    case "mcp_event":
      // Progress from inside an MCP server. The desktop shows it on the expanded tool row; a
      // phone has no room for it and the row already says the call is running.
      return;
    case "status":
      return;
    case "token_usage": {
      state.usage = {
        inputTokens: event.input_tokens ?? 0,
        outputTokens: event.output_tokens ?? 0,
        contextWindow: event.context_window ?? 0,
        cumulativeTotal: event.cumulative?.total_tokens ?? 0,
      };
      return;
    }
    case "permission_request": {
      closeLane(state);
      state.permission = {
        requestId: event.request_id,
        toolName: event.tool_name ?? "",
        command: event.command ?? "",
        explanation: event.explanation ?? "",
        risk: event.risk ?? "",
        arguments: event.arguments ?? {},
      };
      return;
    }
    case "question": {
      closeLane(state);
      state.question = { requestId: event.request_id, questions: event.questions ?? [] };
      return;
    }
    case "steering": {
      closeLane(state);
      append(state, { key: nextKey(state, "steering"), role: "steering", text: event.text ?? "" });
      return;
    }
    case "compaction": {
      closeLane(state);
      // One divider per compaction, not two: the `started` row becomes the `done` row rather
      // than being followed by it.
      const last = state.messages[state.messages.length - 1];
      if (last?.role === "compaction" && event.status === "done") {
        replace(state, state.messages.length - 1, { ...last, status: "done" });
        return;
      }
      append(state, { key: nextKey(state, "compaction"), role: "compaction", status: event.status });
      return;
    }
    case "error": {
      closeLane(state);
      append(state, {
        key: nextKey(state, "error"),
        role: "error",
        title: event.title || "Something went wrong",
        message: event.message ?? "",
        code: event.code ?? "",
      });
      return;
    }
    case "warning": {
      closeLane(state);
      append(state, {
        key: nextKey(state, "warning"),
        role: "warning",
        title: event.title || "Warning",
        message: event.message ?? "",
      });
      return;
    }
    case "done": {
      finishRunningTools(state);
      return;
    }
    default:
      assertNever(event);
  }
}

/**
 * A turn that ends leaves nothing spinning.
 *
 * Tools whose result never arrived — because the turn was cancelled, or the worker died — are
 * marked rather than left animating forever.
 *
 * The pending gates are deliberately *not* cleared here, and the distinction cost a turn to
 * find. A turn that suspends on an approval ends: the worker stops, the daemon says the turn is
 * no longer running, and this is called. But the session is not finished, it is *waiting* — the
 * approval is exactly what it is waiting for, and clearing it takes the Allow button off the
 * screen of somebody whose session is parked until they press it. See `discardPendingGates` for
 * the case where a gate really is dead.
 */
export function finishRunningTools(state: ReduceState): void {
  state.messages = state.messages.map((message) => {
    if (message.role === "tool" && message.status === "running") return { ...message, status: "error" as const };
    if (message.role === "thinking" && message.running) return { ...message, running: false };
    return message;
  });
  state.tools.clear();
  state.lane = null;
  state.thinking = null;
}

/**
 * Forget a gate that can no longer be answered.
 *
 * Only for a turn that reached a terminal state. Such a turn still carries its last request in
 * its history, so replaying it would otherwise put a Deny/Allow card in front of somebody for a
 * decision that was settled days ago and has nowhere left to go.
 */
export function discardPendingGates(state: ReduceState): void {
  state.permission = null;
  state.question = null;
}

function reduceInboundMessage(state: ReduceState, message: A2AMessage, peerSender: string): void {
  const text = message.parts
    .filter((part) => part.kind === "text" && part.text)
    .map((part) => part.text)
    .join("")
    .trim();
  if (!text) return;
  closeLane(state);
  if (peerSender) {
    append(state, { key: nextKey(state, "peer"), role: "peer", sender: peerSender, text });
    return;
  }
  append(state, { key: nextKey(state, "user"), role: "user", text });
}

/**
 * Rebuild a transcript from a session's persisted turns.
 *
 * Turns that reference another turn are a peer's work, not this session's, and are dropped: a
 * peer is its own session with its own transcript, never a lane inside this one.
 */
export function replayTurns(turns: A2ATurn[]): ReduceState {
  const state = newReduceState();
  const ordered = turns
    .filter((turn) => !(turnState(turn).referenceTurnIds ?? []).length)
    .sort((first, second) =>
      String(first.status?.timestamp ?? "").localeCompare(String(second.status?.timestamp ?? "")));

  for (const turn of ordered) {
    state.lane = null;
    // A turn's message stream is its history *plus* its trailing status message: A2A keeps the
    // latest message on `status.message` and only folds it into `history` on the next status
    // update. A turn suspended on a permission never gets that update, so the request it is
    // parked on lives only there — and dropping it would leave the card stuck with nothing to
    // answer.
    const messages = [...(turn.history ?? [])];
    const trailing = turn.status?.message;
    if (trailing && !messages.some((entry) => !!entry.messageId && entry.messageId === trailing.messageId)) {
      messages.push(trailing);
    }
    const peerSender = turnState(turn).peerSender ?? "";
    for (const message of messages) {
      if (message.role === "user") reduceInboundMessage(state, message, peerSender);
      else for (const part of message.parts ?? []) reducePart(state, part);
    }
    // The turn's answer, when it did not also arrive as streamed parts.
    if (!endsWithAssistantProse(state)) {
      for (const artifact of turn.artifacts ?? []) {
        for (const part of artifact.parts ?? []) {
          if (part.kind === "text" && part.text?.trim()) pushAssistantText(state, part.text);
        }
      }
    }
    if (TERMINAL_STATES.has(String(turn.status?.state ?? ""))) {
      finishRunningTools(state);
      discardPendingGates(state);
    }
  }
  return state;
}

const TERMINAL_STATES = new Set(["completed", "failed", "canceled", "rejected"]);

function endsWithAssistantProse(state: ReduceState): boolean {
  for (let index = state.messages.length - 1; index >= 0; index -= 1) {
    const message = state.messages[index];
    if (message.role === "user" || message.role === "peer") return false;
    if (message.role === "assistant" && message.text.trim()) return true;
  }
  return false;
}

function assertNever(value: never): never {
  throw new Error(`Unhandled event: ${JSON.stringify(value)}`);
}

/**
 * The timeline as it is drawn: a contiguous run of tool calls becomes one collapsible row.
 *
 * The desktop does this because a burst of twelve reads is one act, not twelve; a phone needs it
 * more, since twelve rows is the whole screen. Thinking interleaved inside a run is folded in
 * with them, which is what makes "read three files and think about it" a single line.
 */
export type TimelineItem =
  | { kind: "message"; message: ChatMessage }
  | { kind: "tools"; key: string; calls: ToolCallMessage[]; thinking: ChatMessage[] };

export function timelineItems(messages: ChatMessage[]): TimelineItem[] {
  const items: TimelineItem[] = [];
  let index = 0;
  while (index < messages.length) {
    const message = messages[index];
    if (message.role !== "tool") {
      items.push({ kind: "message", message });
      index += 1;
      continue;
    }
    const calls: ToolCallMessage[] = [];
    const thinking: ChatMessage[] = [];
    while (index < messages.length) {
      const candidate = messages[index];
      if (candidate.role === "tool") calls.push(candidate);
      else if (candidate.role === "thinking") thinking.push(candidate);
      else break;
      index += 1;
    }
    items.push({ kind: "tools", key: calls[0]?.key ?? `tools-${index}`, calls, thinking });
  }
  return items;
}
