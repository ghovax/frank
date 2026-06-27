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
import { LuArrowUp, LuBan, LuCheck, LuFolder, LuHistory, LuNetwork, LuShield, LuShieldCheck, LuShieldOff, LuSquare, LuUser } from "react-icons/lu";
import { validateWorkingDirectory, type PermissionMode } from "@/lib/api";

interface ChatInputProps {
  onSend: (text: string) => void;
  onAbort: () => void;
  isStreaming: boolean;
  disabled?: boolean;
  sessionId?: string | null;
  workingDirectory?: string;
  onWorkingDirectoryChange?: (dir: string) => void;
  onBrowseFolder?: () => void;
  agents: { id: string; name: string }[];
  selectedAgent: string;
  onAgentChange: (agent: string) => void;
  permissionMode: PermissionMode;
  onPermissionModeChange: (mode: PermissionMode) => void;
  agentsCount?: number;
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
  permissionMode,
  onPermissionModeChange,
  agentsCount = 0,
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
      items: agents.map((agent) => ({ label: agent.name, value: agent.id })),
    }),
    [agents]
  );

  const permissionCollection = useMemo(
    () => createListCollection({
      items: [
        { label: "Default permissions", value: "default" },
        { label: "Read-only permissions", value: "read_only" },
        { label: "Bypass permissions", value: "bypass" },
      ],
    }),
    []
  );
  const permissionAppearance = {
    default: {
      icon: <LuShield size={16} />,
      color: "fg.subtle",
      bg: "bg",
      borderColor: "border",
      colorPalette: undefined,
    },
    read_only: {
      icon: <LuShieldCheck size={16} />,
      color: "green.fg",
      bg: "green.subtle",
      borderColor: "green.muted",
      colorPalette: "green",
    },
    bypass: {
      icon: <LuShieldOff size={16} />,
      color: "red.fg",
      bg: "red.subtle",
      borderColor: "red.muted",
      colorPalette: "red",
    },
  }[permissionMode];

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
      {/* Top row (above the input): history button on the left, Agents button
          on the right. The Agents button is always shown; the panel renders an
          empty state when there is no activity yet. */}
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
          <Button
            size="xs"
            variant={agentsOpen ? "solid" : "outline"}
            colorPalette={agentsCount > 0 || agentsOpen ? "orange" : undefined}
            borderRadius="sm"
            fontSize="xs"
            h="28px"
            flexShrink={0}
            onClick={onShowAgents}
          >
            <LuNetwork size={13} />
            {agentsCount > 0 ? `Agents (${agentsCount})` : "Agents"}
          </Button>
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

      {/* Bottom row (below the input): permissions and persona on the left,
          working-directory selection aligned to the right. */}
      <Flex justify="space-between" align="center" rowGap={1.5} columnGap={2} flexWrap="wrap" px={2} pb={2}>
        <Flex align="center" gap={1.5} flexWrap="wrap" flexShrink={0}>
          <Box color={permissionAppearance.color} fontSize="sm" flexShrink={0} display="flex" alignItems="center">
            {permissionAppearance.icon}
          </Box>
          <Select.Root
            collection={permissionCollection}
            value={[permissionMode]}
            onValueChange={(details) => {
              const nextMode = details.value[0] as PermissionMode | undefined;
              if (nextMode) onPermissionModeChange(nextMode);
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
                bg={permissionAppearance.bg}
                border="1px solid"
                borderColor={permissionAppearance.borderColor}
                colorPalette={permissionAppearance.colorPalette}
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
                  {permissionCollection.items.map((item) => (
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
        <Flex align="center" justify="flex-end" gap={1.5} flex={{ base: "1 1 100%", md: "1 1 auto" }} minW={0} ml="auto">
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
            w={{ base: "min(100%, 360px)", md: "280px" }}
            maxW="100%"
            textAlign="left"
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
        </Flex>
      </Flex>
    </Box>
  );
}
