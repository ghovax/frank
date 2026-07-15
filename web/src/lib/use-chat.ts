"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  streamA2A,
  abortSession,
  abortToolCall,
  steerSession,
  compactSession,
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
import { isHiddenToolEventName, isSameToolEvent, type PermissionDecision, type QuestionAnswer, type QuestionItem, type ToolEvent, type ToolEventStatus } from "./tool-event";
import { artifactImageKey, type ArtifactAnnotationRecord, type ArtifactImageAnnotation } from "./artifact-annotations";
import type { ArtifactEvent } from "@/components/artifact-bridge";
import { toaster } from "@/components/ui/toaster";
import { asArray, asRecord } from "@/lib/coerce";

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

// A file the user attached to a turn — the metadata needed to render a chip and
// show it (name, on-disk path served by /artifact-page, mime, size). Carried on the
// user message's `meta.attachments`, both on the live send and on replay.
export interface MessageAttachment {
  filename: string;
  path: string;
  mimeType: string;
  size: number;
  annotations?: ArtifactImageAnnotation[];
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "tool_call" | "thinking" | "error" | "warning" | "compaction";
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
  // Combined spend of agents spawned this session — a separate bucket (agents
  // run in their own context; only their deliverable returns to the parent).
  agentInputTokens: number;
  agentOutputTokens: number;
  agentTotalTokens: number;
  agentModelCalls: number;
}

// A turn's input: either typed text or a structured artifact interaction. Both
// drive the same stream; an artifact event travels as a typed DataPart, never as
// prose, so the agent receives it as structured JSON.
export type ChatInput =
  | { kind: "text"; text: string; dataParts?: Record<string, unknown>[] }
  | { kind: "artifact"; event: ArtifactEvent };

export interface QueuedMessage {
  id: string;
  text: string;
  steering: boolean;
  dataParts?: Record<string, unknown>[];
}

// An agent step's ordered timeline — prose, reasoning, and tool calls
// interleaved, mirroring the main chat. Built from that agent's path-tagged events
// (the same unified vocabulary as the root agent). Reasoning is captured here for
// fidelity but, like the main chat, is surfaced by the agents panel as a live status.
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

// Legacy fallback: infer lifecycle from a result payload's `code` suffix. Only used
// when a wire event carries no explicit status (a replayed pre-migration part).
function toolResultStatus(result: unknown): ToolEventStatus {
  const code = String(asRecord(result).code ?? "");
  if (code === "tool_error") return "failed";
  if (code.endsWith("_started") || code === "background_task_scheduled") return "running";
  return "completed";
}

// The explicit ToolStatus from the wire mapped to the UI lifecycle. `input_required`
// is a separate, UI-only state driven by permission/question events, never a result.
function statusFromWire(wireStatus: unknown, result: unknown): ToolEventStatus {
  switch (String(wireStatus ?? "")) {
    case "ok":
      return "completed";
    case "error":
      return "failed";
    case "running":
      return "running";
    default:
      return toolResultStatus(result);
  }
}

function upsertAgentToolResult(step: AgentStep, name: string, toolCallId: string, result: unknown, status: ToolEventStatus): AgentStep {
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
        return { title: "Server request failed", message: "Daisy could not start the turn. Check the server log and try again." };
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
    // "replace"/"update") loses the artifact entirely on its first render.
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

// Normalize the attachments carried by an `attachments` data payload into the lean
// shape the UI renders. Shared by the live send path (the outgoing dataPart) and
// replay (the persisted user-message data part), so a chip looks identical whether it
// was just sent or reloaded from history. Any per-image annotations ride along too, so
// a sent image can display its pins read-only.
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
      annotations: Array.isArray(record.annotations) ? (record.annotations as ArtifactImageAnnotation[]) : undefined,
    });
  }
  return attachments;
}

