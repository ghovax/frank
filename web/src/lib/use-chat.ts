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
  role: "user" | "assistant" | "tool_call" | "thinking" | "error" | "permission";
  content: string;
  timestamp: string;
  meta?: Record<string, unknown>;
}

// An agent step's ordered timeline — prose and tool calls interleaved, mirroring
// the main chat. Built from the A2A sub-task DataPart envelopes.
export type AgentPart =
  | { kind: "text"; content: string }
  | { kind: "tool"; name: string; arguments?: Record<string, unknown>; sequenceNumber: number; toolCallId?: string; result?: unknown };

export interface AgentStep {
  stepId: string;
  agent: string;
  goal: string;
  childTaskId: string;
  parts: AgentPart[];
  thinking: string;
  focus: string;
  icon: string;
  state: TaskState;
  task?: A2ATask;
}

export interface AgentGroup {
  groupId: string;
  toolCallId: string;
  justification: string;
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
  return { stepId, agent, goal, childTaskId, parts: [], thinking: "", focus: "", icon: "", state: "working" };
}

function appendAgentText(step: AgentStep, text: string): AgentStep {
  if (!text) return step;
  const last = step.parts[step.parts.length - 1];
  if (last && last.kind === "text") {
    const parts = step.parts.slice(0, -1);
    parts.push({ kind: "text", content: last.content + text });
    return { ...step, parts };
  }
  return { ...step, parts: [...step.parts, { kind: "text", content: text }] };
}

