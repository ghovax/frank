"use client";

import {
  Box,
  Button,
  createListCollection,
  Flex,
  IconButton,
  Input,
  Menu,
  Portal,
  Select,
} from "@chakra-ui/react";
import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { LuArrowUp, LuChevronDown, LuFolder, LuHistory, LuLock, LuLockOpen, LuNetwork, LuSettings, LuShield, LuShieldCheck, LuShieldOff, LuSquare, LuUser } from "react-icons/lu";
import { validateWorkingDirectory, type PermissionMode } from "@/lib/api";
import { SettingsDialog } from "./settings-dialog";

interface ChatInputProps {
  onSend: (text: string) => void;
  onAbort: () => void;
  isStreaming: boolean;
  disabled?: boolean;
  sessionId?: string | null;
  workingDirectory?: string;
  recentProjects?: { path: string; name: string }[];
  onWorkingDirectoryChange?: (dir: string) => void;
  onBrowseFolder?: () => void;
  sandboxEnabled?: boolean;
  onSandboxEnabledChange?: (enabled: boolean) => void;
  agents: { id: string; name: string; title?: string }[];
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
  sessionId,
  workingDirectory,
  recentProjects = [],
  onWorkingDirectoryChange,
  onBrowseFolder,
  sandboxEnabled = true,
  onSandboxEnabledChange,
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
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [directoryState, setDirectoryState] = useState({
    path: workingDirectory ?? "",
    valid: true,
    checking: false,
  });

  const agentCollection = useMemo(
    () => createListCollection({
      items: agents.map((agent) => ({ label: agent.title || agent.name, value: agent.id })),
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
  const sandboxAppearance = sandboxEnabled
    ? {
        label: "Sandboxed",
        colorPalette: "green" as const,
        variant: "solid" as const,
      }
    : {
        label: "Unsandboxed",
        colorPalette: "red" as const,
        variant: "solid" as const,
      };

  const currentDirectory = (workingDirectory ?? "").trim();
  const directoryValid = !!currentDirectory && directoryState.path === currentDirectory && directoryState.valid;
  // A session is bound to the folder it was started in: once it exists, the
  // project can no longer be changed, so the selector and browse are locked.
  const folderLocked = !!sessionId;
  // The server owns each folder's display name (derived with real path tooling),
  // so the selector only ever reads names — it never parses a path itself.
  const currentProjectName = useMemo(
    () => recentProjects.find((project) => project.path === currentDirectory)?.name ?? "",
    [recentProjects, currentDirectory]
  );
  const projectItems = useMemo(() => {
    const seen = new Set<string>();
    const items: { path: string; name: string }[] = [];
    const candidates = currentDirectory
      ? [{ path: currentDirectory, name: currentProjectName }, ...recentProjects]
      : recentProjects;
    for (const project of candidates) {
      const path = project.path.trim();
      if (!path || seen.has(path)) continue;
      seen.add(path);
      items.push({ path, name: project.name });
    }
    return items;
  }, [currentDirectory, currentProjectName, recentProjects]);

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
          <Button
            size="xs"
            variant="outline"
            borderRadius="sm"
            fontSize="xs"
            h="28px"
            flexShrink={0}
            onClick={() => setSettingsOpen(true)}
          >
            <LuSettings size={13} />
            Settings
          </Button>
        </Flex>

        <Flex align="center" gap={1.5} flexShrink={0} flexWrap="wrap" justify="flex-end">
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
                fontWeight="medium"
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
      <Box px={2} pt={2} pb={2}>
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

      {/* Bottom row (below the input): permission, sandbox, and project controls. */}
      <Flex justify="flex-start" align="center" rowGap={1.5} columnGap={2} flexWrap="wrap" px={2} pb={2}>
        <Flex align="center" gap={2} flexWrap="wrap" flexShrink={0}>
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
                fontWeight="medium"
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
          <Box color={sandboxEnabled ? "green.fg" : "red.fg"} fontSize="sm" pl={1} flexShrink={0} display="flex" alignItems="center">
            {sandboxEnabled ? <LuLock size={16} /> : <LuLockOpen size={16} />}
          </Box>
          <Button
            size="xs"
            variant={sandboxAppearance.variant}
            colorPalette={sandboxAppearance.colorPalette}
            borderRadius="sm"
            fontSize="xs"
            h="28px"
            fontWeight="medium"
            flexShrink={0}
            title={sandboxEnabled
              ? "Commands are confined to the working directory — access outside it needs approval"
              : "Commands can reach the whole filesystem without confinement"}
            onClick={() => onSandboxEnabledChange?.(!sandboxEnabled)}
            disabled={!onSandboxEnabledChange}
          >
            {sandboxAppearance.label}
          </Button>
        </Flex>
        <Flex align="center" justify="flex-start" gap={1.5} flex={{ base: "1 1 100%", md: "0 1 auto" }} minW={0}>
          <IconButton
            aria-label="Browse folder"
            size="xs"
            variant="ghost"
            borderRadius="sm"
            minW="28px"
            h="28px"
            flexShrink={0}
            disabled={folderLocked}
            onClick={onBrowseFolder}
          >
            <LuFolder size={16} />
          </IconButton>
          <Menu.Root>
            <Menu.Trigger asChild>
              <Button
                size="xs"
                variant="outline"
                borderRadius="sm"
                fontSize="xs"
                fontWeight="medium"
                h="28px"
                px={2}
                justifyContent="space-between"
                borderColor={directoryValid ? "border" : "red.muted"}
                bg="bg"
                w={{ base: "min(100%, 220px)", md: "180px" }}
                maxW="100%"
                minW={0}
                disabled={folderLocked}
                title={folderLocked
                  ? `Project folder is fixed for this session to ${currentDirectory}`
                  : currentDirectory || "Choose project"}
              >
                <Box as="span" truncate>
                  {currentProjectName}
                </Box>
                {!folderLocked && <LuChevronDown size={14} />}
              </Button>
            </Menu.Trigger>
            <Portal>
              <Menu.Positioner>
                <Menu.Content borderRadius="sm" minW="220px" maxW="360px">
                  {projectItems.map((project) => (
                    <Menu.Item
                      value={project.path}
                      key={project.path}
                      title={project.path}
                      whiteSpace="nowrap"
                      onClick={() => onWorkingDirectoryChange?.(project.path)}
                    >
                      <Box truncate>{project.name}</Box>
                    </Menu.Item>
                  ))}
                  {projectItems.length > 0 && <Menu.Separator />}
                  <Menu.Item
                    value="open-another-project"
                    bg="blue.subtle"
                    color="blue.fg"
                    fontWeight="semibold"
                    _hover={{ bg: "blue.muted" }}
                    onClick={onBrowseFolder}
                  >
                    Open another project...
                  </Menu.Item>
                </Menu.Content>
              </Menu.Positioner>
            </Portal>
          </Menu.Root>
        </Flex>
      </Flex>

      <SettingsDialog open={settingsOpen} onOpenChange={setSettingsOpen} />
    </Box>
  );
}
