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
} from "@chakra-ui/react";
import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { LuArrowUp, LuBan, LuCheck, LuFolder, LuHistory, LuNetwork, LuShield, LuShieldOff, LuSquare, LuUser } from "react-icons/lu";
import { validateWorkingDirectory } from "@/lib/api";

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
  agentsAvailable?: boolean;
  agentsOpen?: boolean;
  onShowAgents?: () => void;
  historyOpen?: boolean;
  onToggleHistory?: () => void;
}

export function ChatInput({
  onSend,
  onAbort,
  isStreaming,
  disabled,
  workingDirectory,
  onWorkingDirectoryChange,
  onBrowseFolder,
  agents,
  selectedAgent,
  onAgentChange,
  bypassPermissions,
  onToggleBypass,
  agentsCount = 0,
  agentsAvailable = false,
  agentsOpen = false,
  onShowAgents,
  historyOpen = false,
  onToggleHistory,
}: ChatInputProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [inputValue, setInputValue] = useState("");
  const [directoryState, setDirectoryState] = useState({
    path: workingDirectory ?? "",
    valid: true,
    checking: false,
  });

  const agentCollection = useMemo(
    () => createListCollection({
      items: agents.map((agent) => ({ label: agent.label, value: agent.name })),
    }),
    [agents]
  );

  const bypassCollection = useMemo(
    () => createListCollection({
      items: [
        { label: "Default permissions", value: "default" },
        { label: "Bypass permissions", value: "bypass" },
      ],
    }),
    []
  );

  const currentDirectory = (workingDirectory ?? "").trim();
  const directoryValid = !!currentDirectory && directoryState.path === currentDirectory && directoryState.valid;
  const directoryChecking = directoryState.path !== currentDirectory || directoryState.checking;

  useEffect(() => {
    let cancelled = false;
    const timeout = window.setTimeout(() => {
      if (!currentDirectory) {
        setDirectoryState({ path: currentDirectory, valid: false, checking: false });
        return;
      }
      setDirectoryState({ path: currentDirectory, valid: false, checking: true });
      validateWorkingDirectory(currentDirectory)
        .then((result) => {
          if (!cancelled) {
            setDirectoryState({ path: currentDirectory, valid: result.valid, checking: false });
          }
        })
        .catch(() => {
          if (!cancelled) {
            setDirectoryState({ path: currentDirectory, valid: false, checking: false });
          }
        });
    }, 250);

    return () => {
      cancelled = true;
      window.clearTimeout(timeout);
    };
  }, [currentDirectory]);

  function handleSubmit() {
    const trimmed = inputValue.trim();
    if (!trimmed) return;
    if (!directoryValid) return;
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
      {/* Top row (above the input): history button on the left, working
          directory and the Agents button on the right (Agents far right).
          Each "unit" is an atomic flex group so it never splits across rows
          when the row wraps; space-between keeps the row filling the full
          width. */}
      <Flex justify="space-between" align="center" rowGap={1.5} gap={{ base: 1.5, md: 2 }} flexWrap="wrap" px={2} pt={2}>
        <Flex align="center" gap={{ base: 1.5, md: 2 }} flexShrink={0}>
          {onToggleHistory && (
            <Button
              size="xs"
              variant={historyOpen ? "solid" : "outline"}
              colorPalette={historyOpen ? "blue" : undefined}
              borderRadius="sm"
              fontSize="xs"
              h="28px"
              flexShrink={0}
              onClick={onToggleHistory}
            >
              <LuHistory size={13} />
              History
            </Button>
          )}
        </Flex>

        <Flex align="center" gap={1.5} flexShrink={0}>
          <Box
            color={directoryValid ? "green.fg" : "red.fg"}
            opacity={directoryChecking ? 0.45 : 1}
            fontSize="sm"
            flexShrink={0}
            display="flex"
            alignItems="center"
            title={directoryValid ? "Valid directory" : "Invalid directory"}
          >
            {directoryValid ? <LuCheck size={16} /> : <LuBan size={16} />}
          </Box>
          <Input
            size="xs"
            h="28px"
            fontSize="xs"
            placeholder="Working directory"
            value={workingDirectory ?? ""}
            onChange={(event) => onWorkingDirectoryChange?.(event.target.value)}
            border="1px solid"
            borderColor={directoryValid ? "border" : "red.muted"}
            bg="bg"
            borderRadius="sm"
            w={{ base: "100%", sm: "200px" }}
            maxW={{ base: "100%", sm: "280px" }}
          />
          <IconButton
            aria-label="Browse folder"
            size="xs"
            variant="ghost"
            borderRadius="sm"
            minW="28px"
            h="28px"
            flexShrink={0}
            onClick={onBrowseFolder}
          >
            <LuFolder size={16} />
          </IconButton>
          {agentsAvailable && (
            <Button
              size="xs"
              variant={agentsOpen ? "solid" : "outline"}
              colorPalette={agentsCount > 0 || agentsOpen ? "gray" : undefined}
              borderRadius="sm"
              fontSize="xs"
              h="28px"
              flexShrink={0}
              onClick={onShowAgents}
            >
              <LuNetwork size={13} />
              {agentsCount > 0 ? `Agents (${agentsCount})` : "Agents"}
            </Button>
          )}
        </Flex>
      </Flex>

      {/* Message input */}
      <Box px={2} pt={1.5} pb={1.5}>
        <Box
          bg="bg"
          border="1px solid"
          borderColor={directoryValid ? "border" : "red.muted"}
          borderRadius="sm"
          _focusWithin={{ borderColor: "border.emphasized" }}
        >
          <Flex align="center" gap={2} px={1.5} py={1.5}>
            <Input
              ref={inputRef}
              placeholder={
                disabled
                  ? "Connecting to server..."
                  : !directoryValid
                    ? "Choose a valid project path before sending..."
                    : isStreaming
                      ? "Queue a message for the next turn..."
                      : "Send a message..."
              }
              value={inputValue}
              onChange={(event) => setInputValue(event.target.value)}
              onKeyDown={handleKeyDown}
              disabled={disabled}
              fontSize="sm"
              h="32px"
              lineHeight="32px"
              border="none"
              outline="none"
              px={1}
              py={0}
              _focus={{ boxShadow: "none", borderColor: "transparent" }}
              _focusVisible={{ boxShadow: "none", outline: "none", borderColor: "transparent" }}
            />
            <Flex gap={1.5} flexShrink={0}>
              {isStreaming ? (
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
              ) : (
                <IconButton
                  aria-label="Send"
                  onClick={handleSubmit}
                  colorPalette="blue"
                  variant="solid"
                  borderRadius="sm"
                  minW="32px"
                  h="32px"
                  disabled={disabled || !directoryValid || !inputValue.trim()}
                >
                  <LuArrowUp size={16} />
                </IconButton>
              )}
            </Flex>
          </Flex>
        </Box>
      </Box>

      {/* Bottom row (below the input): permissions on the left, persona choice
          next to it. */}
      <Flex justify="flex-start" align="center" gap={1.5} px={2} pb={2}>
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
          w="max-content"
          minW="max-content"
          maxW="none"
          flexShrink={0}
        >
          <Select.Control w="max-content" minW="max-content" maxW="none">
            <Select.Trigger
              w="max-content"
              borderRadius="sm"
              fontSize="xs"
              px={2}
              pe={7}
              bg={bypassPermissions ? "red.subtle" : "bg"}
              border="1px solid"
              borderColor={bypassPermissions ? "red.muted" : "border"}
              colorPalette={bypassPermissions ? "red" : undefined}
              minW="max-content"
              maxW="none"
              whiteSpace="nowrap"
              style={{ height: "28px", minHeight: "28px", lineHeight: "28px" }}
            >
              <Select.ValueText maxW="none" overflow="visible" textOverflow="clip" whiteSpace="nowrap" />
            </Select.Trigger>
            <Select.IndicatorGroup>
              <Select.Indicator />
            </Select.IndicatorGroup>
          </Select.Control>
          <Portal>
            <Select.Positioner>
              <Select.Content borderRadius="sm" minW="max-content" w="max-content">
                {bypassCollection.items.map((item) => (
                  <Select.Item item={item} key={item.value} whiteSpace="nowrap">
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
          w="max-content"
          minW="max-content"
          maxW="none"
          flexShrink={0}
        >
          <Select.Control w="max-content" minW="max-content" maxW="none">
            <Select.Trigger
              w="max-content"
              borderRadius="sm"
              fontSize="xs"
              px={2}
              pe={7}
              bg="bg"
              border="1px solid"
              borderColor="border"
              minW="max-content"
              maxW="none"
              whiteSpace="nowrap"
              style={{ height: "28px", minHeight: "28px", lineHeight: "28px" }}
            >
              <Select.ValueText placeholder="Agent" maxW="none" overflow="visible" textOverflow="clip" whiteSpace="nowrap" />
            </Select.Trigger>
            <Select.IndicatorGroup>
              <Select.Indicator />
            </Select.IndicatorGroup>
          </Select.Control>
          <Portal>
            <Select.Positioner>
              <Select.Content borderRadius="sm" minW="max-content" w="max-content">
                {agentCollection.items.map((item) => (
                  <Select.Item item={item} key={item.value} whiteSpace="nowrap">
                    {item.label}
                    <Select.ItemIndicator />
                  </Select.Item>
                ))}
              </Select.Content>
            </Select.Positioner>
          </Portal>
        </Select.Root>
      </Flex>
    </Box>
  );
}
