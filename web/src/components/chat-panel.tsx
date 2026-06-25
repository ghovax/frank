"use client";

import {
  Box,
  EmptyState,
  Flex,
  VStack,
} from "@chakra-ui/react";
import { LuSend } from "react-icons/lu";
import { useCallback, useEffect, useRef, useState } from "react";
import { useChat } from "@/lib/use-chat";
import { ChatMessageItem } from "./chat-message";
import { ChatInput } from "./chat-input";
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
  const { messages, sessionId, isStreaming, send, abort, handlePermission } =
    useChat(agent, initialSessionId, workingDirectory);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const isPinnedRef = useRef(true);
  const onStreamingChangeRef = useRef(onStreamingChange);
  onStreamingChangeRef.current = onStreamingChange;
  const [bypassPermissions, setBypassPermissionsState] = useState(false);

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
  }, [messages]);

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

  return (
    <Flex direction="column" h="100%">
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
              />
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
      />
    </Flex>
  );
}
