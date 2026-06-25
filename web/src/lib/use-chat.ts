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

function replayEvents(events: StreamEvent[]): ChatMessage[] {
  const messages: ChatMessage[] = [];
  let needsNewAssistant = false;
  let toolSequence = 0;
  const toolIdToSequence = new Map<string, number>();
  const fallbackTimestamp = new Date().toISOString();

  for (const event of events) {
    switch (event.type) {
      case "text_chunk":
      case "agent_text_chunk": {
        const text = event.text ?? "";
        if (needsNewAssistant) {
          messages.push({
            id: `history-${messages.length}-${Math.random()}`,
            role: "assistant",
            content: text,
            timestamp: event.timestamp ?? fallbackTimestamp,
          });
          needsNewAssistant = false;
        } else {
          const lastAssistantIndex = messages.findLastIndex(
            (message) => message.role === "assistant"
          );
          if (lastAssistantIndex !== -1) {
            messages[lastAssistantIndex] = {
              ...messages[lastAssistantIndex],
              content: messages[lastAssistantIndex].content + text,
            };
          } else {
            messages.push({
              id: `history-${messages.length}-${Math.random()}`,
              role: "assistant",
              content: text,
              timestamp: event.timestamp ?? fallbackTimestamp,
            });
          }
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
        }
        break;

      case "thinking":
      case "agent_thinking": {
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

      case "tool_call":
      case "agent_tool_call":
        needsNewAssistant = true;
        const toolCallSequence = ++toolSequence;
        if (event.id) toolIdToSequence.set(event.id, toolCallSequence);
        messages.push({
          id: `history-${messages.length}-${Math.random()}`,
          role: "tool_call",
          content: event.name ?? "unknown",
          timestamp: event.timestamp ?? fallbackTimestamp,
          meta: {
            arguments: event.arguments,
            step_id: event.step_id,
            sequenceNumber: toolCallSequence,
          },
        });
        break;

      case "tool_result": {
        const resultData =
          typeof event.result === "string"
            ? (() => { try { return JSON.parse(event.result); } catch { return null; } })()
            : event.result;
        if (typeof resultData?.code === "string" && resultData.code.endsWith("_started")) {
          break;
        }
        needsNewAssistant = true;
        const resultSequence = toolIdToSequence.get(event.id) ?? ++toolSequence;
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
        needsNewAssistant = true;
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

  return messages;
}

export function useChat(agent: string, initialSessionId: string | null = null, workingDirectory?: string) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(initialSessionId);
  const [isStreaming, setIsStreaming] = useState(false);
  const abortControllerRef = useRef<AbortController | null>(null);
  const assistantTextRef = useRef("");
  const assistantMessageIdRef = useRef("");
  const needsNewAssistantRef = useRef(false);
  const historyLoadedForRef = useRef<string | null>(null);
  const toolSequenceRef = useRef(0);
  const toolIdToSequenceRef = useRef<Map<string, number>>(new Map());

  useEffect(() => {
    if (!initialSessionId) return;
    if (historyLoadedForRef.current === initialSessionId) return;
    historyLoadedForRef.current = initialSessionId;
    let cancelled = false;

    fetchConversation(initialSessionId)
      .then((events) => {
        if (cancelled) return;
        setMessages((current) =>
          current.length === 0 ? replayEvents(events) : current
        );
      })
      .catch(() => {
        // session may no longer exist on the server
      });

    return () => { 
      cancelled = true; 
      historyLoadedForRef.current = null;
    };
  }, [initialSessionId]);

  const send = useCallback(
    (text: string) => {
      if (!text.trim() || isStreaming) return;

      const userMessage: ChatMessage = {
        id: `user-${Date.now()}`,
        role: "user",
        content: text,
        timestamp: new Date().toISOString(),
      };

      assistantTextRef.current = "";
      assistantMessageIdRef.current = "";
      needsNewAssistantRef.current = true;
      toolSequenceRef.current = 0;
      toolIdToSequenceRef.current = new Map();

      setMessages((current) => [...current, userMessage]);

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
              }
              break;

            case "text_chunk":
            case "agent_text_chunk": {
              const chunkText = event.text ?? "";
              if (needsNewAssistantRef.current) {
                const newId = `assistant-${Date.now()}-${Math.random()}`;
                assistantMessageIdRef.current = newId;
                assistantTextRef.current = chunkText;
                needsNewAssistantRef.current = false;
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
                assistantTextRef.current += chunkText;
                const targetId = assistantMessageIdRef.current;
                const updatedText = assistantTextRef.current;
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

            case "thinking":
            case "agent_thinking":
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

            case "tool_call":
            case "agent_tool_call": {
              needsNewAssistantRef.current = true;
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
                      step_id: event.step_id,
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
                break;
              }
              needsNewAssistantRef.current = true;
              const resultSequence = toolIdToSequenceRef.current.get(event.id) ?? ++toolSequenceRef.current;
              flushSync(() => {
                setMessages((current) => [
                  ...current,
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
                ]);
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

            case "done":
            case "agent_done":
              if (event.text && !assistantTextRef.current) {
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
              needsNewAssistantRef.current = false;
              break;
          }
        },
        () => {
          setIsStreaming(false);
          needsNewAssistantRef.current = false;
          setMessages((current) =>
            current.filter(
              (message) =>
                !(message.role === "assistant" && !message.content)
            )
          );
        },
        workingDirectory,
      );
    },
    [agent, sessionId, isStreaming, workingDirectory]
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
    setIsStreaming(false);
  }, [sessionId]);

  const reset = useCallback(() => {
    abort();
    setMessages([]);
    setSessionId(null);
  }, [abort]);

  return {
    messages,
    sessionId,
    isStreaming,
    send,
    abort,
    reset,
    handlePermission,
  };
}
