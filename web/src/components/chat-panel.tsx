"use client";

import {
  Box,
  EmptyState,
  Flex,
  Text,
  VStack,
} from "@chakra-ui/react";
import { LuClock, LuSend } from "react-icons/lu";
import { useCallback, useEffect, useRef, useState } from "react";
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
  onSlashCommand,
  workingDirectory,
  onWorkingDirectoryChange,
  onBrowseFolder,
  isConnected = false,
  onStreamingChange,
}: ChatPanelProps) {
  const { messages, orchestrations, queuedMessages, sessionId, isStreaming, send, abort, dequeueMessage, handlePermission } =
    useChat(agent, initialSessionId, workingDirectory);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const isPinnedRef = useRef(true);
  const onStreamingChangeRef = useRef(onStreamingChange);
  onStreamingChangeRef.current = onStreamingChange;
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
    const threshold = 30;
    isPinnedRef.current = container.scrollTop + container.clientHeight >= container.scrollHeight - threshold;
  }, []);

  useEffect(() => {
    if (sessionId) onSessionCreated(sessionId);
  }, [sessionId, onSessionCreated]);

  useEffect(() => {
    if (!isPinnedRef.current) return;
    scrollContainerRef.current?.scrollTo({
      top: scrollContainerRef.current.scrollHeight,
      behavior: "instant",
    });
  }, [messages, queuedMessages]);

  useEffect(() => {
    if (isStreaming) {
      isPinnedRef.current = true;
      scrollContainerRef.current?.scrollTo({
        top: scrollContainerRef.current.scrollHeight,
        behavior: "instant",
      });
    }
    onStreamingChangeRef.current?.(isStreaming);
  }, [isStreaming]);

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
      <Box ref={scrollContainerRef} flex={1} overflowY="auto" px={2} py={2} onScroll={handleScroll}>
        {messages.length === 0 ? (
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
          <VStack gap={2} align="stretch">
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
                  <Text fontSize="11px" color="fg.subtle" fontWeight="medium">Queued</Text>
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
