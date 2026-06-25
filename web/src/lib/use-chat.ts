"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { flushSync } from "react-dom";
import {
  streamChat,
  resolvePermission,
  abortSession,
  fetchConversation,
  type StreamEvent,
} from "./api";

export interface ChatMessage {
  id: string;
  role:
    | "user"
    | "assistant"
    | "tool_call"
    | "tool_result"
    | "thinking"
    | "error"
    | "permission"
    | "status";
  content: string;
  timestamp: string;
  meta?: Record<string, unknown>;
}

export interface AgentToolCall {
  name: string;
  arguments?: Record<string, unknown>;
  sequenceNumber: number;
}

export interface AgentStep {
  stepId: string;
  agent: string;
  text: string;
  thinking: string;
  toolCalls: AgentToolCall[];
  done: boolean;
}

export interface Orchestration {
  orchestrationId: string;
  toolCallId: string;
  justification: string;
  steps: AgentStep[];
}

function emptyStep(stepId: string, agent = ""): AgentStep {
  return { stepId, agent, text: "", thinking: "", toolCalls: [], done: false };
}

// Immutable update of one step inside one orchestration. Creates the step (and,
// defensively, the orchestration) if a sub-agent event arrives before its
// declaration — though `orchestration_started` always precedes them.
function withStep(
  list: Orchestration[],
  orchestrationId: string,
  stepId: string,
  updater: (step: AgentStep) => AgentStep
): Orchestration[] {
  let found = false;
  const next = list.map((orchestration) => {
    if (orchestration.orchestrationId !== orchestrationId) return orchestration;
    found = true;
    const hasStep = orchestration.steps.some((step) => step.stepId === stepId);
    const steps = hasStep
      ? orchestration.steps.map((step) => (step.stepId === stepId ? updater(step) : step))
      : [...orchestration.steps, updater(emptyStep(stepId))];
    return { ...orchestration, steps };
  });
  if (!found) {
    next.push({
      orchestrationId,
      toolCallId: "",
      justification: "",
      steps: [updater(emptyStep(stepId))],
    });
  }
  return next;
}

function startOrchestration(list: Orchestration[], event: StreamEvent): Orchestration[] {
  const orchestrationId = String(event.orchestration_id ?? "");
  if (!orchestrationId || list.some((orchestration) => orchestration.orchestrationId === orchestrationId)) {
    return list;
  }
  const declaredSteps: AgentStep[] = Array.isArray(event.steps)
    ? event.steps.map((step: { id?: string; agent?: string }) => emptyStep(String(step.id ?? ""), String(step.agent ?? "")))
    : [];
  return [
    ...list,
    {
      orchestrationId,
      toolCallId: String(event.tool_call_id ?? ""),
      justification: String(event.justification ?? ""),
      steps: declaredSteps,
    },
  ];
}

interface ReplayResult {
  messages: ChatMessage[];
  orchestrations: Orchestration[];
}