// The annotations ride along on the message as the model-facing `annotationsPayload`
// shape (`position.x_ratio` / `image_size`), not the `ArtifactImageAnnotation` shape the
// renderer positions markers from. Map them back so the pins land on the image instead of
// collapsing at 0,0. Tolerates an already-mapped annotation too (reload/round-trip).
function artifactImageAnnotationFromPayload(raw: unknown): ArtifactImageAnnotation {
  const record = asRecord(raw);
  const position = asRecord(record.position);
  const size = asRecord(record.image_size);
  const number = (value: unknown): number => (typeof value === "number" ? value : Number(value) || 0);
  const xRatio = number(record.xRatio ?? position.x_ratio ?? number(position.x) / 999);
  const yRatio = number(record.yRatio ?? position.y_ratio ?? number(position.y) / 999);
  const imageWidth = number(record.imageWidth ?? size.width);
  const imageHeight = number(record.imageHeight ?? size.height);
  return {
    id: String(record.id ?? ""),
    sequence: number(record.sequence),
    x: number(record.x ?? Math.round(xRatio * imageWidth)),
    y: number(record.y ?? Math.round(yRatio * imageHeight)),
    xRatio,
    yRatio,
    imageWidth,
    imageHeight,
    comment: String(record.comment ?? ""),
    createdAt: String(record.createdAt ?? record.created_at ?? ""),
    updatedAt: String(record.updatedAt ?? record.updated_at ?? ""),
  };
}

function artifactAnnotationRecordFromData(data: Record<string, unknown> | undefined): ArtifactAnnotationRecord | null {
  if (!data || data.kind !== "artifact_image_annotations") return null;
  const image = asRecord(data.image);
  const annotations = Array.isArray(data.annotations) ? data.annotations.map(artifactImageAnnotationFromPayload) : [];
  const artifactId = String(image.artifact_id ?? image.artifactId ?? "");
  const versionId = String(image.version_id ?? image.versionId ?? "");
  if ((!artifactId || !versionId) || annotations.length === 0) return null;
  const versionSeqRaw = image.version_seq ?? image.versionSeq;
  return {
    image: {
      key: String(image.key ?? artifactImageKey(artifactId, versionId)),
      artifactId,
      versionId,
      title: String(image.title ?? "Image"),
      source: String(image.source ?? ""),
      name: String(image.name ?? ""),
      versionSeq: typeof versionSeqRaw === "number" ? versionSeqRaw : Number(versionSeqRaw) || 0,
    },
    annotations,
    updatedAt: String(data.updatedAt ?? data.updated_at ?? new Date().toISOString()),
  };
}

// The attachments carried on a whole message's parts (replay path — the persisted
// user message keeps its attachments DataPart alongside the text part).
function attachmentsFromMessage(message: A2AMessage): MessageAttachment[] {
  const attachments: MessageAttachment[] = [];
  for (const part of message.parts ?? []) {
    if (part.kind === "data") attachments.push(...attachmentsFromData(part.data));
  }
  return attachments;
}

function artifactAnnotationRecordsFromMessage(message: A2AMessage): ArtifactAnnotationRecord[] {
  const records: ArtifactAnnotationRecord[] = [];
  for (const part of message.parts ?? []) {
    if (part.kind !== "data") continue;
    const record = artifactAnnotationRecordFromData(part.data);
    if (record) records.push(record);
  }
  return records;
}

