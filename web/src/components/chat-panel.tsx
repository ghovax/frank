"use client";

import {
  Box,
  EmptyState,
  Flex,
  Text,
  VStack,
} from "@chakra-ui/react";
import { LuClock, LuSend } from "react-icons/lu";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useChat } from "@/lib/use-chat";
import { ChatMessageItem } from "./chat-message";
import { ChatInput } from "./chat-input";
import { AgentsPanel } from "./agents-panel";
import { setBypassPermissions } from "@/lib/api";

interface ChatPanelProps {
  agent: string;
  agents: { name: string; label: string }[];
  onAgentChange: (agent: string) => void;
  initialSessionId: string | null;
  onSessionCreated: (sessionId: string) => void;
  onSlashCommand?: (command: string) => void;
  workingDirectory?: string;
  onWorkingDirectoryChange?: (dir: string) => void;
  onBrowseFolder?: () => void;
  isConnected?: boolean;
  onStreamingChange?: (isStreaming: boolean) => void;
}

export function ChatPanel({
  agent,
  agents,
  onAgentChange,
  initialSessionId,
  onSessionCreated,
  workingDirectory,
  onWorkingDirectoryChange,
  onBrowseFolder,
  isConnected = false,
  onStreamingChange,
}: ChatPanelProps) {
  const { messages, orchestrations, queuedMessages, sessionId, isStreaming, isHistoryLoading, send, abort, dequeueMessage, handlePermission } =
    useChat(agent, initialSessionId, workingDirectory);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const scrollContentRef = useRef<HTMLDivElement>(null);
  const scrollFrameRef = useRef<number | null>(null);
  const isPinnedRef = useRef(true);
  const onStreamingChangeRef = useRef(onStreamingChange);
  const [bypassPermissions, setBypassPermissionsState] = useState(false);
  const [agentsPanelOpen, setAgentsPanelOpen] = useState(false);
  const [focusedOrchestrationId, setFocusedOrchestrationId] = useState<string | null>(null);

  const openAgents = useCallback((orchestrationId: string) => {
    setFocusedOrchestrationId(orchestrationId);
    setAgentsPanelOpen(true);
  }, []);

  const handleScroll = useCallback(() => {
    const container = scrollContainerRef.current;
    if (!container) return;
    const threshold = 80;
    isPinnedRef.current = container.scrollTop + container.clientHeight >= container.scrollHeight - threshold;
  }, []);

  useEffect(() => {
    onStreamingChangeRef.current = onStreamingChange;
  }, [onStreamingChange]);

  const scheduleScrollToBottom = useCallback(() => {
    if (!isPinnedRef.current) return;
    if (scrollFrameRef.current != null) return;
    scrollFrameRef.current = window.requestAnimationFrame(() => {
      scrollFrameRef.current = null;
      const container = scrollContainerRef.current;
      if (!container || !isPinnedRef.current) return;
      container.scrollTop = container.scrollHeight;
    });
  }, []);

  useEffect(() => {
    if (sessionId) onSessionCreated(sessionId);
  }, [sessionId, onSessionCreated]);

  useLayoutEffect(() => {
    scheduleScrollToBottom();
  }, [messages, queuedMessages, scheduleScrollToBottom]);

  useEffect(() => {
    const content = scrollContentRef.current;
    if (!content) return;
    const observer = new ResizeObserver(() => scheduleScrollToBottom());
    observer.observe(content);
    return () => observer.disconnect();
  }, [scheduleScrollToBottom]);

  useEffect(() => {
    return () => {
      if (scrollFrameRef.current != null) {
        window.cancelAnimationFrame(scrollFrameRef.current);
      }
    };
  }, []);

  useEffect(() => {
    if (isStreaming) {
      isPinnedRef.current = true;
      scheduleScrollToBottom();
    }
    onStreamingChangeRef.current?.(isStreaming);
  }, [isStreaming, scheduleScrollToBottom]);

  async function handleToggleBypass() {
    if (!sessionId) return;
    const newValue = !bypassPermissions;
    setBypassPermissionsState(newValue);
    try {
      await setBypassPermissions(sessionId, newValue);
    } catch {
      setBypassPermissionsState(!newValue);
    }
  }

  const totalSteps = orchestrations.reduce((sum, orchestration) => sum + orchestration.steps.length, 0);

  return (
    <Flex direction="column" h="100%" position="relative">
      <Box ref={scrollContainerRef} flex={1} overflowY="auto" px={2} py={2} onScroll={handleScroll} style={{ overflowAnchor: "none" }}>
        {isHistoryLoading ? (
          <Flex h="100%" />
        ) : messages.length === 0 ? (
          <Flex direction="column" align="center" justify="center" h="100%">
            <EmptyState.Root>
              <EmptyState.Content>
                <EmptyState.Indicator>
                  <LuSend />
                </EmptyState.Indicator>
                <VStack gap={1}>
                  <EmptyState.Title>No messages yet</EmptyState.Title>
                  <EmptyState.Description>
                    Send a message to start
                  </EmptyState.Description>
                </VStack>
              </EmptyState.Content>
            </EmptyState.Root>
          </Flex>
        ) : (
          <VStack ref={scrollContentRef} gap={2} align="stretch">
            {messages.map((message) => (
              <ChatMessageItem
                key={message.id}
                message={message}
                onPermission={handlePermission}
                onOpenAgents={openAgents}
              />
            ))}
            {queuedMessages.map((text, index) => (
              <Box
                key={`queued-${index}`}
                alignSelf="flex-end"
                maxW="80%"
                px={2}
                py={1.5}
                borderRadius="sm"
                border="1px dashed"
                borderColor="border"
                bg="bg.subtle"
                opacity={0.55}
                cursor="pointer"
                title="Queued — click to remove"
                onClick={() => dequeueMessage(index)}
              >
                <Flex align="center" gap={1.5}>
                  <LuClock size={11} />
                  <Text fontSize="xs" color="fg.subtle" fontWeight="medium">Queued</Text>
                </Flex>
                <Text fontSize="sm" color="fg.muted">{text}</Text>
              </Box>
            ))}
          </VStack>
        )}
      </Box>

      <ChatInput
        onSend={send}
        onAbort={abort}
        isStreaming={isStreaming}
        disabled={!isConnected}
        sessionId={sessionId}
        workingDirectory={workingDirectory}
        onWorkingDirectoryChange={onWorkingDirectoryChange}
        onBrowseFolder={onBrowseFolder}
        agents={agents}
        selectedAgent={agent}
        onAgentChange={onAgentChange}
        bypassPermissions={bypassPermissions}
        onToggleBypass={handleToggleBypass}
        agentsCount={totalSteps}
        onShowAgents={() => {
          setFocusedOrchestrationId(null);
          setAgentsPanelOpen(true);
        }}
      />

      <AgentsPanel
        orchestrations={orchestrations}
        open={agentsPanelOpen}
        onClose={() => setAgentsPanelOpen(false)}
        focusedOrchestrationId={focusedOrchestrationId}
      />
    </Flex>
  );
}