function replayEvents(events: StreamEvent[]): ReplayResult {
  const messages: ChatMessage[] = [];
  // laneKey -> index in `messages` of that lane's open assistant block. Mirrors
  // the live streaming logic so reloaded history matches what was shown live.
  const lanes = new Map<string, number>();
  let toolSequence = 0;
  const toolIdToSequence = new Map<string, number>();
  const taskIdToSequence = new Map<string, number>();
  let orchestrations: Orchestration[] = [];
  const fallbackTimestamp = new Date().toISOString();

  for (const event of events) {
    switch (event.type) {
      case "text_chunk": {
        const text = event.text ?? "";
        const laneIndex = lanes.get("main");
        if (laneIndex === undefined) {
          lanes.set("main", messages.length);
          messages.push({
            id: `history-${messages.length}-${Math.random()}`,
            role: "assistant",
            content: text,
            timestamp: event.timestamp ?? fallbackTimestamp,
          });
        } else {
          messages[laneIndex] = {
            ...messages[laneIndex],
            content: messages[laneIndex].content + text,
          };
        }
        break;
      }

      case "orchestration_started":
        orchestrations = startOrchestration(orchestrations, event);
        for (const message of messages) {
          if (message.role === "tool_call" && message.meta?.toolCallId === event.tool_call_id) {
            message.meta = { ...message.meta, orchestrationId: event.orchestration_id };
          }
        }
        break;

      case "agent_text_chunk":
        orchestrations = withStep(
          orchestrations,
          String(event.orchestration_id ?? ""),
          String(event.step_id ?? ""),
          (step) => ({ ...step, text: step.text + (event.text ?? "") })
        );
        break;

      case "agent_thinking":
        orchestrations = withStep(
          orchestrations,
          String(event.orchestration_id ?? ""),
          String(event.step_id ?? ""),
          (step) => ({ ...step, thinking: step.thinking + (event.text ?? "") })
        );
        break;

      case "agent_tool_call":
        orchestrations = withStep(
          orchestrations,
          String(event.orchestration_id ?? ""),
          String(event.step_id ?? ""),
          (step) => ({
            ...step,
            toolCalls: [
              ...step.toolCalls,
              { name: event.name ?? "unknown", arguments: event.arguments, sequenceNumber: step.toolCalls.length + 1 },
            ],
          })
        );
        break;

      case "agent_done":
        orchestrations = withStep(
          orchestrations,
          String(event.orchestration_id ?? ""),
          String(event.step_id ?? ""),
          (step) => ({ ...step, done: true })
        );
        break;

      case "background_started": {
        const startedSequence = event.id ? toolIdToSequence.get(event.id) : undefined;
        if (startedSequence !== undefined && typeof event.task_id === "string") {
          taskIdToSequence.set(event.task_id, startedSequence);
        }
        break;
      }

      case "status":
        if (event.code === "thinking") {
          messages.push({
            id: `history-${messages.length}-${Math.random()}`,
            role: "thinking",
            content: "",
            timestamp: event.timestamp ?? fallbackTimestamp,
          });
        } else if (event.code === "waiting_for_tools") {
          const thinkingIndex = messages.findLastIndex(
            (message) => message.role === "thinking"
          );
          if (thinkingIndex !== -1) {
            messages[thinkingIndex] = {
              ...messages[thinkingIndex],
              content: "Waiting for tool results...",
              timestamp: event.timestamp ?? fallbackTimestamp,
            };
          } else {
            messages.push({
              id: `history-${messages.length}-${Math.random()}`,
              role: "thinking",
              content: "Waiting for tool results...",
              timestamp: event.timestamp ?? fallbackTimestamp,
            });
          }
        }
        break;

      case "thinking": {
        const text = event.text ?? "";
        const thinkingIndex = messages.findLastIndex(
          (message) => message.role === "thinking"
        );
        if (thinkingIndex !== -1) {
          messages[thinkingIndex] = {
            ...messages[thinkingIndex],
            content: text,
          };
        } else {
          messages.push({
            id: `history-${messages.length}-${Math.random()}`,
            role: "thinking",
            content: text,
            timestamp: event.timestamp ?? fallbackTimestamp,
          });
        }
        break;
      }

      case "tool_call": {
        lanes.delete("main");
        const toolCallSequence = ++toolSequence;
        if (event.id) toolIdToSequence.set(event.id, toolCallSequence);
        messages.push({
          id: `history-${messages.length}-${Math.random()}`,
          role: "tool_call",
          content: event.name ?? "unknown",
          timestamp: event.timestamp ?? fallbackTimestamp,
          meta: {
            arguments: event.arguments,
            toolCallId: event.id,
            sequenceNumber: toolCallSequence,
          },
        });
        break;
      }

      case "tool_result": {
        const resultData =
          typeof event.result === "string"
            ? (() => { try { return JSON.parse(event.result); } catch { return null; } })()
            : event.result;
        if (typeof resultData?.code === "string" && resultData.code.endsWith("_started")) {
          const startedSequence = toolIdToSequence.get(event.id);
          const taskId = resultData?.task_identifier;
          if (startedSequence !== undefined && typeof taskId === "string") {
            taskIdToSequence.set(taskId, startedSequence);
          }
          for (const message of messages) {
            if (message.role === "tool_call" && message.meta?.toolCallId === event.id) {
              message.meta = {
                ...message.meta,
                status: "running",
                task_id: taskId,
                output_file: resultData.output_file,
              };
            }
          }
          break;
        }
        lanes.delete("main");
        const resultSequence =
          toolIdToSequence.get(event.id) ??
          (event.task_id ? taskIdToSequence.get(event.task_id) : undefined) ??
          ++toolSequence;
        for (const message of messages) {
          if (message.role === "tool_call" && message.meta?.sequenceNumber === resultSequence) {
            message.meta = { ...message.meta, status: "completed" };
          }
        }
        messages.push({
          id: `history-${messages.length}-${Math.random()}`,
          role: "tool_result",
          content:
            typeof event.result === "string"
              ? event.result
              : JSON.stringify(event.result, null, 2),
          timestamp: event.timestamp ?? fallbackTimestamp,
          meta: { name: event.name, task_id: event.task_id, sequenceNumber: resultSequence },
        });
        break;
      }

      case "user":
        lanes.clear();
        messages.push({
          id: `history-${messages.length}-${Math.random()}`,
          role: "user",
          content: event.text ?? "",
          timestamp: event.timestamp ?? fallbackTimestamp,
        });
        break;

      case "permission_request":
        messages.push({
          id: `perm-${event.request_id}`,
          role: "permission",
          content: event.command ?? "",
          timestamp: event.timestamp ?? fallbackTimestamp,
          meta: {
            request_id: event.request_id,
            justification: event.justification,
            risk: event.risk,
          },
        });
        break;

      case "error":
        messages.push({
          id: `error-${messages.length}`,
          role: "error",
          content: event.message ?? "Unknown error",
          timestamp: event.timestamp ?? fallbackTimestamp,
        });
        break;
    }
  }

  return { messages, orchestrations };
}

