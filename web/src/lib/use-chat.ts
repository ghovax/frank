"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  streamA2A,
  abortSession,
  abortToolCall,
  steerSession,
  resolvePermission,
  resolveQuestion,
  fetchSessionTasks,
  fetchSessionTasksPage,
  subscribeSessionStream,
  type A2AStreamResult,
  type A2AMessage,
  type A2APart,
  type A2ATask as A2ATaskWire,
  type PermissionMode,
  type WorkspaceStrategy,
} from "./api";
import { isControlToolName, isSameToolEvent, type PermissionDecision, type QuestionAnswer, type QuestionItem, type ToolEvent, type ToolEventStatus } from "./tool-event";
import type { WidgetEvent } from "@/components/widget-bridge";
import { toaster } from "@/components/ui/toaster";

// Re-export the A2A task shape so components can consume it from one place.
export type A2ATask = A2ATaskWire;

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

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "tool_call" | "thinking" | "error";
  content: string;
  timestamp: string;
  meta?: Record<string, unknown>;
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

// A turn's input: either typed text or a structured widget interaction. Both
// drive the same stream; a widget event travels as a typed DataPart, never as
// prose, so the agent receives it as structured JSON.
export type ChatInput =
  | { kind: "text"; text: string }
  | { kind: "widget"; event: WidgetEvent };

export interface QueuedMessage {
  id: string;
  text: string;
  steering: boolean;
}

// An agent step's ordered timeline — prose, reasoning, and tool calls
// interleaved, mirroring the main chat. Built from the A2A sub-task DataPart
// envelopes. Reasoning is captured here for fidelity but, like the main chat, is
// not rendered as transcript rows (the agents panel surfaces it as a live status).
export type AgentPart =
  | { kind: "text"; content: string }
  | { kind: "thinking"; content: string; status: ToolEventStatus; durationMs?: number }
  | ({ kind: "tool" } & ToolEvent);

type ThinkingPart = Extract<AgentPart, { kind: "thinking" }>;
type ToolPart = Extract<AgentPart, { kind: "tool" }>;

export interface AgentStep {
  stepId: string;
  agent: string;
  goal: string;
  childTaskId: string;
  parts: AgentPart[];
  state: TaskState;
  task?: A2ATask;
}

export interface AgentGroup {
  groupId: string;
  toolCallId: string;
  steps: AgentStep[];
}

export function isStepDone(step: AgentStep): boolean {
  return TERMINAL_STATES.has(step.state);
}

// Concatenate the text parts of an A2A task's artifacts.
export function taskArtifactText(task: A2ATask | undefined): string {
  if (!task?.artifacts) return "";
  const texts: string[] = [];
  for (const artifact of task.artifacts) {
    for (const part of artifact.parts ?? []) {
      if (part.kind === "text" && part.text) texts.push(part.text);
    }
  }
  return texts.join("\n");
}

function emptyStep(stepId: string, agent = "", goal = "", childTaskId = ""): AgentStep {
  return { stepId, agent, goal, childTaskId, parts: [], state: "working" };
}

// One place that knows a thinking part's shape, so call sites spell out only what
// differs (reasoning text, a finished status). Defaults to a running placeholder
// with no body.
function thinkingPart(fields: Partial<ThinkingPart> = {}): ThinkingPart {
  return { kind: "thinking", content: "", status: "running", ...fields };
}

function isThinkingPart(part: AgentPart): part is ThinkingPart {
  return part.kind === "thinking";
}

function isRunningThinking(part: AgentPart): part is ThinkingPart {
  return isThinkingPart(part) && part.status === "running";
}

function isToolPart(part: AgentPart): part is ToolPart {
  return part.kind === "tool";
}

// Close out any in-flight thinking part (mark it done). Called before a new
// prose block, tool call, or the step finishing. Parts persist — a finished
// "Thinking" stays as the marker for that reasoning phase.
function finishAgentThinking(step: AgentStep): AgentStep {
  if (!step.parts.some(isRunningThinking)) return step;
  return { ...step, parts: step.parts.map((part) => (isRunningThinking(part) ? { ...part, status: "done" } : part)) };
}

// Close the in-flight thinking part and stamp how long it ran, so the timeline
// shows "Thought for Ns". The server measures the duration; here it's just recorded.
function finishAgentThinkingWithDuration(step: AgentStep, durationMs: number): AgentStep {
  if (!step.parts.some(isRunningThinking)) return step;
  return { ...step, parts: step.parts.map((part) => (isRunningThinking(part) ? { ...part, status: "done", durationMs } : part)) };
}

function finishRunningAgentTools(step: AgentStep): AgentStep {
  if (!step.parts.some((part) => isToolPart(part) && part.status === "running")) return step;
  return {
    ...step,
    parts: step.parts.map((part) =>
      isToolPart(part) && part.status === "running"
        ? { ...part, status: "completed" as const }
        : part
    ),
  };
}

// The single path for the thinking signal — the iteration-start ping and any
// streamed reasoning. Ensures a running thinking part exists, then appends the
// reasoning text. A bare ping just keeps the part alive (no body, no second
// placeholder stacked).
function applyAgentThinking(step: AgentStep, text: string): AgentStep {
  let index = step.parts.findLastIndex(isRunningThinking);
  let parts = step.parts;
  if (index === -1) {
    parts = [...parts, thinkingPart()];
    index = parts.length - 1;
  }
  if (!text) return { ...step, parts };
  return {
    ...step,
    parts: parts.map((part, partIndex) =>
      partIndex === index && isThinkingPart(part) ? { ...part, content: part.content + text } : part
    ),
  };
}