function appendAgentToolCall(step: AgentStep, name: string, toolArguments: Record<string, unknown> | undefined, toolCallId: string): AgentStep {
  const toolCount = step.parts.filter((part) => part.kind === "tool").length;
  return {
    ...step,
    parts: [...step.parts, { kind: "tool", name, arguments: toolArguments, sequenceNumber: toolCount + 1, toolCallId }],
  };
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
  const next = !hasText && artifactText ? appendAgentText(step, artifactText) : step;
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
  toolSequence: number;
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
        justification: String(data.justification ?? ""),
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

function setRunningThinking(state: ReduceState, focus: string, icon: string): void {
  const index = state.messages.findLastIndex(isRunningThinkingMessage);
  if (index !== -1) {
    state.messages = state.messages.map((message, messageIndex) =>
      messageIndex === index ? { ...message, meta: { ...message.meta, focus, icon } } : message
    );
    return;
  }
  state.messages = [
    ...state.messages,
    {
      id: `status-${state.messages.length}-${Math.random()}`,
      role: "thinking",
      content: "",
      timestamp: new Date().toISOString(),
      meta: { status: "running", focus, icon },
    },
  ];
}

function setThinkingFocus(state: ReduceState, focus: string, icon: string): void {
  // A focus-set starts a new thinking step. Finalize whatever is currently
  // running so it persists in the timeline — an entry that was already shown
  // (e.g. the generic "Thinking" placeholder) must never be replaced or removed.
  // If the running step already shows this exact focus, leave it as-is.
  const lastIndex = state.messages.findLastIndex(isRunningThinkingMessage);
  const last = lastIndex !== -1 ? state.messages[lastIndex] : undefined;
  if (last && last.meta?.focus === focus && last.meta?.icon === icon) return;
  finishRunningThinking(state);
  state.messages = [
    ...state.messages,
    {
      id: `status-${state.messages.length}-${Math.random()}`,
      role: "thinking",
      content: "",
      timestamp: new Date().toISOString(),
      meta: { status: "running", focus, icon },
    },
  ];
}

function hasAssistantTextAfterLastUser(state: ReduceState): boolean {
  const lastUserIndex = state.messages.findLastIndex((message) => message.role === "user");
  return state.messages
    .slice(lastUserIndex + 1)
    .some((message) => message.role === "assistant" && message.content.trim());
}

function pushAssistantText(state: ReduceState, text: string): void {
  if (!text) return;
  finishRunningThinking(state);
  if (state.lane === null) {
    const id = `assistant-${state.messages.length}-${Math.random()}`;
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
    { id: `user-${state.messages.length}-${Math.random()}`, role: "user", content: text, timestamp: new Date().toISOString() },
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
      const code = String(data.code ?? "");
      if (code === "thinking") {
        setRunningThinking(state, String(data.label ?? "Thinking"), String(data.icon ?? ""));
      } else if (code === "waiting_for_tools") {
        // Implementation detail: the model has finished thinking and is paused
        // on tool execution. Tool calls surface their own running/done status,
        // so just close out any in-flight thinking indicator without leaving a
        // "Waiting for tools..." placeholder behind.
        finishRunningThinking(state);
      }
      break;
    }
    case "thinking": {
      const label = data.label ? String(data.label) : "";
      const icon = String(data.icon ?? "");
      const text = String(data.text ?? "");
      if (label) setThinkingFocus(state, label, icon || "focus");
      if (text) {
        finishRunningThinking(state);
        const index = state.messages.findLastIndex((message) => message.role === "thinking");
        if (index !== -1) {
          state.messages = state.messages.map((message, messageIndex) => (messageIndex === index ? { ...message, content: message.content + text } : message));
        } else {
          state.messages = [...state.messages, { id: `thinking-${state.messages.length}-${Math.random()}`, role: "thinking", content: text, timestamp: new Date().toISOString(), meta: { focus: label, icon: icon || "focus" } }];
        }
      }
      break;
    }
    case "tool_call": {
      finishRunningThinking(state);
      state.lane = null;
      const sequence = ++state.toolSequence;
      state.messages = [
        ...state.messages,
        {
          id: `toolcall-${data.toolCallId ?? sequence}-${Math.random()}`,
          role: "tool_call",
          content: String(data.name ?? "unknown"),
          timestamp: new Date().toISOString(),
          meta: { arguments: data.arguments, toolCallId: String(data.toolCallId ?? ""), sequenceNumber: sequence, status: "running" },
        },
      ];
      break;
    }
    case "tool_result": {
      finishRunningThinking(state);
      state.lane = null;
      const toolCallId = String(data.toolCallId ?? "");
      const currentMessage = state.messages.find((message) => message.role === "tool_call" && message.meta?.toolCallId === toolCallId);
      const mergedResult = data.name === "call_mcp_tool" ? mergeMcpFinalResult(currentMessage?.meta?.result, data.result) : data.result;
      const artifactUpdate = applyArtifactUpdates(state.messages, mergedResult, toolCallId);
      state.messages = artifactUpdate.messages.map((message) =>
        message.role === "tool_call" && message.meta?.toolCallId === toolCallId
          ? { ...message, meta: { ...message.meta, status: "completed", result: artifactUpdate.result } }
          : message
      );
      break;
    }
    case "mcp_event": {
      const toolCallId = String(data.toolCallId ?? "");
      const streamed = streamedMcpResult(data);
      const currentMessage = state.messages.find((message) => message.role === "tool_call" && message.meta?.toolCallId === toolCallId);
      const mergedResult = mergeMcpResult(currentMessage?.meta?.result, streamed);
      const artifactUpdate = applyArtifactUpdates(state.messages, mergedResult, toolCallId);
      state.messages = artifactUpdate.messages.map((message) =>
        message.role === "tool_call" && message.meta?.toolCallId === toolCallId
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
      state.messages = [
        ...state.messages,
        { id: `error-${state.messages.length}-${Math.random()}`, role: "error", content: String(data.message ?? "Unknown error"), timestamp: new Date().toISOString() },
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
      state.agentGroups = withStep(state.agentGroups, String(data.groupId ?? ""), String(data.stepId ?? ""), (step) => ({
        ...step,
        thinking: step.thinking + String(data.text ?? ""),
        focus: data.label ? String(data.label) : step.focus,
        icon: data.icon ? String(data.icon) : step.icon,
      }));
      break;
    case "sub_task_status":
      if (data.label) {
        state.agentGroups = withStep(state.agentGroups, String(data.groupId ?? ""), String(data.stepId ?? ""), (step) => ({
          ...step,
          focus: String(data.label),
          icon: data.icon ? String(data.icon) : step.icon,
        }));
      }
      break;
    case "sub_task_tool_call":
      state.agentGroups = withStep(state.agentGroups, String(data.groupId ?? ""), String(data.stepId ?? ""), (step) => appendAgentToolCall(step, String(data.name ?? "unknown"), data.arguments as Record<string, unknown> | undefined, String(data.toolCallId ?? "")));
      break;
    case "sub_task_tool_result": {
      const toolCallId = String(data.toolCallId ?? "");
      let currentPartResult: unknown;
      for (const group of state.agentGroups) {
        for (const step of group.steps) {
          const part = step.parts.find((candidate) => candidate.kind === "tool" && candidate.toolCallId === toolCallId);
          if (part?.kind === "tool") currentPartResult = part.result;
        }
      }
      const mergedResult = data.name === "call_mcp_tool" ? mergeMcpFinalResult(currentPartResult, data.result) : data.result;
      const artifactUpdate = applyArtifactUpdatesToAgentGroups(state.agentGroups, mergedResult, toolCallId);
      state.agentGroups = withStep(artifactUpdate.agentGroups, String(data.groupId ?? ""), String(data.stepId ?? ""), (step) => ({
        ...step,
        parts: step.parts.map((part) =>
          part.kind === "tool" && toolCallId && part.toolCallId === toolCallId
            ? { ...part, result: artifactUpdate.result }
            : part
        ),
      }));
      break;
    }
    case "sub_task_mcp_event": {
      const toolCallId = String(data.toolCallId ?? "");
      const streamed = streamedMcpResult(data);
      let currentPartResult: unknown;
      for (const group of state.agentGroups) {
        for (const step of group.steps) {
          const part = step.parts.find((candidate) => candidate.kind === "tool" && candidate.toolCallId === toolCallId);
          if (part?.kind === "tool") currentPartResult = part.result;
        }
      }
      const mergedResult = mergeMcpResult(currentPartResult, streamed);
      const artifactUpdate = applyArtifactUpdatesToAgentGroups(state.agentGroups, mergedResult, toolCallId);
      state.agentGroups = withStep(artifactUpdate.agentGroups, String(data.groupId ?? ""), String(data.stepId ?? ""), (step) => ({
        ...step,
        parts: step.parts.map((part) =>
          part.kind === "tool" && toolCallId && part.toolCallId === toolCallId
            ? { ...part, result: artifactUpdate.result }
            : part
        ),
      }));
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
  }
}

// Reconstruct messages + agentGroups from a session's persisted A2A tasks.
function replayTasks(tasks: A2ATask[]): { messages: ChatMessage[]; agentGroups: AgentGroup[] } {
  const mainTasks = tasks
    .filter((task) => !(task.metadata && Array.isArray((task.metadata as Record<string, unknown>).referenceTaskIds)))
    .sort((first, second) => String(first.status?.timestamp ?? "").localeCompare(String(second.status?.timestamp ?? "")));
  const state: ReduceState = { messages: [], agentGroups: [], lane: null, toolSequence: 0 };
  for (const task of mainTasks) {
    state.lane = null;
    for (const message of task.history ?? []) {
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
  permissionMode: PermissionMode = "default"
) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [agentGroups, setAgentGroups] = useState<AgentGroup[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(initialSessionId);
  const [isStreaming, setIsStreaming] = useState(false);
  const [isHistoryLoading, setIsHistoryLoading] = useState(!!initialSessionId);
  const [queuedMessages, setQueuedMessages] = useState<string[]>([]);

  const abortControllerRef = useRef<AbortController | null>(null);
  const stateRef = useRef<ReduceState>({ messages: [], agentGroups: [], lane: null, toolSequence: 0 });
  const sessionIdRef = useRef<string | null>(initialSessionId);
  const historyLoadedForRef = useRef<string | null>(null);
  const queuedMessagesRef = useRef<string[]>([]);
  const isStreamingRef = useRef(false);
  const runStreamRef = useRef<(text: string) => void>(() => {});

  const setQueue = useCallback((next: string[]) => {
    queuedMessagesRef.current = next;
    setQueuedMessages(next);
  }, []);

  const flush = useCallback(() => {
    setMessages(stateRef.current.messages);
    setAgentGroups(stateRef.current.agentGroups);
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
        stateRef.current = { messages: replayed.messages, agentGroups: replayed.agentGroups, lane: null, toolSequence: 0 };
        setSessionId(initialSessionId);
        sessionIdRef.current = initialSessionId;
        flush();
      })
      .catch(() => {})
      .finally(() => {
        if (!cancelled) setIsHistoryLoading(false);
      });
    return () => {
      cancelled = true;
      historyLoadedForRef.current = null;
    };
  }, [initialSessionId, flush]);

  const runStream = useCallback(
    (text: string) => {
      // Optimistic user message + reset the open prose lane.
      stateRef.current.lane = null;
      stateRef.current.toolSequence = 0;
      stateRef.current.messages = [
        ...stateRef.current.messages,
        { id: `user-${Date.now()}`, role: "user", content: text, timestamp: new Date().toISOString() },
      ];
      flush();

      isStreamingRef.current = true;
      setIsStreaming(true);

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
          const pending = queuedMessagesRef.current;
          if (pending.length > 0) {
            const [next, ...rest] = pending;
            setQueue(rest);
            runStreamRef.current(next);
          } else {
            isStreamingRef.current = false;
            setIsStreaming(false);
          }
        },
        workingDirectory,
        permissionMode
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
      runStream(trimmed);
    },
    [runStream, setQueue]
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
    stateRef.current = { messages: [], agentGroups: [], lane: null, toolSequence: 0 };
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
    abort,
    reset,
    dequeueMessage,
    handlePermission,
  };
}