export function useChat(agent: string, initialSessionId: string | null = null, workingDirectory?: string) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [orchestrations, setOrchestrations] = useState<Orchestration[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(initialSessionId);
  const [isStreaming, setIsStreaming] = useState(false);
  const [isHistoryLoading, setIsHistoryLoading] = useState(!!initialSessionId);
  const abortControllerRef = useRef<AbortController | null>(null);
  // One independent assistant text block per concurrent stream. The main
  // agent uses lane "main"; each orchestration step uses its step_id. Without
  // this, parallel sub-agents would all append into one block and interleave.
  const lanesRef = useRef<Map<string, { id: string; text: string }>>(new Map());
  const historyLoadedForRef = useRef<string | null>(null);
  const toolSequenceRef = useRef(0);
  const toolIdToSequenceRef = useRef<Map<string, number>>(new Map());
  // Background work (searches, sub-agents, slow bash) finishes in a later event
  // that carries only a task_id, not the original tool-call id. Map the task_id
  // back to the call's number so a "#N started" and "#N finished" share one N.
  const taskIdToSequenceRef = useRef<Map<string, number>>(new Map());

  // Messages typed while the agent is busy. They are sent automatically, one at
  // a time, as the current turn finishes — appended to the same session so the
  // server-side conversation (and its prompt cache) only grows, never resets.
  const [queuedMessages, setQueuedMessages] = useState<string[]>([]);
  const queuedMessagesRef = useRef<string[]>([]);
  const isStreamingRef = useRef(false);
  const runStreamRef = useRef<(text: string) => void>(() => {});

  const setQueue = useCallback((next: string[]) => {
    queuedMessagesRef.current = next;
    setQueuedMessages(next);
  }, []);

  useEffect(() => {
    if (!initialSessionId) return;
    if (historyLoadedForRef.current === initialSessionId) return;
    historyLoadedForRef.current = initialSessionId;
    let cancelled = false;

    fetchConversation(initialSessionId)
      .then((events) => {
        if (cancelled) return;
        setSessionId(initialSessionId);
        const replayed = replayEvents(events);
        setMessages(replayed.messages);
        setOrchestrations(replayed.orchestrations);
      })
      .catch(() => {
        // session may no longer exist on the server
      })
      .finally(() => {
        if (!cancelled) setIsHistoryLoading(false);
      });

    return () => { 
      cancelled = true; 
      historyLoadedForRef.current = null;
    };
  }, [initialSessionId]);

  const runStream = useCallback(
    (text: string) => {
      const userMessage: ChatMessage = {
        id: `user-${Date.now()}`,
        role: "user",
        content: text,
        timestamp: new Date().toISOString(),
      };

      lanesRef.current = new Map();
      toolSequenceRef.current = 0;
      toolIdToSequenceRef.current = new Map();
      taskIdToSequenceRef.current = new Map();

      setMessages((current) => [...current, userMessage]);

      isStreamingRef.current = true;
      setIsStreaming(true);

      abortControllerRef.current = streamChat(
        text,
        agent,
        sessionId,
        (event: StreamEvent) => {
          switch (event.type) {
            case "session":
              if (event.session_id) setSessionId(event.session_id);
              break;

            case "status":
              if (event.code === "thinking") {
                flushSync(() => {
                  setMessages((current) => [
                    ...current,
                    {
                      id: `thinking-${Date.now()}-${Math.random()}`,
                      role: "thinking" as const,
                      content: "",
                      timestamp: event.timestamp ?? new Date().toISOString(),
                    },
                  ]);
                });
              } else if (event.code === "waiting_for_tools") {
                setMessages((current) => {
                  const thinkingIndex = current.findLastIndex(
                    (message) => message.role === "thinking"
                  );
                  if (thinkingIndex !== -1) {
                    const updated = [...current];
                    updated[thinkingIndex] = {
                      ...current[thinkingIndex],
                      content: "Waiting for tool results...",
                      timestamp: event.timestamp ?? new Date().toISOString(),
                    };
                    return updated;
                  }
                  return [
                    ...current,
                    {
                      id: `thinking-${Date.now()}-${Math.random()}`,
                      role: "thinking" as const,
                      content: "Waiting for tool results...",
                      timestamp: event.timestamp ?? new Date().toISOString(),
                    },
                  ];
                });
              }
              break;

            case "text_chunk": {
              const chunkText = event.text ?? "";
              const lane = lanesRef.current.get("main");
              if (!lane) {
                const newId = `assistant-main-${Date.now()}-${Math.random()}`;
                lanesRef.current.set("main", { id: newId, text: chunkText });
                setMessages((current) => [
                  ...current,
                  {
                    id: newId,
                    role: "assistant" as const,
                    content: chunkText,
                    timestamp: new Date().toISOString(),
                  },
                ]);
              } else {
                lane.text += chunkText;
                const targetId = lane.id;
                const updatedText = lane.text;
                setMessages((current) =>
                  current.map((message) =>
                    message.id === targetId
                      ? { ...message, content: updatedText }
                      : message
                  )
                );
              }
              break;
            }

            case "orchestration_started": {
              setOrchestrations((current) => startOrchestration(current, event));
              // Link the originating orchestrate tool-call row to this group.
              setMessages((current) =>
                current.map((message) =>
                  message.role === "tool_call" && message.meta?.toolCallId === event.tool_call_id
                    ? { ...message, meta: { ...message.meta, orchestrationId: event.orchestration_id } }
                    : message
                )
              );
              break;
            }

            case "agent_text_chunk":
              setOrchestrations((current) =>
                withStep(current, String(event.orchestration_id ?? ""), String(event.step_id ?? ""), (step) => ({
                  ...step,
                  text: step.text + (event.text ?? ""),
                }))
              );
              break;

            case "thinking":
              setMessages((current) => {
                const thinkingIndex = current.findLastIndex(
                  (message) => message.role === "thinking"
                );
                if (thinkingIndex !== -1) {
                  const updated = [...current];
                  updated[thinkingIndex] = {
                    ...current[thinkingIndex],
                    content: event.text ?? "",
                    timestamp: event.timestamp ?? new Date().toISOString(),
                  };
                  return updated;
                }
                return [
                  ...current,
                  {
                    id: `thinking-${Date.now()}-${Math.random()}`,
                    role: "thinking",
                    content: event.text ?? "",
                    timestamp: event.timestamp ?? new Date().toISOString(),
                  },
                ];
              });
              break;

            case "agent_thinking":
              setOrchestrations((current) =>
                withStep(current, String(event.orchestration_id ?? ""), String(event.step_id ?? ""), (step) => ({
                  ...step,
                  thinking: step.thinking + (event.text ?? ""),
                }))
              );
              break;

            case "agent_tool_call":
              setOrchestrations((current) =>
                withStep(current, String(event.orchestration_id ?? ""), String(event.step_id ?? ""), (step) => ({
                  ...step,
                  toolCalls: [
                    ...step.toolCalls,
                    { name: event.name ?? "unknown", arguments: event.arguments, sequenceNumber: step.toolCalls.length + 1 },
                  ],
                }))
              );
              break;

            case "tool_call": {
              // Close the main lane so the agent's next text starts a fresh
              // block after the tool call, in order.
              lanesRef.current.delete("main");
              const sequence = ++toolSequenceRef.current;
              if (event.id) toolIdToSequenceRef.current.set(event.id, sequence);
              flushSync(() => {
                setMessages((current) => [
                  ...current,
                  {
                    id: `toolcall-${event.id ?? Date.now()}-${Math.random()}`,
                    role: "tool_call",
                    content: event.name ?? "unknown",
                    timestamp: event.timestamp ?? new Date().toISOString(),
                    meta: {
                      arguments: event.arguments,
                      toolCallId: event.id,
                      sequenceNumber: sequence,
                    },
                  },
                ]);
              });
              break;
            }

            case "tool_result": {
              const resultData =
                typeof event.result === "object" && event.result !== null
                  ? event.result
                  : typeof event.result === "string"
                    ? (() => { try { return JSON.parse(event.result); } catch { return null; } })()
                    : null;
              if (typeof resultData?.code === "string" && resultData.code.endsWith("_started")) {
                // Remember which number this background task belongs to so its
                // eventual completion reuses it instead of taking a new number.
                const startedSequence = toolIdToSequenceRef.current.get(event.id);
                const taskId = resultData?.task_identifier;
                if (startedSequence != null && typeof taskId === "string") {
                  taskIdToSequenceRef.current.set(taskId, startedSequence);
                }
                setMessages((current) =>
                  current.map((message) =>
                    message.role === "tool_call" && message.meta?.toolCallId === event.id
                      ? {
                          ...message,
                          meta: {
                            ...message.meta,
                            status: "running",
                            task_id: taskId,
                            output_file: resultData.output_file,
                          },
                        }
                      : message
                  )
                );
                break;
              }
              // A tool result is the main agent's; close its lane so the
              // follow-up text appears after the result.
              lanesRef.current.delete("main");
              const resultSequence =
                toolIdToSequenceRef.current.get(event.id) ??
                (event.task_id ? taskIdToSequenceRef.current.get(event.task_id) : undefined) ??
                ++toolSequenceRef.current;
              flushSync(() => {
                setMessages((current) => {
                  const updated = current.map((message) =>
                    message.role === "tool_call" && message.meta?.sequenceNumber === resultSequence
                      ? { ...message, meta: { ...message.meta, status: "completed" } }
                      : message
                  );
                  return [
                    ...updated,
                    {
                      id: `toolresult-${event.id ?? Date.now()}-${Math.random()}`,
                      role: "tool_result",
                      content:
                        typeof event.result === "string"
                          ? event.result
                          : JSON.stringify(event.result, null, 2),
                      timestamp: event.timestamp ?? new Date().toISOString(),
                      meta: { name: event.name, task_id: event.task_id, sequenceNumber: resultSequence },
                    },
                  ];
                });
              });
              break;
            }

            case "permission_request":
              flushSync(() => {
                setMessages((current) => [
                  ...current,
                  {
                    id: `perm-${event.request_id}`,
                    role: "permission",
                    content: event.command ?? "",
                    timestamp: event.timestamp ?? new Date().toISOString(),
                    meta: {
                      request_id: event.request_id,
                      justification: event.justification,
                      risk: event.risk,
                    },
                  },
                ]);
              });
              break;

            case "error":
              flushSync(() => {
                setMessages((current) => [
                  ...current,
                  {
                    id: `error-${Date.now()}`,
                    role: "error",
                    content: event.message ?? "Unknown error",
                    timestamp: event.timestamp ?? new Date().toISOString(),
                  },
                ]);
              });
              break;

            case "background_started": {
              // A sub-agent was launched; link its task to the spawning call's
              // number so its completion result reuses it.
              const startedSequence = event.id ? toolIdToSequenceRef.current.get(event.id) : undefined;
              if (startedSequence != null && typeof event.task_id === "string") {
                taskIdToSequenceRef.current.set(event.task_id, startedSequence);
              }
              break;
            }

            case "done": {
              // Fallback: if the turn produced final text but nothing was
              // streamed (e.g. progress streaming disabled), render it once.
              if (event.text && !lanesRef.current.get("main")) {
                setMessages((current) => [
                  ...current,
                  {
                    id: `assistant-${Date.now()}-${Math.random()}`,
                    role: "assistant" as const,
                    content: event.text,
                    timestamp: event.timestamp ?? new Date().toISOString(),
                  },
                ]);
              }
              lanesRef.current.delete("main");
              break;
            }

            case "agent_done":
              setOrchestrations((current) =>
                withStep(current, String(event.orchestration_id ?? ""), String(event.step_id ?? ""), (step) => ({
                  ...step,
                  done: true,
                }))
              );
              break;
          }
        },
        () => {
          lanesRef.current.clear();
          setMessages((current) =>
            current.filter(
              (message) =>
                !(message.role === "assistant" && !message.content)
            )
          );
          // Drain the next queued message into a fresh turn, or go idle.
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
      );
    },
    [agent, sessionId, workingDirectory, setQueue]
  );

  useEffect(() => {
    runStreamRef.current = runStream;
  }, [runStream]);

  const send = useCallback(
    (text: string) => {
      const trimmed = text.trim();
      if (!trimmed) return;
      // Busy → queue it for the next turn rather than dropping it.
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
      if (!sessionId) return;
      await resolvePermission(sessionId, requestId, decision);
      setMessages((current) =>
        current.map((message) =>
          message.id === `perm-${requestId}`
            ? { ...message, meta: { ...message.meta, resolved: decision } }
            : message
        )
      );
    },
    [sessionId]
  );

  const abort = useCallback(() => {
    abortControllerRef.current?.abort();
    if (sessionId) abortSession(sessionId);
  }, [sessionId]);

  const dequeueMessage = useCallback((index: number) => {
    setQueue(queuedMessagesRef.current.filter((_, i) => i !== index));
  }, [setQueue]);

  const reset = useCallback(() => {
    abort();
    setMessages([]);
    setOrchestrations([]);
    setSessionId(null);
  }, [abort]);

  return {
    messages,
    orchestrations,
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
