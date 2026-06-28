"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  streamA2A,
  abortSession,
  resolvePermission,
  fetchSessionTasks,
  type A2AStreamResult,
  type A2AMessage,
  type A2APart,
  type A2ATask as A2ATaskWire,
  type PermissionMode,
} from "./api";
import { isControlToolName, isSameToolEvent, nextToolSequence, type ToolEvent, type ToolEventStatus } from "./tool-event";
import type { WidgetEvent } from "@/components/widget-bridge";

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

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "tool_call" | "thinking" | "error" | "permission" | "widget_event";
  content: string;
  timestamp: string;
  meta?: Record<string, unknown>;
}

// A turn's input: either typed text or a structured widget interaction. Both
// drive the same stream; a widget event travels as a typed DataPart, never as
// prose, so the agent receives it as structured JSON.
export type ChatInput =
  | { kind: "text"; text: string }
  | { kind: "widget"; event: WidgetEvent };

// An agent step's ordered timeline — prose, reasoning, and tool calls
// interleaved, mirroring the main chat. Built from the A2A sub-task DataPart
// envelopes. A `thinking` part renders through the same ThinkingIndicator the
// main chat uses, so a sub-agent's reasoning reads identically.
export type AgentPart =
  | { kind: "text"; content: string }
  | { kind: "thinking"; content: string; status: ToolEventStatus }
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

// The 1-based position of the next tool card within a step's timeline.
function nextAgentToolSequence(step: AgentStep): number {
  return nextToolSequence(step.parts.filter(isToolPart));
}

// Close out any in-flight thinking part (mark it done). Called before a new
// prose block, tool call, or the step finishing. Parts persist — a finished
// "Thinking" stays as the marker for that reasoning phase.
function finishAgentThinking(step: AgentStep): AgentStep {
  if (!step.parts.some(isRunningThinking)) return step;
  return { ...step, parts: step.parts.map((part) => (isRunningThinking(part) ? { ...part, status: "done" } : part)) };
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
    parts: [...step.parts, { kind: "tool", name, arguments: toolArguments, sequenceNumber: nextAgentToolSequence(step), toolCallId, status: "running" }],
  };
}

function upsertAgentToolResult(step: AgentStep, name: string, toolCallId: string, result: unknown): AgentStep {
  const status = asRecord(result).code === "tool_error" ? "failed" as const : "completed" as const;
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
    parts: [...step.parts, { kind: "tool", name: name || "unknown", sequenceNumber: nextAgentToolSequence(step), toolCallId, result, status }],
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

    if (!didReplace && mode === "upsert") {
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
  lane: string | null; // id of the open assistant prose block, if any
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
    sequenceNumber: message.meta?.sequenceNumber as number | undefined,
    toolCallId: String(message.meta?.toolCallId ?? ""),
    result: message.meta?.result,
    status: status === "running" || status === "completed" || status === "done" || status === "failed" ? status : undefined,
  };
}

function messageMatchesToolEvent(message: ChatMessage, name: string, toolCallId: string): boolean {
  const event = toolEventFromMessage(message);
  return event ? isSameToolEvent(event, name, toolCallId) : false;
}

function messageToolEvents(messages: ChatMessage[]): ToolEvent[] {
  return messages.flatMap((message) => {
    const event = toolEventFromMessage(message);
    return event ? [event] : [];
  });
}

function pushAssistantText(state: ReduceState, text: string): void {
  if (!text) return;
  finishRunningThinking(state);
  if (state.lane === null) {
    const id = `assistant-${state.messages.length}`;
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
    { id: `user-${state.messages.length}`, role: "user", content: text, timestamp: new Date().toISOString() },
  ];
}

function reduceAgentMessage(state: ReduceState, message: A2AMessage): void {
  for (const part of message.parts ?? []) {
    if (part.kind === "text") {
      pushAssistantText(state, part.text ?? "");
      continue;
    }
    if (part.kind !== "data" || !part.data) continue;
    reduceDataPart(state, part.data);
  }
}

