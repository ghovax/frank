"use client";

import { Box, Button, EmptyState, Field, Flex, Input, Text, VStack } from "@chakra-ui/react";
import { LuFolder, LuSend } from "react-icons/lu";
import { useEffect, useRef } from "react";
import { useChat } from "@/lib/use-chat";
import { ChatMessageItem } from "./chat-message";
import { ChatInput } from "./chat-input";

interface ChatPanelProps {
  agent: string;
  agents: string[];
  onAgentChange: (agent: string) => void;
  initialSessionId: string | null;
  onSessionCreated: (sessionId: string) => void;
  onSlashCommand?: (command: string) => void;
  workingDirectory?: string;
  onWorkingDirectoryChange?: (dir: string) => void;
  onBrowseFolder?: () => void;
  isConnected?: boolean;
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
}: ChatPanelProps) {
  const { messages, sessionId, isStreaming, send, abort, handlePermission } =
    useChat(agent, initialSessionId, workingDirectory);
  const scrollContainerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (sessionId) onSessionCreated(sessionId);
  }, [sessionId, onSessionCreated]);

  useEffect(() => {
    scrollContainerRef.current?.scrollTo({
      top: scrollContainerRef.current.scrollHeight,
      behavior: "smooth",
    });
  }, [messages]);

  function dispatchSlashCommand(command: string) {
    if (command === "/abort") {
      abort();
      return;
    }
    onSlashCommand?.(command);
  }

  return (
    <Flex direction="column" h="100%">
      <Box ref={scrollContainerRef} flex={1} overflowY="auto" px={3} py={3}>
        {messages.length === 0 ? (
          <Flex direction="column" align="center" justify="center" h="100%">
            <VStack align="center" gap={6} maxW="360px" w="100%">
              <EmptyState.Root>
                <EmptyState.Content>
                  <EmptyState.Indicator>
                    <LuSend />
                  </EmptyState.Indicator>
                  <VStack gap={1}>
                    <EmptyState.Title>No messages yet</EmptyState.Title>
                    <EmptyState.Description>
                      Send a message or type / for commands
                    </EmptyState.Description>
                  </VStack>
                </EmptyState.Content>
              </EmptyState.Root>

              <Field.Root w="100%">
                <Field.Label fontSize="xs" color="fg.muted" textAlign="center" w="100%">
                  Working directory
                </Field.Label>
                <Flex gap={1} w="100%">
                  <Input
                    size="xs"
                    fontSize="xs"
                    placeholder="/path/to/project"
                    value={workingDirectory ?? ""}
                    onChange={(e) => onWorkingDirectoryChange?.(e.target.value)}
                    borderRadius="lg"
                  />
                  <Button
                    size="xs"
                    variant="outline"
                    borderRadius="lg"
                    fontSize="xs"
                    px={2}
                    minW="unset"
                    onClick={onBrowseFolder}
                    title="Browse..."
                  >
                    <LuFolder size={14} />
                  </Button>
                </Flex>
              </Field.Root>
            </VStack>
          </Flex>
        ) : (
          <VStack gap={4} align="stretch">
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
        onSlashCommand={dispatchSlashCommand}
        isStreaming={isStreaming}
        agents={agents}
        selectedAgent={agent}
        onAgentChange={onAgentChange}
        disabled={!isConnected}
      />
    </Flex>
  );
}