function reduceUserMessage(state: ReduceState, message: A2AMessage): void {
  const text = (message.parts ?? []).filter((part) => part.kind === "text").map((part) => part.text ?? "").join("");
  const attachments = attachmentsFromMessage(message);
  const artifactAnnotationRecords = artifactAnnotationRecordsFromMessage(message);
  // A user-role message with no visible prose AND no attachments is a silent
  // artifact interaction (the user clicked something in an artifact; the payload is a
  // data part, not text). The live path renders nothing for these, so replay must
  // match — never a blank bubble. (Autonomous background-resume wakes are
  // agent-authored, not user messages, so they never reach this path at all.) A
  // typed user turn always carries prose; an attachment-only turn carries chips.
  if (!text.trim() && attachments.length === 0 && artifactAnnotationRecords.length === 0) return;
  state.lane = null;
  const meta = {
    ...(attachments.length > 0 ? { attachments } : {}),
    ...(artifactAnnotationRecords.length > 0 ? { artifactAnnotationRecords } : {}),
  };
  state.messages = [
    ...state.messages,
    {
      id: stableMessageId(state, "user", message.messageId),
      role: "user",
      content: text,
      timestamp: new Date().toISOString(),
      ...(Object.keys(meta).length > 0 ? { meta } : {}),
    },
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

// Mark an agent step terminal: close its thinking/tools and stamp the task state.
function finishAgentStepState(step: AgentStep, taskState: string): AgentStep {
  const next = finishRunningAgentTools(finishAgentThinking(step));
  return { ...next, state: (taskState as TaskState) || "completed" };
}

// Ensure the group + step for a lane exist (events can arrive before group_started),
// and link the spawning tool-call card in the transcript to this group.
function ensureLaneGroup(state: ReduceState, groupId: string, stepId: string, agent?: string, goal?: string, toolCallId?: string): void {
  const group = state.agentGroups.find((candidate) => candidate.groupId === groupId);
  if (!group) {
    state.agentGroups = [...state.agentGroups, { groupId, toolCallId: toolCallId ?? "", steps: [emptyStep(stepId, agent ?? "", goal ?? "")] }];
  } else if (!group.steps.some((step) => step.stepId === stepId)) {
    state.agentGroups = state.agentGroups.map((candidate) =>
      candidate.groupId === groupId ? { ...candidate, steps: [...candidate.steps, emptyStep(stepId, agent ?? "", goal ?? "")] } : candidate
    );
  } else if (agent) {
    state.agentGroups = withStep(state.agentGroups, groupId, stepId, (step) => ({ ...step, agent: agent || step.agent, goal: goal || step.goal }));
  }
  if (toolCallId) {
    state.messages = state.messages.map((message) =>
      message.role === "tool_call" && String(message.meta?.toolCallId ?? "") === toolCallId
        ? { ...message, meta: { ...message.meta, groupId, status: "running" } }
        : message
    );
  }
}

// A path-tagged event (path non-empty) belongs to an agent. It uses the SAME
// unified vocabulary as the root agent; we route it into the agents panel keyed by the
// deepest path segment. An agent is just an agent at a deeper path — there is no
// separate sub_task_* vocabulary any more.
function reduceAgentLaneEvent(state: ReduceState, data: Record<string, unknown>): void {
  const path = Array.isArray(data.path) ? (data.path as Array<Record<string, unknown>>) : [];
  const segment = path[path.length - 1] ?? {};
  const groupId = String(segment.group_id ?? "");
  const stepId = String(segment.step_id ?? "");
  if (!groupId || !stepId) return;
  const apply = (updater: (step: AgentStep) => AgentStep) => {
    state.agentGroups = withStep(state.agentGroups, groupId, stepId, updater);
  };
  switch (String(data.kind ?? "")) {
    case "group_started":
      ensureLaneGroup(state, groupId, stepId, String(data.agent_name ?? ""), String(data.title ?? ""), String(data.tool_call_id ?? ""));
      break;
    case "text":
      ensureLaneGroup(state, groupId, stepId);
      apply((step) => appendAgentText(step, String(data.text ?? "")));
      break;
    case "thinking":
      ensureLaneGroup(state, groupId, stepId);
      apply((step) => applyAgentThinking(step, String(data.text ?? "")));
      break;
    case "thinking_done":
      apply((step) => finishAgentThinkingWithDuration(step, Number(data.duration_ms ?? 0)));
      break;
    case "status":
      if (String(data.code ?? "") === "waiting_for_tools") apply(finishAgentThinking);
      break;
    case "tool_call":
      if (isHiddenToolEventName(data.tool_name)) break;
      ensureLaneGroup(state, groupId, stepId);
      apply((step) => appendAgentToolCall(step, String(data.tool_name ?? "unknown"), data.arguments as Record<string, unknown> | undefined, String(data.tool_call_id ?? "")));
      break;
    case "tool_result": {
      const toolName = String(data.tool_name ?? "unknown");
      if (isHiddenToolEventName(toolName)) break;
      const toolCallId = String(data.tool_call_id ?? "");
      const mergedResult = toolName === "call_mcp_tool" ? mergeMcpFinalResult(agentToolResult(state.agentGroups, toolCallId), data.display) : data.display;
      const artifactUpdate = applyArtifactUpdatesToAgentGroups(state.agentGroups, mergedResult, toolCallId);
      const status = statusFromWire(data.status, artifactUpdate.result);
      state.agentGroups = withStep(artifactUpdate.agentGroups, groupId, stepId, (step) => upsertAgentToolResult(step, toolName, toolCallId, artifactUpdate.result, status));
      break;
    }
    case "mcp_event": {
      const toolCallId = String(data.tool_call_id ?? "");
      const mergedResult = mergeMcpResult(agentToolResult(state.agentGroups, toolCallId), streamedMcpResult(data));
      const artifactUpdate = applyArtifactUpdatesToAgentGroups(state.agentGroups, mergedResult, toolCallId);
      state.agentGroups = withStep(artifactUpdate.agentGroups, groupId, stepId, (step) => upsertAgentToolResult(step, "call_mcp_tool", toolCallId, artifactUpdate.result, "running"));
      break;
    }
    case "done":
      apply((step) => finishAgentStepState(step, String(data.state ?? "completed")));
      break;
    default:
      break;
  }
}

function reduceDataPart(state: ReduceState, data: Record<string, unknown>, sourceId?: string): void {
  const kind = String(data.kind ?? "");
  // An agent event (non-empty path) is routed to the agents panel; the root
  // agent's own events (empty path) drive the main transcript below.
  if (Array.isArray(data.path) && data.path.length > 0) {
    reduceAgentLaneEvent(state, data);
    return;
  }
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
    case "compaction": {
      // A context-compaction marker: "started" shows a live compacting indicator,
      // "done" turns it into the separator (or, when nothing was compacted, drops
      // it). Renders as a full-width divider — not an assistant/user bubble.
      state.lane = null;
      const status = String(data.status ?? "");
      if (status === "started") {
        state.messages = [
          ...state.messages,
          {
            id: stableMessageId(state, "compaction", sourceId),
            role: "compaction",
            content: "",
            timestamp: new Date().toISOString(),
            meta: { status: "running", reason: String(data.reason ?? "") },
          },
        ];
        break;
      }
      if (status === "done") {
        const changed = data.ok !== false && Number(data.messages_after ?? 0) < Number(data.messages_before ?? 0);
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
          reason: String(data.reason ?? ""),
          messagesBefore: Number(data.messages_before ?? 0),
          messagesAfter: Number(data.messages_after ?? 0),
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
      const cumulative = asRecord(data.cumulative);
      // Per-call (latest) figures — this is the actual current context, not a sum.
      const contextInputTokens = Number(data.input_tokens ?? 0);
      const contextOutputTokens = Number(data.output_tokens ?? 0);
      const agents = asRecord(data.agents);
      state.tokenUsage = {
        inputTokens: Number(cumulative.input_tokens ?? 0),
        outputTokens: Number(cumulative.output_tokens ?? 0),
        totalTokens: Number(cumulative.total_tokens ?? 0),
        cacheReadTokens: Number(cumulative.cache_read_tokens ?? 0),
        reasoningTokens: Number(cumulative.reasoning_tokens ?? 0),
        modelCalls: Number(cumulative.model_calls ?? 0),
        contextInputTokens,
        contextOutputTokens,
        contextTokens: contextInputTokens + contextOutputTokens,
        contextWindow: Number(data.context_window ?? 0),
        agentInputTokens: Number(agents.input_tokens ?? 0),
        agentOutputTokens: Number(agents.output_tokens ?? 0),
        agentTotalTokens: Number(agents.total_tokens ?? 0),
        agentModelCalls: Number(agents.model_calls ?? 0),
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
      finishRunningThinkingWithDuration(state, Number(data.duration_ms ?? 0));
      break;
    case "tool_call": {
      // Every tool call breaks the prose lane so surrounding text doesn't run
      // together. Hidden runtime envelopes such as query still cause the break.
      state.lane = null;
      if (isHiddenToolEventName(data.tool_name)) break;
      finishRunningThinking(state);
      const toolCallId = String(data.tool_call_id ?? "");
      state.messages = [
        ...state.messages,
        {
          id: `toolcall-${toolCallId || crypto.randomUUID()}`,
          role: "tool_call",
          content: String(data.tool_name ?? "unknown"),
          timestamp: new Date().toISOString(),
          meta: { arguments: data.arguments, toolCallId, status: "running" },
        },
      ];
      break;
    }
    case "tool_result": {
      if (isHiddenToolEventName(data.tool_name)) break;
      finishRunningThinking(state);
      state.lane = null;
      const toolName = String(data.tool_name ?? "");
      const toolCallId = String(data.tool_call_id ?? "");
      const currentMessage = state.messages.find((message) => messageMatchesToolEvent(message, toolName, toolCallId));
      const mergedResult = toolName === "call_mcp_tool" ? mergeMcpFinalResult(currentMessage?.meta?.result, data.display) : data.display;
      const artifactUpdate = applyArtifactUpdates(state.messages, mergedResult, toolCallId);
      const resultStatus = statusFromWire(data.status, artifactUpdate.result);
      // set_tasks / update_tasks complete through this same universal path and carry
      // the authoritative task list for the side panel inside their result.
      const resultTasks = asRecord(artifactUpdate.result).tasks;
      if (Array.isArray(resultTasks)) state.tasks = mergeTasks(state.tasks, resultTasks);
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
      const toolCallId = String(data.tool_call_id ?? "");
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
      const toolCallId = String(data.tool_call_id ?? "");
      const permission = {
        requestId: String(data.request_id ?? ""),
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
      const toolCallId = String(data.tool_call_id ?? "");
      const question = {
        requestId: String(data.request_id ?? ""),
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
      const toolName = String(data.tool_name ?? "");
      const toolCallId = String(data.tool_call_id ?? "");
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
      const title = String(data.title ?? "Warning");
      const message = String(data.message ?? "");
      state.messages = [
        ...state.messages,
        {
          id: stableMessageId(state, "warning", sourceId),
          role: "warning",
          content: message ? `${title} — ${message}` : title,
          timestamp: new Date().toISOString(),
          meta: { warning: { code: String(data.code ?? ""), title, message } },
        },
      ];
      break;
    }
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
    finishActiveTools(state);
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
    if (TERMINAL_STATES.has(task.status?.state as TaskState)) finishActiveTools(state);
  }
  // Related child tasks are not transcript turns, but their persisted terminal
  // state is authoritative for the exact agents-panel lane they own. This closes
  // a lane even when its live `done` event was missed because the parent was
  // stopped, the viewer disconnected, or the server restarted.
  for (const task of tasks) {
    const metadata = task.metadata as Record<string, unknown> | undefined;
    if (!metadata || !Array.isArray(metadata.referenceTaskIds)) continue;
    const lane = metadata.agentLane as Record<string, unknown> | undefined;
    const groupId = String(lane?.groupId ?? "");
    const stepId = String(lane?.stepId ?? "");
    const taskState = task.status?.state as TaskState;
    if (!groupId || !stepId || !TERMINAL_STATES.has(taskState)) continue;
    state.agentGroups = withStep(
      state.agentGroups,
      groupId,
      stepId,
      (step) => finishAgentStepState(step, taskState),
    );
  }
  return { messages: state.messages, agentGroups: state.agentGroups, tasks: state.tasks, tokenUsage: state.tokenUsage, keyCounts: state.keyCounts };
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
  // Artifact interactions that arrived mid-turn wait here and drain after the
  // current stream finishes. Kept separate from the user-visible text queue.
  const queuedArtifactEventsRef = useRef<ArtifactEvent[]>([]);
  // Render errors fire automatically when an artifact mounts, so a broken artifact
  // (including on session replay) must not spawn the same turn twice. Tracked by
  // artifact + message; user-driven events (clicks) are never deduped.
  const seenRenderErrorsRef = useRef<Set<string>>(new Set());
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
      // Optimistic input message + reset the open prose lane. Artifact events are
      // delivered to the model but never shown as a chat entry (an error is the
      // model's own realization; an interaction is silent), so only text appears.
      stateRef.current.lane = null;
      const userMessageId = crypto.randomUUID();
      if (input.kind === "text") {
        const dataParts = input.dataParts ?? [];
        const attachments = dataParts.flatMap((dataPart) => attachmentsFromData(dataPart));
        const artifactAnnotationRecords = dataParts
          .map((dataPart) => artifactAnnotationRecordFromData(dataPart))
          .filter((record): record is ArtifactAnnotationRecord => Boolean(record));
        const meta = {
          ...(attachments.length > 0 ? { attachments } : {}),
          ...(artifactAnnotationRecords.length > 0 ? { artifactAnnotationRecords } : {}),
        };
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
      }

      isStreamingRef.current = true;
      streamedLocallyRef.current = true;
      setIsStreaming(true);

      const text = input.kind === "text" ? input.text : "";
      const dataParts = input.kind === "artifact"
        ? [{
            kind: "artifact_event",
            artifactId: input.event.artifactId,
            title: input.event.title,
            event: input.event.event,
            data: input.event.data,
          }]
        : input.dataParts;

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
          // The turn's stream has closed. A clean turn already settled its cards via
          // the terminal status-update; but an abort (Stop) or a connection that drops
          // mid-turn (server stall, network loss) closes the stream with no terminal
          // status, which would otherwise leave every in-flight tool/thinking card
          // spinning forever. Sweep here as the single catch-all so a card can never
          // outlive its stream.
          finishRunningThinking(stateRef.current);
          finishActiveTools(stateRef.current);
          flush();
          // Drain queued text first (user intent), then any artifact events that
          // arrived mid-turn. A message still flagged `steering` here was accepted by
          // the backend but never echoed back (`acknowledgeSteering` clears the flag
          // when steering is honored mid-turn) — the turn ended, failed, or raced past
          // its last drain before applying it. It was NOT applied, so treat it exactly
          // like any other pending message and send it as a fresh turn, rather than
          // stranding it forever as a "Steering next opening" chip. The backend
          // discards its own copy on stream close, so this can't double-apply.
          const pendingText = queuedMessagesRef.current;
          const pendingArtifact = queuedArtifactEventsRef.current;
          // A user Stop closes the stream; do not immediately relaunch a queued
          // message as a new turn — that is exactly the "Stop didn't stop" symptom.
          // Consume the one-shot flag and fall through to idle, leaving the queue for
          // the user to send deliberately.
          const abortedByUser = abortedByUserRef.current;
          abortedByUserRef.current = false;
          if (!abortedByUser && pendingText.length > 0) {
            const next = pendingText[0];
            setQueue(queuedMessagesRef.current.filter((message) => message.id !== next.id));
            runStreamRef.current({ kind: "text", text: next.text, dataParts: next.dataParts });
          } else if (!abortedByUser && pendingArtifact.length > 0) {
            const [next, ...rest] = pendingArtifact;
            queuedArtifactEventsRef.current = rest;
            runStreamRef.current({ kind: "artifact", event: next });
          } else {
            isStreamingRef.current = false;
            setIsStreaming(false);
            // Our locally-driven turn is over. Return to viewer mode so that if the
            // harness later wakes this session on its own (an autonomous background
            // resume), the read-only subscribe stream picks the new turn up live
            // instead of the wake only appearing on a manual reload.
            streamedLocallyRef.current = false;
          }
        },
        workingDirectory,
        workspaceStrategy,
        permissionMode,
        userMessageId,
        dataParts,
        projectId
      );
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
        // While the turn is paused on a pending decision (a permission or question
        // prompt), a new message can only be plain-queued — never steered. Steering
        // could not be honored until the decision is resolved anyway, and a "Steering
        // next opening" chip would misrepresent that. It drains when the turn ends.
        if (ctx && !queueOnly && dataParts.length === 0) {
          setQueue([...queuedMessagesRef.current, { ...pending, steering: true }]);
          return steerSession(ctx, trimmed)
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
        }
        setQueue([...queuedMessagesRef.current, pending]);
        return Promise.resolve();
      }
      runStream({ kind: "text", text: trimmed, dataParts });
      return Promise.resolve();
    },
    [runStream, setQueue]
  );

  // A rendered artifact posted an interaction. Deliver it as a structured turn,
  // queueing behind the active stream if one is running.
  const sendArtifactEvent = useCallback(
    (event: ArtifactEvent) => {
      if (event.event === "render_error") {
        const message = typeof (event.data as { message?: unknown })?.message === "string"
          ? (event.data as { message: string }).message
          : JSON.stringify(event.data);
        const signature = `${event.artifactId}|${message}`;
        if (seenRenderErrorsRef.current.has(signature)) return;
        seenRenderErrorsRef.current.add(signature);
      }
      if (isStreamingRef.current) {
        queuedArtifactEventsRef.current = [...queuedArtifactEventsRef.current, event];
        return;
      }
      runStream({ kind: "artifact", event });
    },
    [runStream]
  );

  // Settle a stuck prompt card and tell the user when their decision/answer could
  // not be delivered — a dropped connection, or a request that already expired
  // because the turn moved on. Without this the card stays at "input required" and
  // the composer stays gated, with no hint that the click was lost.
  const notifyResolveFailure = useCallback((requestId: string, kind: "decision" | "answer", status: string) => {
    stateRef.current.messages = stateRef.current.messages.map((message) => {
      const permission = message.meta?.permission as { requestId?: string } | undefined;
      const question = message.meta?.question as { requestId?: string } | undefined;
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
      const permission = message.meta?.permission as { requestId?: string } | undefined;
      const question = message.meta?.question as { requestId?: string } | undefined;
      if (message.role !== "tool_call" || (permission?.requestId !== requestId && question?.requestId !== requestId)) return message;
      return { ...message, meta: { ...message.meta, status: "completed" } };
    });
    flush();
  }, [flush]);

  const handlePermission = useCallback(
    async (requestId: string, decision: PermissionDecision) => {
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
        const permission = message.meta?.permission as { requestId?: string } | undefined;
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
        const question = message.meta?.question as { requestId?: string } | undefined;
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
        const question = message.meta?.question as { requestId?: string } | undefined;
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
      abortControllerRef.current?.abort();
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
      const permission = message.meta?.permission as { requestId?: string } | undefined;
      const question = message.meta?.question as { requestId?: string } | undefined;
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
    return abortSession(ctx).then((ok) => {
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
    isStreaming: isStreaming || sessionRunning,
    isHistoryLoading,
    historyError,
    hasOlderHistory,
    isOlderHistoryLoading,
    reloadHistory,
    send,
    sendArtifactEvent,
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