function reduceDataPart(state: ReduceState, data: Record<string, unknown>): void {
  const kind = String(data.kind ?? "");
  switch (kind) {
    case "status": {
      // The model has finished thinking and is paused on tool execution. Tool
      // calls surface their own running/done status, so just close out any
      // in-flight thinking indicator. (The thinking ping itself is a `thinking`
      // event now, not a status — this only handles the wait edge.)
      if (String(data.code ?? "") === "waiting_for_tools") finishRunningThinking(state);
      break;
    }
    case "thinking":
      applyThinking(state, String(data.text ?? ""));
      break;
    case "tool_call": {
      if (isControlToolName(data.name)) break;
      finishRunningThinking(state);
      state.lane = null;
      const sequence = nextToolSequence(messageToolEvents(state.messages));
      state.messages = [
        ...state.messages,
        {
          id: `toolcall-${data.toolCallId ?? sequence}`,
          role: "tool_call",
          content: String(data.name ?? "unknown"),
          timestamp: new Date().toISOString(),
          meta: { arguments: data.arguments, toolCallId: String(data.toolCallId ?? ""), sequenceNumber: sequence, status: "running" },
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
      let matched = false;
      state.messages = artifactUpdate.messages.map((message) =>
        messageMatchesToolEvent(message, toolName, toolCallId)
          ? (matched = true, { ...message, meta: { ...message.meta, status: "completed", result: artifactUpdate.result } })
          : message
      );
      if (!matched) {
        const sequence = nextToolSequence(messageToolEvents(state.messages));
        state.messages = [
          ...state.messages,
          {
            id: `toolcall-${toolCallId || crypto.randomUUID()}`,
            role: "tool_call",
            content: toolName || "unknown",
            timestamp: new Date().toISOString(),
            meta: { toolCallId, sequenceNumber: sequence, status: "completed", result: artifactUpdate.result },
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
      finishRunningThinking(state);
      state.messages = [
        ...state.messages,
        {
          id: `perm-${data.requestId}`,
          role: "permission",
          content: String(data.command ?? ""),
          timestamp: new Date().toISOString(),
          meta: { request_id: String(data.requestId ?? ""), justification: data.justification, risk: data.risk },
        },
      ];
      break;
    }
    case "error": {
      finishRunningThinking(state);
      const toolName = String(data.name ?? data.tool ?? "");
      const toolCallId = String(data.toolCallId ?? "");
      if (toolCallId) {
        const result = { code: "tool_error", message: String(data.message ?? "Unknown error") };
        let matched = false;
        state.messages = state.messages.map((message) =>
          messageMatchesToolEvent(message, toolName, toolCallId)
            ? (matched = true, { ...message, meta: { ...message.meta, status: "failed", result } })
            : message
        );
        if (matched) break;
      }
      state.messages = [
        ...state.messages,
        { id: `error-${state.messages.length}`, role: "error", content: String(data.message ?? "Unknown error"), timestamp: new Date().toISOString() },
      ];
      break;
    }
    case "agent_group_started":
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

function textOnlyAgentMessage(message: A2AMessage): string | null {
  if (message.role !== "agent") return null;
  const parts = message.parts ?? [];
  if (parts.length === 0 || parts.some((part) => part.kind !== "text")) return null;
  return parts.map((part) => part.text ?? "").join("");
}

function subTaskTextMessage(message: A2AMessage): { key: string; data: Record<string, unknown>; text: string } | null {
  if (message.role !== "agent") return null;
  const parts = message.parts ?? [];
  if (parts.length !== 1 || parts[0]?.kind !== "data" || !parts[0].data) return null;
  const data = parts[0].data;
  if (data.kind !== "sub_task_text") return null;
  const key = [
    String(data.groupId ?? ""),
    String(data.stepId ?? ""),
    String(data.childTaskId ?? ""),
  ].join("\u0000");
  return { key, data, text: String(data.text ?? "") };
}

function compactReplayMessages(messages: A2AMessage[] | undefined): A2AMessage[] {
  const compacted: A2AMessage[] = [];
  for (const message of messages ?? []) {
    const text = textOnlyAgentMessage(message);
    if (text !== null) {
      const last = compacted[compacted.length - 1];
      const previousText = last ? textOnlyAgentMessage(last) : null;
      if (last && previousText !== null) {
        last.parts = [{ kind: "text", text: previousText + text }];
      } else {
        compacted.push({ ...message, parts: [{ kind: "text", text }] });
      }
      continue;
    }

    const subText = subTaskTextMessage(message);
    if (subText) {
      const last = compacted[compacted.length - 1];
      const previousSubText = last ? subTaskTextMessage(last) : null;
      if (last && previousSubText && previousSubText.key === subText.key) {
        last.parts = [{
          kind: "data",
          data: { ...previousSubText.data, text: previousSubText.text + subText.text },
        }];
      } else {
        compacted.push({
          ...message,
          parts: [{ kind: "data", data: { ...subText.data, text: subText.text } }],
        });
      }
      continue;
    }

    compacted.push(message);
  }
  return compacted;
}

// Reconstruct messages + agentGroups from a session's persisted A2A tasks.
function replayTasks(tasks: A2ATask[]): { messages: ChatMessage[]; agentGroups: AgentGroup[] } {
  const mainTasks = tasks
    .filter((task) => !(task.metadata && Array.isArray((task.metadata as Record<string, unknown>).referenceTaskIds)))
    .sort((first, second) => String(first.status?.timestamp ?? "").localeCompare(String(second.status?.timestamp ?? "")));
  const state: ReduceState = { messages: [], agentGroups: [], lane: null };
  for (const task of mainTasks) {
    state.lane = null;
    for (const message of compactReplayMessages(task.history)) {
      if (message.role === "user") reduceUserMessage(state, message);
      else reduceAgentMessage(state, message);
    }
  }
  return { messages: state.messages, agentGroups: state.agentGroups };
}

export function useChat(
  agent: string,
  initialSessionId: string | null = null,
  workingDirectory?: string,
  permissionMode: PermissionMode = "default",
  // Whether a turn is currently running on this session (from the server-tracked
  // running set). Drives live polling when we are viewing — but not driving — it.
  sessionRunning: boolean = false
) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [agentGroups, setAgentGroups] = useState<AgentGroup[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(initialSessionId);
  const [isStreaming, setIsStreaming] = useState(false);
  const [isHistoryLoading, setIsHistoryLoading] = useState(!!initialSessionId);
  const [queuedMessages, setQueuedMessages] = useState<string[]>([]);

  const abortControllerRef = useRef<AbortController | null>(null);
  const stateRef = useRef<ReduceState>({ messages: [], agentGroups: [], lane: null });
  const sessionIdRef = useRef<string | null>(initialSessionId);
  const historyLoadedForRef = useRef<string | null>(null);
  const queuedMessagesRef = useRef<string[]>([]);
  // Widget interactions that arrived mid-turn wait here and drain after the
  // current stream finishes. Kept separate from the user-visible text queue.
  const queuedWidgetEventsRef = useRef<WidgetEvent[]>([]);
  // Render errors fire automatically when a widget mounts, so a broken artifact
  // (including on session replay) must not spawn the same turn twice. Tracked by
  // artifact + message; user-driven events (clicks) are never deduped.
  const seenRenderErrorsRef = useRef<Set<string>>(new Set());
  const isStreamingRef = useRef(false);
  // Tracks whether this session was running, so we do a final refresh when its
  // turn finishes (the poll loop stops once it is no longer running).
  const wasRunningRef = useRef(false);
  // True once we have driven a turn in this mount. For such a session the live
  // SSE is authoritative, so we never poll it (polling would replace the live
  // state with a replay and churn message ids).
  const streamedLocallyRef = useRef(false);
  const runStreamRef = useRef<(input: ChatInput) => void>(() => {});
  const flushFrameRef = useRef<number | null>(null);

  const setQueue = useCallback((next: string[]) => {
    queuedMessagesRef.current = next;
    setQueuedMessages(next);
  }, []);

  const flushNow = useCallback(() => {
    if (flushFrameRef.current != null) {
      window.cancelAnimationFrame(flushFrameRef.current);
      flushFrameRef.current = null;
    }
    setMessages(stateRef.current.messages);
    setAgentGroups(stateRef.current.agentGroups);
  }, []);

  const flush = useCallback(() => {
    if (typeof window === "undefined") {
      setMessages(stateRef.current.messages);
      setAgentGroups(stateRef.current.agentGroups);
      return;
    }
    if (flushFrameRef.current != null) return;
    flushFrameRef.current = window.requestAnimationFrame(() => {
      flushFrameRef.current = null;
      setMessages(stateRef.current.messages);
      setAgentGroups(stateRef.current.agentGroups);
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
    historyLoadedForRef.current = initialSessionId;
    let cancelled = false;
    fetchSessionTasks(initialSessionId)
      .then((tasks) => {
        if (cancelled) return;
        const replayed = replayTasks(tasks);
        stateRef.current = { messages: replayed.messages, agentGroups: replayed.agentGroups, lane: null };
        setSessionId(initialSessionId);
        sessionIdRef.current = initialSessionId;
        flushNow();
      })
      .catch(() => {})
      .finally(() => {
        if (!cancelled) setIsHistoryLoading(false);
      });
    return () => {
      cancelled = true;
      historyLoadedForRef.current = null;
    };
  }, [initialSessionId, flushNow]);

  // Live updates for a session that is running on the server but is not being
  // driven by this hook (we switched back to it, or it was started elsewhere).
  // The backend persists each step to the task store as the turn progresses, so
  // polling + re-replay keeps the transcript current in real time. Re-replay is a
  // smooth in-place update because message ids are deterministic. We never poll
  // while streaming locally — the live SSE already delivers those updates.
  useEffect(() => {
    if (!initialSessionId) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const refresh = async () => {
      if (cancelled || isStreamingRef.current || streamedLocallyRef.current) return;
      try {
        const tasks = await fetchSessionTasks(initialSessionId);
        if (cancelled || isStreamingRef.current || streamedLocallyRef.current) return;
        const replayed = replayTasks(tasks);
        stateRef.current = { messages: replayed.messages, agentGroups: replayed.agentGroups, lane: null };
        flushNow();
      } catch {
        // transient — try again on the next tick
      }
    };
    if (sessionRunning) {
      wasRunningRef.current = true;
      const tick = async () => {
        await refresh();
        if (!cancelled) timer = setTimeout(tick, 1000);
      };
      timer = setTimeout(tick, 1000);
    } else if (wasRunningRef.current) {
      // The turn just finished — capture its final state once, then stop.
      wasRunningRef.current = false;
      refresh();
    }
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
  }, [sessionRunning, initialSessionId, flushNow]);

  const runStream = useCallback(
    (input: ChatInput) => {
      // Optimistic input message + reset the open prose lane. A widget event
      // renders as its own structured entry rather than a user prose bubble.
      stateRef.current.lane = null;
      const optimistic: ChatMessage =
        input.kind === "text"
          ? { id: `user-${Date.now()}`, role: "user", content: input.text, timestamp: new Date().toISOString() }
          : {
              id: `widget-${Date.now()}`,
              role: "widget_event",
              content: input.event.event,
              timestamp: new Date().toISOString(),
              meta: { widgetEvent: input.event },
            };
      stateRef.current.messages = [...stateRef.current.messages, optimistic];
      flush();

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
            if (update.contextId && !sessionIdRef.current) {
              sessionIdRef.current = update.contextId;
              setSessionId(update.contextId);
            }
            if (update.status?.message) {
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
            reduceAgentMessage(stateRef.current, result as unknown as A2AMessage);
            flush();
          }
        },
        () => {
          stateRef.current.lane = null;
          // Drain queued text first (user intent), then any widget events that
          // arrived mid-turn.
          const pendingText = queuedMessagesRef.current;
          const pendingWidget = queuedWidgetEventsRef.current;
          if (pendingText.length > 0) {
            const [next, ...rest] = pendingText;
            setQueue(rest);
            runStreamRef.current({ kind: "text", text: next });
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
        permissionMode,
        dataPart
      );
    },
    [agent, workingDirectory, permissionMode, flush, setQueue]
  );

  useEffect(() => {
    runStreamRef.current = runStream;
  }, [runStream]);

  const send = useCallback(
    (text: string) => {
      const trimmed = text.trim();
      if (!trimmed) return;
      if (isStreamingRef.current) {
        setQueue([...queuedMessagesRef.current, trimmed]);
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
    async (requestId: string, decision: "allow" | "deny") => {
      const ctx = sessionIdRef.current;
      if (!ctx) return;
      await resolvePermission(ctx, requestId, decision);
      stateRef.current.messages = stateRef.current.messages.map((message) =>
        message.id === `perm-${requestId}` ? { ...message, meta: { ...message.meta, resolved: decision } } : message
      );
      flush();
    },
    [flush]
  );

  const abort = useCallback(() => {
    abortControllerRef.current?.abort();
    const ctx = sessionIdRef.current;
    if (ctx) abortSession(ctx);
  }, []);

  const dequeueMessage = useCallback((index: number) => {
    setQueue(queuedMessagesRef.current.filter((_, i) => i !== index));
  }, [setQueue]);

  const reset = useCallback(() => {
    abort();
    stateRef.current = { messages: [], agentGroups: [], lane: null };
    setMessages([]);
    setAgentGroups([]);
    setSessionId(null);
    sessionIdRef.current = null;
  }, [abort]);

  return {
    messages,
    agentGroups,
    queuedMessages,
    sessionId,
    isStreaming,
    isHistoryLoading,
    send,
    sendWidgetEvent,
    abort,
    reset,
    dequeueMessage,
    handlePermission,
  };
}
