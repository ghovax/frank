"use client";

import {
  Box,
  createListCollection,
  Flex,
  IconButton,
  Input,
  Portal,
  Select,
} from "@chakra-ui/react";
import { useMemo, useRef, useState, type KeyboardEvent } from "react";
import { LuArrowUp, LuFolder, LuShield, LuShieldOff, LuSquare, LuUser } from "react-icons/lu";

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
}: ChatInputProps) {
  const inputRef = useRef<HTMLInputElement>(null);
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
    onSend(trimmed);
    setInputValue("");
  }

  function handleKeyDown(event: KeyboardEvent) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      if (isStreaming) return;
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
              px={1}
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
              px={1}
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
      </Flex>
      <Flex gap={2} px={2} pb={2} align="center">
        <Box flex={1} bg="bg" borderRadius="sm">
          <Input
            ref={inputRef}
            placeholder={disabled ? "Connecting to server..." : "Send a message..."}
            value={inputValue}
            onChange={(event) => setInputValue(event.target.value)}
            onKeyDown={handleKeyDown}
            disabled={isStreaming || disabled}
            fontSize="sm"
            size="sm"
            h="34px"
            borderRadius="sm"
            border="1px solid"
            borderColor="border"
            _focus={{ borderColor: "border.emphasized", boxShadow: "none" }}
          />
        </Box>
        {isStreaming ? (
          <IconButton
            aria-label="Stop"
            onClick={onAbort}
            colorPalette="red"
            variant="solid"
            borderRadius="sm"
            minW="34px"
            h="34px"
          >
            <LuSquare size={16} />
          </IconButton>
        ) : (
          <IconButton
            aria-label="Send"
            onClick={handleSubmit}
            colorPalette="blue"
            variant="solid"
            borderRadius="sm"
            minW="34px"
            h="34px"
            disabled={disabled}
          >
            <LuArrowUp size={16} />
          </IconButton>
        )}
      </Flex>
    </Box>
  );
}
