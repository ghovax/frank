"use client";

import {
  Box,
  Button,
  createListCollection,
  Flex,
  IconButton,
  Input,
  Portal,
  Select,
  Text,
  Textarea,
} from "@chakra-ui/react";
import { useMemo, useRef, useState, type KeyboardEvent } from "react";
import { LuArrowUp, LuFolder, LuNetwork, LuShield, LuShieldOff, LuSquare, LuUser } from "react-icons/lu";

interface ChatInputProps {
  onSend: (text: string) => void;
  onAbort: () => void;
  isStreaming: boolean;
  disabled?: boolean;
  sessionId?: string | null;
  workingDirectory?: string;
  onWorkingDirectoryChange?: (dir: string) => void;
  onBrowseFolder?: () => void;
  agents: { name: string; label: string }[];
  selectedAgent: string;
  onAgentChange: (agent: string) => void;
  bypassPermissions: boolean;
  onToggleBypass: () => void;
  agentsCount?: number;
  onShowAgents?: () => void;
}

export function ChatInput({
  onSend,
  onAbort,
  isStreaming,
  disabled,
  sessionId,
  workingDirectory,
  onWorkingDirectoryChange,
  onBrowseFolder,
  agents,
  selectedAgent,
  onAgentChange,
  bypassPermissions,
  onToggleBypass,
  agentsCount = 0,
  onShowAgents,
}: ChatInputProps) {
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const [inputValue, setInputValue] = useState("");

  const agentCollection = useMemo(
    () => createListCollection({
      items: agents.map((agent) => ({ label: agent.label, value: agent.name })),
    }),
    [agents]
  );

  const bypassCollection = useMemo(
    () => createListCollection({
      items: [
        { label: "Default", value: "default" },
        { label: "Bypass", value: "bypass" },
      ],
    }),
    []
  );

  function handleSubmit() {
    const trimmed = inputValue.trim();
    if (!trimmed) return;
    // While the agent is busy this enqueues for the next turn (handled upstream).
    onSend(trimmed);
    setInputValue("");
  }

  function handleKeyDown(event: KeyboardEvent) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      handleSubmit();
    }
  }

  return (
    <Box borderTop="1px solid" borderColor="border" bg="bg.subtle">
      <Flex gap={2} px={2} pt={2} pb={1.5} align="center">
        <Box color={bypassPermissions ? "red.fg" : "fg.subtle"} fontSize="sm" flexShrink={0} display="flex" alignItems="center">
          {bypassPermissions ? <LuShieldOff size={16} /> : <LuShield size={16} />}
        </Box>
        <Select.Root
          collection={bypassCollection}
          value={[bypassPermissions ? "bypass" : "default"]}
          onValueChange={(details) => {
            if (details.value[0] === "bypass" && !bypassPermissions) onToggleBypass();
            if (details.value[0] === "default" && bypassPermissions) onToggleBypass();
          }}
          size="xs"
          w="80px"
          flexShrink={0}
        >
          <Select.Control>
            <Select.Trigger
              borderRadius="sm"
              fontSize="xs"
              px={2}
              bg={bypassPermissions ? "red.subtle" : "bg"}
              border="1px solid"
              borderColor={bypassPermissions ? "red.muted" : "border"}
              colorPalette={bypassPermissions ? "red" : undefined}
              style={{ height: "28px", minHeight: "28px", lineHeight: "28px" }}
            >
              <Select.ValueText />
            </Select.Trigger>
            <Select.IndicatorGroup>
              <Select.Indicator />
            </Select.IndicatorGroup>
          </Select.Control>
          <Portal>
            <Select.Positioner>
              <Select.Content borderRadius="sm" minW="100px">
                {bypassCollection.items.map((item) => (
                  <Select.Item item={item} key={item.value}>
                    {item.label}
                    <Select.ItemIndicator />
                  </Select.Item>
                ))}
              </Select.Content>
            </Select.Positioner>
          </Portal>
        </Select.Root>
        <Box color="fg.muted" fontSize="sm" flexShrink={0} display="flex" alignItems="center">
          <LuUser size={16} />
        </Box>
        <Select.Root
          collection={agentCollection}
          value={[selectedAgent]}
          onValueChange={(details) => {
            if (details.value[0]) onAgentChange(details.value[0]);
          }}
          size="xs"
          w="80px"
          flexShrink={0}
        >
          <Select.Control>
            <Select.Trigger
              borderRadius="sm"
              fontSize="xs"
              px={2}
              bg="bg"
              border="1px solid"
              borderColor="border"
              style={{ height: "28px", minHeight: "28px", lineHeight: "28px" }}
            >
              <Select.ValueText placeholder="Agent" />
            </Select.Trigger>
            <Select.IndicatorGroup>
              <Select.Indicator />
            </Select.IndicatorGroup>
          </Select.Control>
          <Portal>
            <Select.Positioner>
              <Select.Content borderRadius="sm" minW="100px">
                {agentCollection.items.map((item) => (
                  <Select.Item item={item} key={item.value}>
                    {item.label}
                    <Select.ItemIndicator />
                  </Select.Item>
                ))}
              </Select.Content>
            </Select.Positioner>
          </Portal>
        </Select.Root>
        <Box flex={1} />
        <Input
          size="xs"
          h="28px"
          fontSize="xs"
          placeholder="/path"
          value={workingDirectory ?? ""}
          onChange={(event) => onWorkingDirectoryChange?.(event.target.value)}
          disabled={!!sessionId}
          border="1px solid"
          borderColor="border"
          bg="bg"
          borderRadius="sm"
          w="200px"
          flexShrink={0}
        />
        <IconButton
          aria-label="Browse folder"
          size="xs"
          variant="ghost"
          borderRadius="sm"
          minW="28px"
          h="28px"
          onClick={onBrowseFolder}
        >
          <LuFolder size={16} />
        </IconButton>
        {agentsCount > 0 && (
          <Button
            size="xs"
            variant="solid"
            colorPalette="orange"
            borderRadius="sm"
            fontSize="xs"
            h="28px"
            flexShrink={0}
            onClick={onShowAgents}
          >
            <LuNetwork size={13} />
            Agents ({agentsCount})
          </Button>
        )}
      </Flex>
      <Box px={2} pb={2}>
        <Box
          bg="bg"
          border="1px solid"
          borderColor="border"
          borderRadius="md"
          _focusWithin={{ borderColor: "border.emphasized" }}
        >
          <Textarea
            ref={inputRef}
            placeholder={
              disabled
                ? "Connecting to server..."
                : isStreaming
                  ? "Queue a message — it's sent on the next turn..."
                  : "Send a message..."
            }
            value={inputValue}
            onChange={(event) => setInputValue(event.target.value)}
            onKeyDown={handleKeyDown}
            disabled={disabled}
            fontSize="sm"
            border="none"
            outline="none"
            resize="none"
            px={3}
            py={2}
            _focus={{ boxShadow: "none", borderColor: "transparent" }}
            _focusVisible={{ boxShadow: "none", outline: "none", borderColor: "transparent" }}
          />
          <Flex align="center" justify="space-between" px={3} pt={1} pb={2.5} gap={2}>
            <Text fontSize="xs" color="fg.subtle" truncate>
              {isStreaming
                ? "Agent is working — messages queue for the next turn"
                : "Enter to send — Shift+Enter for newline"}
            </Text>
            <Flex gap={1.5} flexShrink={0}>
              {isStreaming && (
                <IconButton
                  aria-label="Stop"
                  onClick={onAbort}
                  colorPalette="red"
                  variant="solid"
                  borderRadius="sm"
                  minW="32px"
                  h="32px"
                >
                  <LuSquare size={14} />
                </IconButton>
              )}
              <IconButton
                aria-label="Send"
                onClick={handleSubmit}
                colorPalette="blue"
                variant="solid"
                borderRadius="sm"
                minW="32px"
                h="32px"
                disabled={disabled || !inputValue.trim()}
              >
                <LuArrowUp size={16} />
              </IconButton>
            </Flex>
          </Flex>
        </Box>
      </Box>
    </Box>
  );
}