function appendAgentText(step: AgentStep, text: string): AgentStep {
  if (!text) return step;
  step = finishAgentThinking(step);
  const last = step.parts[step.parts.length - 1];
  if (last && last.kind === "text") {
    const parts = step.parts.slice(0, -1);
    parts.push({ kind: "text", content: last.content + text });
    return { ...step, parts };
  }
  return { ...step, parts: [...step.parts, { kind: "text", content: text }] };
}

function appendAgentToolCall(step: AgentStep, name: string, toolArguments: Record<string, unknown> | undefined, toolCallId: string): AgentStep {
  if (toolCallId && step.parts.some((part) => isToolPart(part) && part.toolCallId === toolCallId)) {
    return step;
  }
  step = finishAgentThinking(step);
  return {
    ...step,
    parts: [...step.parts, { kind: "tool", name, arguments: toolArguments, toolCallId, status: "running" }],
  };
}

// The lifecycle status implied by a tool result. A "*_started" / scheduled
// result (bash and web search run in the background) keeps the card "running" —
// the matching "*_completed" result arrives later and finalizes it. Approving a
// command only lets it *run*, so it must not jump straight to "completed".
function toolResultStatus(result: unknown): ToolEventStatus {
  const code = String(asRecord(result).code ?? "");
  if (code === "tool_error") return "failed";
  if (code.endsWith("_started") || code === "background_task_scheduled") return "running";
  return "completed";
}

function upsertAgentToolResult(step: AgentStep, name: string, toolCallId: string, result: unknown): AgentStep {
  const status = toolResultStatus(result);
  let matched = false;
  const parts = step.parts.map((part) => {
    if (!isToolPart(part) || !isSameToolEvent(part, name, toolCallId)) return part;
    matched = true;
    return { ...part, result, status };
  });
  if (matched) return { ...step, parts };
  // No matching call (result arrived first / orphaned) — record it as a finished tool.
  return {
    ...step,
    parts: [...step.parts, { kind: "tool", name: name || "unknown", toolCallId, result, status }],
  };
}

// The result currently stored for a tool part, located by its call id across all
// agent steps — so a streamed/MCP update merges into what is already there.
function agentToolResult(agentGroups: AgentGroup[], toolCallId: string): unknown {
  for (const group of agentGroups) {
    for (const step of group.steps) {
      const part = step.parts.find((candidate) => isToolPart(candidate) && candidate.toolCallId === toolCallId);
      if (part && isToolPart(part)) return part.result;
    }
  }
  return undefined;
}

function artifactPartsText(parts: A2APart[] | undefined): string {
  return (parts ?? [])
    .filter((part) => part.kind === "text" && part.text)
    .map((part) => part.text ?? "")
    .join("");
}

function streamArtifactText(result: Extract<A2AStreamResult, { kind: "artifact-update" }>): string {
  return artifactPartsText(result.artifact?.parts);
}

function finishAgentStep(step: AgentStep, task: A2ATask | undefined): AgentStep {
  const state = (task?.status?.state as TaskState) ?? "completed";
  const artifactText = taskArtifactText(task);
  const hasText = step.parts.some((part) => part.kind === "text");
  const next = finishRunningAgentTools(finishAgentThinking(!hasText && artifactText ? appendAgentText(step, artifactText) : step));
  return { ...next, state, task };
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
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
        return { title: "Model temporarily unavailable", message: "The selected model provider is temporarily unavailable. Try again in a moment or switch models." };
      case "rate_limited":
        return { title: "Model rate limit reached", message: "The selected provider is rate limiting requests. Wait a bit or switch to another model." };
      case "authentication_failed":
        return { title: "Provider credentials need attention", message: "The selected provider rejected the configured credentials. Check the API key or choose another model." };
      case "connection_failed":
      case "network_error":
        return { title: "Connection interrupted", message: "The model connection dropped before the turn finished. Check the connection and retry." };
      case "server_error":
        return { title: "Server request failed", message: "Daisy could not start the turn. Check the server log and try again." };
      case "request_too_large":
        return { title: "Request is too large", message: "The model could not accept this much context. Start a smaller follow-up or switch to a model with more capacity." };
      case "request_rejected":
        return { title: "Model rejected the request", message: "The model could not accept this turn. Adjust the request or switch models." };
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

function friendlyErrorFromMessage(message: A2AMessage | undefined): FriendlyError | null {
  for (const part of message?.parts ?? []) {
    if (part.kind === "data" && part.data?.kind === "error") {
      return friendlyErrorFromData(part.data);
    }
  }
  return null;
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
  const artifacts = Array.isArray(event.artifacts) ? event.artifacts : [];
  return {
    server: data.server ?? event.server,
    tool: data.tool ?? event.tool,
    event: event.event,
    payload: event.payload,
    progress: event.progress,
    total: event.total,
    message: event.message,
    artifacts,
  };
}

function mergeMcpResult(existing: unknown, streamed: Record<string, unknown>): Record<string, unknown> {
  const current = asRecord(existing);
  const events = Array.isArray(current.events) ? current.events : [];
  const currentArtifacts = Array.isArray(current.artifacts) ? current.artifacts : [];
  const streamedArtifacts = Array.isArray(streamed.artifacts) ? streamed.artifacts : [];
  return {
    ...current,
    ...streamed,
    events: [...events, streamed],
    artifacts: [...currentArtifacts, ...streamedArtifacts],
  };
}

function mergeMcpFinalResult(existing: unknown, finalResult: unknown): unknown {
  const current = asRecord(existing);
  const finalRecord = asRecord(finalResult);
  if (Object.keys(finalRecord).length === 0) return finalResult;
  const currentArtifacts = Array.isArray(current.artifacts) ? current.artifacts : [];
  if (currentArtifacts.length === 0 || Array.isArray(finalRecord.artifacts)) return finalResult;
  return {
    ...finalRecord,
    events: current.events,
    artifacts: currentArtifacts,
  };
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function artifactIdentifier(value: unknown): string {
  const artifact = asRecord(value);
  return String(artifact.artifact_id ?? artifact.artifactId ?? artifact.id ?? "").trim();
}

function artifactTargetIdentifier(value: unknown): string {
  const artifact = asRecord(value);
  return String(
    artifact.artifact_target_id
      ?? artifact.artifactTargetId
      ?? artifact.target_artifact_id
      ?? artifact.targetArtifactId
      ?? ""
  ).trim();
}

function artifactUpdateMode(value: unknown): string {
  const artifact = asRecord(value);
  return String(
    artifact.artifact_update_mode
      ?? artifact.artifactUpdateMode
      ?? artifact.update_mode
      ?? artifact.updateMode
      ?? "append"
  ).toLowerCase().trim();
}

function resultArtifacts(value: unknown): unknown[] {
  return asArray(asRecord(value).artifacts);
}

function withResultArtifacts(value: unknown, artifacts: unknown[]): unknown {
  const record = asRecord(value);
  if (Object.keys(record).length === 0) return value;
  return { ...record, artifacts };
}

function replaceArtifactInResult(result: unknown, targetId: string, nextArtifact: unknown): { result: unknown; replaced: boolean } {
  const artifacts = resultArtifacts(result);
  if (artifacts.length === 0) return { result, replaced: false };
  let replaced = false;
  const nextArtifacts = artifacts.map((artifact) => {
    if (artifactIdentifier(artifact) !== targetId) return artifact;
    replaced = true;
    return { ...asRecord(artifact), ...asRecord(nextArtifact), artifact_id: artifactIdentifier(nextArtifact) || targetId };
  });
  return { result: replaced ? withResultArtifacts(result, nextArtifacts) : result, replaced };
}

function applyArtifactUpdates(
  messages: ChatMessage[],
  result: unknown,
  currentToolCallId: string
): { messages: ChatMessage[]; result: unknown } {
  const artifacts = resultArtifacts(result);
  if (artifacts.length === 0) return { messages, result };

  let nextMessages = messages;
  const remainingArtifacts: unknown[] = [];

  for (const artifact of artifacts) {
    const mode = artifactUpdateMode(artifact);
    const updateTargetId = artifactTargetIdentifier(artifact) || artifactIdentifier(artifact);
    if (!updateTargetId || !["replace", "update", "upsert"].includes(mode)) {
      remainingArtifacts.push(artifact);
      continue;
    }

    let didReplace = false;
    nextMessages = nextMessages.map((message) => {
      if (didReplace || message.role !== "tool_call" || message.meta?.toolCallId === currentToolCallId) return message;
      const replacement = replaceArtifactInResult(message.meta?.result, updateTargetId, artifact);
      if (!replacement.replaced) return message;
      didReplace = true;
      return { ...message, meta: { ...message.meta, result: replacement.result } };
    });

    // No existing artifact matched this target. Whatever the mode, keep the
    // artifact so it still renders fresh — dropping it (the old behavior for
    // "replace"/"update") loses the widget entirely on its first render.
    if (!didReplace) {
      remainingArtifacts.push(artifact);
    }
  }

  return { messages: nextMessages, result: withResultArtifacts(result, remainingArtifacts) };
}

function applyArtifactUpdatesToAgentGroups(
  agentGroups: AgentGroup[],
  result: unknown,
  currentToolCallId: string
): { agentGroups: AgentGroup[]; result: unknown } {
  const artifacts = resultArtifacts(result);
  if (artifacts.length === 0) return { agentGroups, result };

  let nextAgentGroups = agentGroups;
  const remainingArtifacts: unknown[] = [];

  for (const artifact of artifacts) {
    const mode = artifactUpdateMode(artifact);
    const updateTargetId = artifactTargetIdentifier(artifact) || artifactIdentifier(artifact);
    if (!updateTargetId || !["replace", "update", "upsert"].includes(mode)) {
      remainingArtifacts.push(artifact);
      continue;
    }

    let didReplace = false;
    nextAgentGroups = nextAgentGroups.map((group) => ({
      ...group,
      steps: group.steps.map((step) => ({
        ...step,
        parts: step.parts.map((part) => {
          if (didReplace || part.kind !== "tool" || part.toolCallId === currentToolCallId) return part;
          const replacement = replaceArtifactInResult(part.result, updateTargetId, artifact);
          if (!replacement.replaced) return part;
          didReplace = true;
          return { ...part, result: replacement.result };
        }),
      })),
    }));

    if (!didReplace && mode === "upsert") {
      remainingArtifacts.push(artifact);
    }
  }

  return { agentGroups: nextAgentGroups, result: withResultArtifacts(result, remainingArtifacts) };
}

function withStep(
  list: AgentGroup[],
  groupId: string,
  stepId: string,
  updater: (step: AgentStep) => AgentStep
): AgentGroup[] {
  return list.map((group) => {
    if (group.groupId !== groupId) return group;
    const hasStep = group.steps.some((step) => step.stepId === stepId);
    const steps = hasStep
      ? group.steps.map((step) => (step.stepId === stepId ? updater(step) : step))
      : [...group.steps, updater(emptyStep(stepId))];
    return { ...group, steps };
  });
}

// A2A stream reduction — turn agent message parts into chat + group UI
// state. Shared by the live stream and the replay path so both render identically.

interface ReduceState {
  messages: ChatMessage[];
  agentGroups: AgentGroup[];
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
  return { messages: [], agentGroups: [], tasks: [], lane: null, tokenUsage: null, keyCounts: new Map() };
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

function startAgentGroup(state: ReduceState, data: Record<string, unknown>): void {
  const groupId = String(data.groupId ?? "");
  if (!groupId) return;
  const rawSteps = Array.isArray(data.steps) ? (data.steps as Record<string, unknown>[]) : [];
  const newSteps = rawSteps.map((step) =>
    emptyStep(String(step.id ?? ""), String(step.agent ?? ""), String(step.prompt ?? ""), String(step.childTaskId ?? ""))
  );
  const existing = state.agentGroups.find((group) => group.groupId === groupId);
  if (existing) {
    // A per-turn group of spawned agents grows as more agents are spawned —
    // merge in any steps not already present.
    const knownIds = new Set(existing.steps.map((step) => step.stepId));
    const added = newSteps.filter((step) => !knownIds.has(step.stepId));
    if (added.length > 0) {
      state.agentGroups = state.agentGroups.map((group) =>
        group.groupId === groupId ? { ...group, steps: [...group.steps, ...added] } : group
      );
    }
  } else {
    state.agentGroups = [
      ...state.agentGroups,
      {
        groupId,
        toolCallId: String(data.toolCallId ?? ""),
        steps: newSteps,
      },
    ];
  }
  // Link the spawning tool-call row to this group.
  state.messages = state.messages.map((message) =>
    message.role === "tool_call" && message.meta?.toolCallId === data.toolCallId
      ? { ...message, meta: { ...message.meta, groupId, status: "running" } }
      : message
  );
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

function finishRunningTools(state: ReduceState): void {
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

function pushAssistantText(state: ReduceState, text: string, sourceId?: string): void {
  if (!text) return;
  finishRunningThinking(state);
  if (state.lane === null) {
    const id = stableMessageId(state, "asst", sourceId);
    state.lane = id;
    state.messages = [...state.messages, { id, role: "assistant", content: text, timestamp: new Date().toISOString() }];
  } else {
    const laneId = state.lane;
    state.messages = state.messages.map((message) =>
      message.id === laneId ? { ...message, content: message.content + text } : message
    );
  }
}

function reduceUserMessage(state: ReduceState, message: A2AMessage): void {
  const text = (message.parts ?? []).filter((part) => part.kind === "text").map((part) => part.text ?? "").join("");
  state.lane = null;
  state.messages = [
    ...state.messages,
    { id: stableMessageId(state, "user", message.messageId), role: "user", content: text, timestamp: new Date().toISOString() },
  ];
}

function reduceAgentMessage(state: ReduceState, message: A2AMessage): void {
  for (const part of message.parts ?? []) {
    if (part.kind === "text") {
      pushAssistantText(state, part.text ?? "", message.messageId);
      continue;
    }
    if (part.kind !== "data" || !part.data) continue;
    reduceDataPart(state, part.data, message.messageId);
  }
}

function steeringText(message: A2AMessage | undefined): string {
  for (const part of message?.parts ?? []) {
    if (part.kind === "data" && part.data?.kind === "steering") {
      return String(part.data.text ?? "").trim();
    }
  }
  return "";
}

function reduceDataPart(state: ReduceState, data: Record<string, unknown>, sourceId?: string): void {
  const kind = String(data.kind ?? "");
  switch (kind) {
    case "steering": {
      const text = String(data.text ?? "").trim();
      if (!text) break;
      state.lane = null;
      state.messages = [
        ...state.messages,
        { id: stableMessageId(state, "user", sourceId), role: "user", content: text, timestamp: new Date().toISOString() },
      ];
      break;
    }
    case "tasks_updated": {
      state.tasks = mergeTasks(state.tasks, Array.isArray(data.tasks) ? data.tasks : []);
      break;
    }
    case "token_usage": {
      // The cumulative totals grow monotonically across the session, so the
      // latest part is authoritative on both the live stream and on replay.
      const cumulative = asRecord(data.cumulative);
      // Per-call (latest) figures — this is the actual current context, not a sum.
      const contextInputTokens = Number(data.inputTokens ?? 0);
      const contextOutputTokens = Number(data.outputTokens ?? 0);
      state.tokenUsage = {
        inputTokens: Number(cumulative.inputTokens ?? 0),
        outputTokens: Number(cumulative.outputTokens ?? 0),
        totalTokens: Number(cumulative.totalTokens ?? 0),
        cacheReadTokens: Number(cumulative.cacheReadTokens ?? 0),
        reasoningTokens: Number(cumulative.reasoningTokens ?? 0),
        modelCalls: Number(cumulative.modelCalls ?? 0),
        contextInputTokens,
        contextOutputTokens,
        contextTokens: contextInputTokens + contextOutputTokens,
        contextWindow: Number(data.contextWindow ?? 0),
      };
      break;
    }
    case "status": {
      // The model has finished thinking and is paused on tool execution. Tool
      // calls surface their own running/done status, so just close out any
      // in-flight thinking indicator. (The thinking ping itself is a `thinking`
      // event now, not a status — this only handles the wait edge.)
      if (String(data.code ?? "") === "waiting_for_tools") finishRunningThinking(state);
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
      applyThinking(state, String(data.text ?? ""));
      break;
    case "thinking_done":
      finishRunningThinkingWithDuration(state, Number(data.durationMs ?? 0));
      break;
    case "tool_call": {
      // Every tool call (even a control one like update_goal that renders nothing)
      // breaks the prose lane so surrounding text doesn't run together.
      state.lane = null;
      if (isControlToolName(data.name)) break;
      finishRunningThinking(state);
      const toolCallId = String(data.toolCallId ?? "");
      state.messages = [
        ...state.messages,
        {
          id: `toolcall-${toolCallId || crypto.randomUUID()}`,
          role: "tool_call",
          content: String(data.name ?? "unknown"),
          timestamp: new Date().toISOString(),
          meta: { arguments: data.arguments, toolCallId, status: "running" },
        },
      ];
      break;
    }
    case "tool_result": {
      if (isControlToolName(data.name)) break;
      finishRunningThinking(state);
      state.lane = null;
      const toolName = String(data.name ?? "");
      const toolCallId = String(data.toolCallId ?? "");
      const currentMessage = state.messages.find((message) => messageMatchesToolEvent(message, toolName, toolCallId));
      const mergedResult = data.name === "call_mcp_tool" ? mergeMcpFinalResult(currentMessage?.meta?.result, data.result) : data.result;
      const artifactUpdate = applyArtifactUpdates(state.messages, mergedResult, toolCallId);
      const resultStatus = toolResultStatus(artifactUpdate.result);
      let matched = false;
      state.messages = artifactUpdate.messages.map((message) =>
        messageMatchesToolEvent(message, toolName, toolCallId)
          ? (matched = true, { ...message, meta: { ...message.meta, status: resultStatus, result: artifactUpdate.result } })
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
            meta: { toolCallId, status: resultStatus, result: artifactUpdate.result },
          },
        ];
      }
      break;
    }
    case "mcp_event": {
      const toolCallId = String(data.toolCallId ?? "");
      const streamed = streamedMcpResult(data);
      const currentMessage = state.messages.find((message) => messageMatchesToolEvent(message, "call_mcp_tool", toolCallId));
      const mergedResult = mergeMcpResult(currentMessage?.meta?.result, streamed);
      const artifactUpdate = applyArtifactUpdates(state.messages, mergedResult, toolCallId);
      state.messages = artifactUpdate.messages.map((message) =>
        messageMatchesToolEvent(message, "call_mcp_tool", toolCallId)
          ? { ...message, meta: { ...message.meta, status: "running", result: artifactUpdate.result } }
          : message
      );
      break;
    }
    case "permission_request": {
      // The approval lives on the tool call that triggered it — the card flips to
      // "input required" and shows the prompt inline, so the command (and later
      // its output) stay together. The event always carries the toolCallId.
      finishRunningThinking(state);
      const toolCallId = String(data.toolCallId ?? "");
      const permission = {
        requestId: String(data.requestId ?? ""),
        justification: data.justification ? String(data.justification) : undefined,
        risk: data.risk ? String(data.risk) : undefined,
      };
      state.messages = state.messages.map((message) =>
        message.role === "tool_call" && String(message.meta?.toolCallId ?? "") === toolCallId
          ? { ...message, meta: { ...message.meta, status: "input_required", permission } }
          : message
      );
      break;
    }
    case "question": {
      // An ask_user prompt attaches to the tool call that asked it, same lifecycle
      // as a permission: the card flips to "input required" and renders the
      // question inline; the tool result finalizes it once answered.
      finishRunningThinking(state);
      const toolCallId = String(data.toolCallId ?? "");
      const question = {
        requestId: String(data.requestId ?? ""),
        questions: Array.isArray(data.questions) ? (data.questions as QuestionItem[]) : [],
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
      const toolName = String(data.name ?? data.tool ?? "");
      const toolCallId = String(data.toolCallId ?? "");
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
      }
      pushErrorMessage(state, friendlyErrorFromData(data), sourceId);
      break;
    }
    case "agent_group_started":
      state.lane = null;
      startAgentGroup(state, data);
      break;
    case "sub_task_text":
      state.agentGroups = withStep(state.agentGroups, String(data.groupId ?? ""), String(data.stepId ?? ""), (step) => appendAgentText(step, String(data.text ?? "")));
      break;
    case "sub_task_thinking":
      state.agentGroups = withStep(state.agentGroups, String(data.groupId ?? ""), String(data.stepId ?? ""), (step) =>
        applyAgentThinking(step, String(data.text ?? ""))
      );
      break;
    case "sub_task_thinking_done":
      state.agentGroups = withStep(state.agentGroups, String(data.groupId ?? ""), String(data.stepId ?? ""), (step) =>
        finishAgentThinkingWithDuration(step, Number(data.durationMs ?? 0))
      );
      break;
    case "sub_task_status":
      if (String(data.code ?? "") === "waiting_for_tools") {
        state.agentGroups = withStep(state.agentGroups, String(data.groupId ?? ""), String(data.stepId ?? ""), finishAgentThinking);
      }
      break;
    case "sub_task_tool_call":
      if (isControlToolName(data.name)) break;
      state.agentGroups = withStep(state.agentGroups, String(data.groupId ?? ""), String(data.stepId ?? ""), (step) => appendAgentToolCall(step, String(data.name ?? "unknown"), data.arguments as Record<string, unknown> | undefined, String(data.toolCallId ?? "")));
      break;
    case "sub_task_tool_result": {
      const toolName = String(data.name ?? "unknown");
      if (isControlToolName(toolName)) break;
      const toolCallId = String(data.toolCallId ?? "");
      const currentPartResult = agentToolResult(state.agentGroups, toolCallId);
      const mergedResult = toolName === "call_mcp_tool" ? mergeMcpFinalResult(currentPartResult, data.result) : data.result;
      const artifactUpdate = applyArtifactUpdatesToAgentGroups(state.agentGroups, mergedResult, toolCallId);
      state.agentGroups = withStep(artifactUpdate.agentGroups, String(data.groupId ?? ""), String(data.stepId ?? ""), (step) =>
        upsertAgentToolResult(step, toolName, toolCallId, artifactUpdate.result)
      );
      break;
    }
    case "sub_task_mcp_event": {
      const toolCallId = String(data.toolCallId ?? "");
      const streamed = streamedMcpResult(data);
      const currentPartResult = agentToolResult(state.agentGroups, toolCallId);
      const mergedResult = mergeMcpResult(currentPartResult, streamed);
      const artifactUpdate = applyArtifactUpdatesToAgentGroups(state.agentGroups, mergedResult, toolCallId);
      state.agentGroups = withStep(artifactUpdate.agentGroups, String(data.groupId ?? ""), String(data.stepId ?? ""), (step) =>
        upsertAgentToolResult(step, "call_mcp_tool", toolCallId, artifactUpdate.result)
      );
      break;
    }
    case "sub_task_done":
      state.agentGroups = withStep(state.agentGroups, String(data.groupId ?? ""), String(data.stepId ?? ""), (step) => finishAgentStep(step, data.task as A2ATask | undefined));
      break;
    default:
      break; // status (non-thinking), other — no UI change
  }
}

function reduceArtifactUpdate(state: ReduceState, result: Extract<A2AStreamResult, { kind: "artifact-update" }>): void {
  const text = streamArtifactText(result);
  if (!text.trim()) return;
  if (hasAssistantTextAfterLastUser(state)) return;
  pushAssistantText(state, text);
}

function reduceFinalStatus(state: ReduceState, result: Extract<A2AStreamResult, { kind: "status-update" }>): void {
  if (result.final || TERMINAL_STATES.has(result.status?.state as TaskState)) {
    finishRunningThinking(state);
    finishRunningTools(state);
  }
}

// Reconstruct messages + agentGroups from a session's persisted A2A tasks. The
// history arrives already compacted server-side (adjacent same-kind deltas merged),
// so it is reduced as-is — no client-side compaction pass.
function replayTasks(tasks: A2ATask[]): { messages: ChatMessage[]; agentGroups: AgentGroup[]; tasks: ChatTask[]; tokenUsage: TokenUsage | null; keyCounts: Map<string, number> } {
  const mainTasks = tasks
    .filter((task) => !(task.metadata && Array.isArray((task.metadata as Record<string, unknown>).referenceTaskIds)))
    .sort((first, second) => String(first.status?.timestamp ?? "").localeCompare(String(second.status?.timestamp ?? "")));
  const state: ReduceState = newReduceState();
  for (const task of mainTasks) {
    state.lane = null;
    // A task's full message stream is its history PLUS its trailing status
    // message: the A2A TaskManager keeps the latest message on `status.message`
    // and only folds it into `history` on the *next* status update. A turn
    // suspended mid-flight (e.g. awaiting a permission) never gets that next
    // update, so its last message — the pending request — lives only there.
    // Reduce both so replay reproduces exactly what the live stream showed
    // instead of dropping the trailing message and leaving the card stuck.
    const replayMessages = [...(task.history ?? [])];
    const trailing = task.status?.message;
    if (trailing && !replayMessages.some((message) => !!message.messageId && message.messageId === trailing.messageId)) {
      replayMessages.push(trailing);
    }
    for (const message of replayMessages) {
      if (message.role === "user") reduceUserMessage(state, message);
      else reduceAgentMessage(state, message);
    }
  }
  return { messages: state.messages, agentGroups: state.agentGroups, tasks: state.tasks, tokenUsage: state.tokenUsage, keyCounts: state.keyCounts };
}

export function useChat(
  agent: string,
  initialSessionId: string | null = null,
  workingDirectory?: string,
  workspaceStrategy: WorkspaceStrategy = "none",
  permissionMode: PermissionMode = "default",
  selectedModel: string = "",
  // Whether a turn is currently running on this session (from the server-tracked
  // running set). Drives the live subscribe stream when we are viewing — but not
  // driving — it.
  sessionRunning: boolean = false
) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [agentGroups, setAgentGroups] = useState<AgentGroup[]>([]);
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

  const abortControllerRef = useRef<AbortController | null>(null);
  const stateRef = useRef<ReduceState>(newReduceState());
  const sessionIdRef = useRef<string | null>(initialSessionId);
  const historyLoadedForRef = useRef<string | null>(null);
  const historyFragmentsRef = useRef<A2ATask[]>([]);
  const historyPageCursorRef = useRef<number | null>(null);
  const hasOlderHistoryRef = useRef(false);
  const isOlderHistoryLoadingRef = useRef(false);
  const queuedMessagesRef = useRef<QueuedMessage[]>([]);
  // Widget interactions that arrived mid-turn wait here and drain after the
  // current stream finishes. Kept separate from the user-visible text queue.
  const queuedWidgetEventsRef = useRef<WidgetEvent[]>([]);
  // Render errors fire automatically when a widget mounts, so a broken artifact
  // (including on session replay) must not spawn the same turn twice. Tracked by
  // artifact + message; user-driven events (clicks) are never deduped.
  const seenRenderErrorsRef = useRef<Set<string>>(new Set());
  const isStreamingRef = useRef(false);
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
    setAgentGroups(stateRef.current.agentGroups);
    setTasks(stateRef.current.tasks);
    setTokenUsage(stateRef.current.tokenUsage);
  }, []);

  const flush = useCallback(() => {
    if (typeof window === "undefined") {
      setMessages(stateRef.current.messages);
      setAgentGroups(stateRef.current.agentGroups);
      setTasks(stateRef.current.tasks);
      setTokenUsage(stateRef.current.tokenUsage);
      return;
    }
    if (flushFrameRef.current != null) return;
    flushFrameRef.current = window.requestAnimationFrame(() => {
      flushFrameRef.current = null;
      setMessages(stateRef.current.messages);
      setAgentGroups(stateRef.current.agentGroups);
      setTasks(stateRef.current.tasks);
      setTokenUsage(stateRef.current.tokenUsage);
    });
  }, []);

  const applyHistoryFragments = useCallback(() => {
    const replayed = replayTasks(historyFragmentsRef.current);
    stateRef.current = { messages: replayed.messages, agentGroups: replayed.agentGroups, tasks: replayed.tasks, lane: null, tokenUsage: replayed.tokenUsage, keyCounts: replayed.keyCounts };
    flushNow();
  }, [flushNow]);

  const notifyTurnError = useCallback((taskId: string, message: A2AMessage | undefined) => {
    const error = friendlyErrorFromMessage(message);
    if (!error) return;
    const key = `${taskId || sessionIdRef.current || "turn"}:${error.code}:${error.status ?? ""}`;
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
      abortControllerRef.current?.abort();
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
          const page = await fetchSessionTasksPage(initialSessionId, null, controller.signal, HISTORY_PAGE_LIMIT);
          if (cancelled) return;
          historyFragmentsRef.current = page.tasks;
          historyPageCursorRef.current = page.next_before_row_id;
          hasOlderHistoryRef.current = page.has_more;
          setHasOlderHistory(page.has_more);
          setSessionId(initialSessionId);
          sessionIdRef.current = initialSessionId;
          applyHistoryFragments();
          setIsHistoryLoading(false);
          return;
        } catch {
          if (cancelled || controller.signal.aborted) return;
          if (attempt === MAX_ATTEMPTS - 1) {
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
        const page = await fetchSessionTasksPage(ctx, cursor, undefined, HISTORY_PAGE_LIMIT);
        if (sessionIdRef.current !== ctx) return;
        historyFragmentsRef.current = [...page.tasks, ...historyFragmentsRef.current];
        historyPageCursorRef.current = page.next_before_row_id;
        hasOlderHistoryRef.current = page.has_more;
        cursor = page.next_before_row_id;
        fetchedAny = true;
      }
    } catch {
      // Leave what we have loaded; the fill effect re-triggers and resumes from the
      // last cursor since hasOlderHistoryRef is still true.
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

  // Live updates for a session that is running on the server but is not being
  // driven by this hook (we switched back to it, or it was started elsewhere).
  // Subscribes to the server's per-context event stream: one compacted snapshot
  // (catch-up) then a live tail of each emitted part, applied as O(delta) updates
  // through the same reducer the driver uses — instead of polling and re-replaying
  // the whole transcript every second. Never subscribes while we're driving the
  // turn ourselves; the live message/stream SSE is authoritative for that.
  useEffect(() => {
    if (!initialSessionId) return;
    if (streamedLocallyRef.current || isStreamingRef.current) return;

    let cancelled = false;
    let subscription: { abort: () => void } | null = null;
    const controller = new AbortController();

    const applySnapshot = (tasks: A2ATask[]) => {
      const replayed = replayTasks(tasks);
      stateRef.current = { messages: replayed.messages, agentGroups: replayed.agentGroups, tasks: replayed.tasks, lane: null, tokenUsage: replayed.tokenUsage, keyCounts: replayed.keyCounts };
      sessionIdRef.current = initialSessionId;
      setSessionId(initialSessionId);
      flushNow();
    };

    if (sessionRunning) {
      wasRunningRef.current = true;
      subscription = subscribeSessionStream(
        initialSessionId,
        (frame) => {
          if (cancelled || isStreamingRef.current || streamedLocallyRef.current) return;
          if (frame.kind === "snapshot") {
            applySnapshot(frame.tasks);
            setHistoryError(false);
            setIsHistoryLoading(false);
          } else if (frame.kind === "live") {
            reduceAgentMessage(stateRef.current, frame.message);
            flush();
          }
        },
        () => {
          // Stream closed (turn finished or dropped). The sessionRunning flip to
          // false re-runs this effect and the else branch captures final state.
        },
      );
    } else if (wasRunningRef.current) {
      // The turn just finished — capture its terminal state once (the live tail
      // misses the final artifact, which arrives as a separate artifact-update),
      // then stop.
      wasRunningRef.current = false;
      void (async () => {
        try {
          const tasks = await fetchSessionTasks(initialSessionId, controller.signal);
          if (!cancelled && !isStreamingRef.current && !streamedLocallyRef.current) applySnapshot(tasks);
        } catch {
          // transient — leave the last live state in place
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
      // Optimistic input message + reset the open prose lane. Widget events are
      // delivered to the model but never shown as a chat entry (an error is the
      // model's own realization; an interaction is silent), so only text appears.
      stateRef.current.lane = null;
      if (input.kind === "text") {
        stateRef.current.messages = [
          ...stateRef.current.messages,
          { id: `user-${Date.now()}`, role: "user", content: input.text, timestamp: new Date().toISOString() },
        ];
        flush();
      }

      isStreamingRef.current = true;
      streamedLocallyRef.current = true;
      setIsStreaming(true);

      const text = input.kind === "text" ? input.text : "";
      const dataPart =
        input.kind === "widget"
          ? {
              kind: "widget_event",
              artifactId: input.event.artifactId,
              title: input.event.title,
              event: input.event.event,
              data: input.event.data,
            }
          : undefined;

      abortControllerRef.current = streamA2A(
        text,
        agent,
        sessionIdRef.current,
        (result: A2AStreamResult) => {
          const kind = (result as { kind?: string }).kind;
          if (kind === "status-update") {
            const update = result as Extract<A2AStreamResult, { kind: "status-update" }>;
            if (update.status?.state === "failed") {
              notifyTurnError(update.taskId, update.status.message);
            }
            if (update.contextId && !sessionIdRef.current) {
              sessionIdRef.current = update.contextId;
              setSessionId(update.contextId);
            }
            if (update.status?.message) {
              acknowledgeSteering(steeringText(update.status.message));
              reduceAgentMessage(stateRef.current, update.status.message);
            }
            reduceFinalStatus(stateRef.current, update);
            flush();
          } else if (kind === "artifact-update") {
            reduceArtifactUpdate(stateRef.current, result as Extract<A2AStreamResult, { kind: "artifact-update" }>);
            flush();
          } else if (!kind || kind === "task") {
            const task = result as A2ATask;
            if (task.contextId && !sessionIdRef.current) {
              sessionIdRef.current = task.contextId;
              setSessionId(task.contextId);
            }
          } else if (kind === "message") {
            const message = result as unknown as A2AMessage;
            acknowledgeSteering(steeringText(message));
            reduceAgentMessage(stateRef.current, message);
            flush();
          }
        },
        () => {
          stateRef.current.lane = null;
          // Drain queued text first (user intent), then any widget events that
          // arrived mid-turn.
          const pendingText = queuedMessagesRef.current.filter((message) => !message.steering);
          const pendingWidget = queuedWidgetEventsRef.current;
          if (pendingText.length > 0) {
            const next = pendingText[0];
            setQueue(queuedMessagesRef.current.filter((message) => message.id !== next.id));
            runStreamRef.current({ kind: "text", text: next.text });
          } else if (pendingWidget.length > 0) {
            const [next, ...rest] = pendingWidget;
            queuedWidgetEventsRef.current = rest;
            runStreamRef.current({ kind: "widget", event: next });
          } else {
            isStreamingRef.current = false;
            setIsStreaming(false);
          }
        },
        workingDirectory,
        workspaceStrategy,
        permissionMode,
        selectedModel,
        dataPart
      );
    },
    [agent, workingDirectory, workspaceStrategy, permissionMode, selectedModel, flush, setQueue, acknowledgeSteering, notifyTurnError]
  );

  useEffect(() => {
    runStreamRef.current = runStream;
  }, [runStream]);

  const send = useCallback(
    (text: string) => {
      const trimmed = text.trim();
      if (!trimmed) return;
      if (isStreamingRef.current) {
        const pending = { id: crypto.randomUUID(), text: trimmed, steering: false };
        const ctx = sessionIdRef.current;
        if (ctx) {
          setQueue([...queuedMessagesRef.current, { ...pending, steering: true }]);
          steerSession(ctx, trimmed)
            .then((queued) => {
              if (!queued) {
                setQueue(queuedMessagesRef.current.map((message) =>
                  message.id === pending.id ? { ...message, steering: false } : message
                ));
              }
            })
            .catch(() => {
              setQueue(queuedMessagesRef.current.map((message) =>
                message.id === pending.id ? { ...message, steering: false } : message
              ));
            });
          return;
        }
        setQueue([...queuedMessagesRef.current, pending]);
        return;
      }
      runStream({ kind: "text", text: trimmed });
    },
    [runStream, setQueue]
  );

  // A rendered widget posted an interaction. Deliver it as a structured turn,
  // queueing behind the active stream if one is running.
  const sendWidgetEvent = useCallback(
    (event: WidgetEvent) => {
      if (event.event === "render_error") {
        const message = typeof (event.data as { message?: unknown })?.message === "string"
          ? (event.data as { message: string }).message
          : JSON.stringify(event.data);
        const signature = `${event.artifactId}|${message}`;
        if (seenRenderErrorsRef.current.has(signature)) return;
        seenRenderErrorsRef.current.add(signature);
      }
      if (isStreamingRef.current) {
        queuedWidgetEventsRef.current = [...queuedWidgetEventsRef.current, event];
        return;
      }
      runStream({ kind: "widget", event });
    },
    [runStream]
  );

  const handlePermission = useCallback(
    async (requestId: string, decision: PermissionDecision) => {
      const ctx = sessionIdRef.current;
      if (!ctx) return;
      await resolvePermission(ctx, requestId, decision);
      // Record the decision and hand the card back to its normal lifecycle: an
      // approval resumes as "running" (the result/error finalizes it), a denial
      // is settled by the error the backend emits for this tool call.
      stateRef.current.messages = stateRef.current.messages.map((message) => {
        const permission = message.meta?.permission as { requestId?: string } | undefined;
        if (message.role !== "tool_call" || permission?.requestId !== requestId) return message;
        return {
          ...message,
          meta: { ...message.meta, status: "running", permission: { ...permission, decision } },
        };
      });
      flush();
    },
    [flush]
  );

  const handleQuestion = useCallback(
    async (requestId: string, answers: QuestionAnswer[]) => {
      const ctx = sessionIdRef.current;
      if (!ctx) return;
      await resolveQuestion(ctx, requestId, answers);
      // Record the answer and hand the card back to its running lifecycle; the
      // tool_result finalizes it (same shape as a resolved permission).
      stateRef.current.messages = stateRef.current.messages.map((message) => {
        const question = message.meta?.question as { requestId?: string } | undefined;
        if (message.role !== "tool_call" || question?.requestId !== requestId) return message;
        return {
          ...message,
          meta: { ...message.meta, status: "running", question: { ...question, answers } },
        };
      });
      flush();
    },
    [flush]
  );

  const abort = useCallback(() => {
    const ctx = sessionIdRef.current;
    if (ctx) abortSession(ctx);
    else abortControllerRef.current?.abort();
  }, []);

  const abortTool = useCallback((toolCallId: string) => {
    const ctx = sessionIdRef.current;
    if (!ctx || !toolCallId) return;
    void abortToolCall(ctx, toolCallId);
    stateRef.current.messages = stateRef.current.messages.map((message) => (
      message.role === "tool_call" && message.meta?.toolCallId === toolCallId
        ? { ...message, meta: { ...message.meta, status: "failed" } }
        : message
    ));
    flush();
  }, [flush]);

  const dequeueMessage = useCallback((index: number) => {
    setQueue(queuedMessagesRef.current.filter((_, i) => i !== index));
  }, [setQueue]);

  const reset = useCallback(() => {
    abort();
    stateRef.current = newReduceState();
    setMessages([]);
    setAgentGroups([]);
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
    agentGroups,
    tasks,
    tokenUsage,
    queuedMessages,
    sessionId,
    isStreaming,
    isHistoryLoading,
    historyError,
    hasOlderHistory,
    isOlderHistoryLoading,
    reloadHistory,
    send,
    sendWidgetEvent,
    abort,
    abortTool,
    reset,
    dequeueMessage,
    handlePermission,
    handleQuestion,
  };
}
