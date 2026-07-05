"use client";

import {
  Box,
  Button,
  createListCollection,
  Flex,
  Menu,
  Portal,
  Select,
  Text,
  Textarea,
} from "@chakra-ui/react";
import { AnimatePresence, motion } from "motion/react";
import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { LuAppWindow, LuArrowUp, LuBrain, LuCheck, LuChevronDown, LuChevronLeft, LuChevronRight, LuCircle, LuCoins, LuFolder, LuFoldVertical, LuGitBranch, LuGitFork, LuHardDrive, LuHistory, LuLock, LuLockOpen, LuNetwork, LuPaperclip, LuScan, LuSettings, LuShield, LuShieldCheck, LuShieldOff, LuSquare, LuTriangleAlert, LuUser, LuX, LuZap } from "react-icons/lu";
import { fetchMessageHistory, saveMessageHistory, uploadResearchFile, validateWorkingDirectory, type ModelOption, type PermissionMode, type ProviderOption, type ResearchUpload } from "@/lib/api";
import { AttachmentChip } from "./attachment-chips";
import { Tooltip } from "./ui/tooltip";
import { ModelSelect, modelSupportsAttachments } from "./model-select";
import { ConnectionSwitcher } from "./connection-switcher";
import { SettingsDialog } from "./settings-dialog";
import type { ChatTask, TokenUsage } from "@/lib/use-chat";
import { InlineField } from "./tool-views/primitives";

const MotionFlex = motion.create(Flex);

interface ChatInputProps {
  onSend: (text: string, dataPart?: Record<string, unknown>) => void;
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
  workspaceStrategy?: "none" | "branch" | "worktree";
  workspaceBranch?: string;
  workspaceRuntimeDirectory?: string;
  workspaceRuntimeDirectoryName?: string;
  workspaceError?: string;
  onWorkspaceStrategyChange?: (strategy: "none" | "branch" | "worktree") => void;
  agents: { id: string; name: string; title?: string }[];
  selectedAgent: string;
  onAgentChange: (agent: string) => void;
  permissionMode: PermissionMode;
  onPermissionModeChange: (mode: PermissionMode) => void;
  agentsCount?: number;
  agentsOpen?: boolean;
  onShowAgents?: () => void;
  previewsCount?: number;
  previewOpen?: boolean;
  onTogglePreview?: () => void;
  historyOpen?: boolean;
  onToggleHistory?: () => void;
  models: ModelOption[];
  modelProviders: ProviderOption[];
  recentModels?: { id: string; name: string; provider: string }[];
  selectedModel: string;
  // The globally-selected model, shown on the chip when no per-conversation
  // override is set (selectedModel is "").
  globalModel?: string;
  onModelChange: (model: string) => void;
  // A live "Thinking" / "Working through N tool calls" label shown as a compact
  // chip next to the Settings button while a turn runs and no tool group is
  // active (otherwise the status lives in the active group's header). null hides it.
  thinkingLabel?: string | null;
  // Running token totals for the session, summed from the model's reported usage.
  // Null until the first turn reports usage.
  tokenUsage?: TokenUsage | null;
  // Compact the conversation now (summarize the older history). Shown once a
  // session has real context to compact.
  onCompact?: () => void;
  // How many of the most recent user turns are kept verbatim during compaction
  // (from the server's _COMPACTION_KEEP_RECENT_TURNS). The button is available
  // once there are more user messages than this threshold.
  compactionKeepRecentTurns: number;
  // How many user messages exist in the current session. Used together with
  // compactionKeepRecentTurns to decide whether compaction would be meaningful.
  compactionUserCount: number;
}

// A filling circle for how full the model's context window is. The arc grows with
// the fill fraction and shifts colour as it approaches the limit (blue -> amber ->
// red) so a nearly-full context reads at a glance. Sized 13px to match the icons.
function ContextFillRing({ fraction }: { fraction: number }) {
  const clamped = Math.max(0, Math.min(1, fraction));
  const radius = 5.5;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference * (1 - clamped);
  const stroke = clamped >= 0.9
    ? "var(--chakra-colors-red-solid)"
    : clamped >= 0.75
      ? "var(--chakra-colors-orange-solid)"
      : "var(--chakra-colors-blue-solid)";
  return (
    <Box w="13px" h="13px" flexShrink={0} display="flex" alignItems="center" justifyContent="center">
      <svg width="13" height="13" viewBox="0 0 14 14">
        <circle cx="7" cy="7" r={radius} fill="none" stroke="var(--chakra-colors-bg-muted)" strokeWidth="2" />
        <circle
          cx="7"
          cy="7"
          r={radius}
          fill="none"
          stroke={stroke}
          strokeWidth="2"
          strokeLinecap="round"
          strokeDasharray={circumference}
          strokeDashoffset={offset}
          transform="rotate(-90 7 7)"
        />
      </svg>
    </Box>
  );
}

// The context-usage chip: a fill ring + percent, then the current context size
// against the model's window. It reflects the latest exchange actually occupying
// the window (not the cumulative session sum, which lives in the tooltip).
function ContextUsageChip({ tokenUsage }: { tokenUsage?: TokenUsage | null }) {
  if (!tokenUsage || tokenUsage.contextTokens <= 0) return null;
  const hasContext = tokenUsage.contextWindow > 0;
  const contextFraction = hasContext ? tokenUsage.contextTokens / tokenUsage.contextWindow : 0;
  const contextPercent = Math.min(100, Math.round(contextFraction * 100));
  const tooltipContent = (
    <Box fontSize="xs" lineHeight="1.6" whiteSpace="nowrap">
      <Text fontWeight="semibold" mb={1} color="fg">
        Session totals
      </Text>
      <Flex direction="column" ps={3} gap={0.5}>
        <InlineField label="Input"><Text>{tokenUsage.inputTokens.toLocaleString()}</Text></InlineField>
        <InlineField label="Output"><Text>{tokenUsage.outputTokens.toLocaleString()}</Text></InlineField>
        <InlineField label="Total"><Text>{tokenUsage.totalTokens.toLocaleString()}</Text></InlineField>
        {tokenUsage.cacheReadTokens > 0 && (
          <InlineField label="Cache reads"><Text>{tokenUsage.cacheReadTokens.toLocaleString()}</Text></InlineField>
        )}
        {tokenUsage.reasoningTokens > 0 && (
          <InlineField label="Reasoning"><Text>{tokenUsage.reasoningTokens.toLocaleString()}</Text></InlineField>
        )}
        <InlineField label="Model calls"><Text>{tokenUsage.modelCalls}</Text></InlineField>
      </Flex>
      <Box h="1px" bg="border" my={2} />
      <Text fontWeight="semibold" mb={1} color="fg">
        Context (this turn)
      </Text>
      <Flex direction="column" ps={3} gap={0.5}>
        <InlineField label="Input"><Text>{tokenUsage.contextInputTokens.toLocaleString()}</Text></InlineField>
        <InlineField label="Output"><Text>{tokenUsage.contextOutputTokens.toLocaleString()}</Text></InlineField>
        {hasContext && (
          <InlineField label="Window"><Text>{tokenUsage.contextWindow.toLocaleString()}</Text></InlineField>
        )}
      </Flex>
    </Box>
  );
  return (
    <Tooltip
      content={tooltipContent}
      contentProps={{ p: 3, bg: "bg", color: "fg", borderRadius: "sm", boxShadow: "lg", border: "1px solid", borderColor: "border" }}
      openDelay={200}
      closeDelay={60}
      positioning={{ placement: "top" }}
    >
      <Flex
        align="center"
        gap={1.5}
        h="28px"
        px={2}
        borderRadius="sm"
        border="1px solid"
        borderColor="border"
        bg="bg"
        color="fg.subtle"
        flexShrink={0}
      >
        {hasContext && (
          <>
            <ContextFillRing fraction={contextFraction} />
            <Text fontSize="xs" fontWeight="medium" whiteSpace="nowrap">
              {contextPercent}%
            </Text>
            <Box w="1px" h="14px" bg="border" flexShrink={0} />
          </>
        )}
        <Box display="flex" alignItems="center" flexShrink={0}>
          <LuCoins size={13} />
        </Box>
        <Text fontSize="xs" fontWeight="medium" whiteSpace="nowrap">
          {tokenUsage.contextTokens.toLocaleString()}
          {hasContext ? ` / ${tokenUsage.contextWindow.toLocaleString()}` : ""}
        </Text>
      </Flex>
    </Tooltip>
  );
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
  workspaceStrategy = "none",
  workspaceBranch = "",
  workspaceRuntimeDirectory = "",
  workspaceRuntimeDirectoryName = "",
  workspaceError = "",
  onWorkspaceStrategyChange,
  agents,
  selectedAgent,
  onAgentChange,
  permissionMode = "default",
  onPermissionModeChange,
  agentsCount = 0,
  agentsOpen = false,
  onShowAgents,
  previewsCount = 0,
  previewOpen = false,
  onTogglePreview,
  historyOpen = false,
  onToggleHistory,
  models,
  modelProviders,
  recentModels = [],
  selectedModel,
  globalModel = "",
  onModelChange,
  thinkingLabel,
  tokenUsage,
  onCompact,
  compactionKeepRecentTurns,
  compactionUserCount,
}: ChatInputProps) {
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [inputValue, setInputValue] = useState("");
  const [attachments, setAttachments] = useState<ResearchUpload[]>([]);
  const [uploadingCount, setUploadingCount] = useState(0);
  const [dragActive, setDragActive] = useState(false);
  const [historyIndex, setHistoryIndex] = useState(-1);
  const [messageHistory, setMessageHistory] = useState<string[]>([]);
  const draftInputRef = useRef("");
  const [configExpanded, setConfigExpanded] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [displayedThinkingLabel, setDisplayedThinkingLabel] = useState(thinkingLabel ?? "");
  const [thinkingVisible, setThinkingVisible] = useState(!!thinkingLabel);
  const [directoryState, setDirectoryState] = useState({
    path: workingDirectory ?? "",
    valid: false,
    isGitRepository: false,
    repositoryRoot: "",
    checking: !!workingDirectory,
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
        { label: "User-configured permissions", value: "default" },
        { label: "Auto-classify permissions", value: "auto" },
        { label: "Read-only permissions", value: "read_only" },
        { label: "Bypass permissions", value: "bypass" },
      ],
    }),
    []
  );
  const workspaceCollection = useMemo(
    () => createListCollection({
      items: [
        { label: "Unmanaged", value: "none" },
        { label: "Branch", value: "branch" },
        { label: "Worktree", value: "worktree" },
      ],
    }),
    []
  );
  const permissionAppearance = {
    default: {
      icon: <LuShield size={13} />,
      color: "fg.subtle",
      bg: "bg",
      borderColor: "border",
      colorPalette: undefined,
    },
    auto: {
      icon: <LuZap size={13} />,
      color: "blue.fg",
      bg: "blue.subtle",
      borderColor: "blue.muted",
      colorPalette: "blue",
    },
    read_only: {
      icon: <LuShieldCheck size={13} />,
      color: "green.fg",
      bg: "green.subtle",
      borderColor: "green.muted",
      colorPalette: "green",
    },
    bypass: {
      icon: <LuShieldOff size={13} />,
      color: "red.fg",
      bg: "red.subtle",
      borderColor: "red.muted",
      colorPalette: "red",
    },
  }[permissionMode] ?? {
    icon: <LuShield size={13} />,
    color: "fg.subtle",
    bg: "bg",
    borderColor: "border",
    colorPalette: undefined,
  };
  // Short label for the composer's permission chip — the full descriptive labels
  // ("User-configured permissions", …) stay in the dropdown, but the trigger only
  // needs a word so the bottom row doesn't sprawl and wrap.
  const permissionShortLabel = {
    default: "Default",
    auto: "Auto",
    read_only: "Read-only",
    bypass: "Bypass",
  }[permissionMode] ?? "Default";
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
  const workspaceChoices: { value: "none" | "branch" | "worktree"; label: string; icon: ReactNode; colorPalette?: "purple" | "green" }[] = [
    { value: "none", label: "Unmanaged", icon: <LuHardDrive size={13} /> },
    { value: "branch", label: "Branch", icon: <LuGitBranch size={13} />, colorPalette: "purple" },
    { value: "worktree", label: "Worktree", icon: <LuGitFork size={13} />, colorPalette: "green" },
  ];
  const workspaceDetail =
    workspaceStrategy === "branch" || workspaceStrategy === "worktree"
      ? {
          label: workspaceBranch || workspaceRuntimeDirectoryName || workspaceRuntimeDirectory || workspaceStrategy,
          title: [
            workspaceStrategy === "worktree" ? "Session worktree" : "Session branch",
            workspaceBranch,
            workspaceRuntimeDirectoryName,
            workspaceRuntimeDirectory,
            workspaceError,
          ].filter(Boolean).join("\n"),
          icon: workspaceStrategy === "worktree" ? <LuGitFork size={13} /> : <LuGitBranch size={13} />,
          colorPalette: workspaceStrategy === "worktree" ? "green" as const : "purple" as const,
        }
      : null;

  const historyAppearance = historyOpen
    ? {
        variant: "solid" as const,
        colorPalette: "blue" as const,
        bg: undefined,
        borderColor: undefined,
      }
    : {
        variant: "outline" as const,
        colorPalette: undefined,
        bg: "bg",
        borderColor: "border.emphasized",
      };
  const agentsAppearance = agentsOpen || agentsCount > 0
    ? {
        variant: "solid" as const,
        colorPalette: "orange" as const,
        bg: undefined,
        borderColor: undefined,
      }
    : {
        variant: "outline" as const,
        colorPalette: undefined,
        bg: "bg",
        borderColor: "border.emphasized",
      };
  const previewAppearance = previewOpen || previewsCount > 0
    ? {
        variant: "solid" as const,
        colorPalette: "teal" as const,
        bg: undefined,
        borderColor: undefined,
      }
    : {
        variant: "outline" as const,
        colorPalette: undefined,
        bg: "bg",
        borderColor: "border.emphasized",
      };

  // The composer's file-attach affordance is gated on the selected model's
  // capabilities (models.dev): a text-only model cannot process attachments, so
  // offering to attach is misleading. Falls back to the global default when there
  // is no per-conversation override; unknown/custom models are not blocked.
  const effectiveModelId = selectedModel || globalModel;
  const attachmentsSupported = modelSupportsAttachments(models, effectiveModelId);

  const currentDirectory = (workingDirectory ?? "").trim();
  const directoryStateMatchesCurrent = directoryState.path === currentDirectory;
  const directoryValid = !!currentDirectory && directoryStateMatchesCurrent && directoryState.valid;
  // A session is bound to the folder it was started in: once it exists, the
  // project can no longer be changed, so the selector and browse are locked.
  const folderLocked = !!sessionId;
  // The workspace strategy decides how that first turn prepares the session.
  // Changing it after the session exists would desynchronize the UI setting from
  // the checkout/branch/worktree the backend already created.
  const workspaceLocked = !!sessionId;
  const gitWorkspaceAvailable = directoryStateMatchesCurrent && directoryState.valid && directoryState.isGitRepository;
  const gitWorkspaceUnavailable = directoryStateMatchesCurrent && directoryState.valid && !directoryState.checking && !directoryState.isGitRepository;
  const gitWorkspaceUnavailableLabel = directoryState.valid
    ? "Branch and worktree sessions require the selected folder to be inside a Git repository."
    : "Choose a valid working directory before using branch or worktree sessions.";
  const displayedWorkspaceStrategy =
    !workspaceLocked && gitWorkspaceUnavailable && workspaceStrategy !== "none" ? "none" : workspaceStrategy;
  const selectedWorkspaceChoice =
    workspaceChoices.find((choice) => choice.value === displayedWorkspaceStrategy) ?? workspaceChoices[0];
  const workspaceAppearance = {
    none: { icon: <LuHardDrive size={13} />, color: "fg.subtle", bg: "bg", borderColor: "border", colorPalette: undefined },
    branch: { icon: <LuGitBranch size={13} />, color: "purple.fg", bg: "purple.subtle", borderColor: "purple.muted", colorPalette: "purple" },
    worktree: { icon: <LuGitFork size={13} />, color: "green.fg", bg: "green.subtle", borderColor: "green.muted", colorPalette: "green" },
  }[displayedWorkspaceStrategy];
  const workspaceStrategyValid = workspaceLocked || workspaceStrategy === "none" || gitWorkspaceAvailable;
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
        setDirectoryState({ path: currentDirectory, valid: false, isGitRepository: false, repositoryRoot: "", checking: false });
        return;
      }
      setDirectoryState({ path: currentDirectory, valid: false, isGitRepository: false, repositoryRoot: "", checking: true });
      validateWorkingDirectory(currentDirectory)
        .then((result) => {
          if (!cancelled) {
            setDirectoryState({
              path: currentDirectory,
              valid: result.valid,
              isGitRepository: result.is_git_repository,
              repositoryRoot: result.repository_root,
              checking: false,
            });
          }
        })
        .catch(() => {
          if (!cancelled) {
            setDirectoryState({ path: currentDirectory, valid: false, isGitRepository: false, repositoryRoot: "", checking: false });
          }
        });
    }, 250);

    return () => {
      cancelled = true;
      window.clearTimeout(timeout);
    };
  }, [currentDirectory]);

  useEffect(() => {
    if (workspaceLocked || workspaceStrategy === "none" || directoryState.checking) return;
    if (directoryState.path !== currentDirectory || !directoryState.valid || directoryState.isGitRepository) return;
    onWorkspaceStrategyChange?.("none");
  }, [
    currentDirectory,
    directoryState.checking,
    directoryState.isGitRepository,
    directoryState.path,
    directoryState.valid,
    onWorkspaceStrategyChange,
    workspaceLocked,
    workspaceStrategy,
  ]);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  // Auto-resize the textarea as the user types, so the input grows with its
  // content up to the configured maximum height.
  useEffect(() => {
    const textarea = inputRef.current;
    if (!textarea) return;
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 180)}px`;
  }, [inputValue]);

  useEffect(() => {
    if (thinkingLabel) {
      const showTimer = window.setTimeout(() => {
        setDisplayedThinkingLabel(thinkingLabel);
        setThinkingVisible(true);
      }, 0);
      return () => window.clearTimeout(showTimer);
    }
    const hideTimer = window.setTimeout(() => setThinkingVisible(false), 180);
    const clearTimer = window.setTimeout(() => setDisplayedThinkingLabel(""), 520);
    return () => {
      window.clearTimeout(hideTimer);
      window.clearTimeout(clearTimer);
    };
  }, [thinkingLabel]);

  // Fetch message history when the working directory changes.
  useEffect(() => {
    if (!workingDirectory) {
      setMessageHistory([]);
      return;
    }
    fetchMessageHistory(workingDirectory).then(setMessageHistory).catch(() => {});
  }, [workingDirectory]);

  async function handleFiles(files: FileList | File[]) {
    const selected = Array.from(files);
    if (selected.length === 0) return;
    setUploadingCount((current) => current + selected.length);
    for (const file of selected) {
      try {
        const uploaded = await uploadResearchFile(file);
        setAttachments((current) => [...current, uploaded]);
      } catch {
        // The send button stays disabled only while uploads are in flight; a failed
        // upload simply does not become an attachment.
      } finally {
        setUploadingCount((current) => Math.max(0, current - 1));
      }
    }
  }

  function removeAttachment(uploadId: string) {
    setAttachments((current) => current.filter((attachment) => attachment.upload_id !== uploadId));
  }

  function handleSubmit() {
    const trimmed = inputValue.trim();
    if (!trimmed && attachments.length === 0) return;
    if (!directoryValid) return;
    if (!workspaceStrategyValid) return;
    if (uploadingCount > 0) return;
    const sendText = trimmed || "Use the attached research source(s).";
    const dataPart = attachments.length > 0
      ? {
          kind: "research_attachments",
          attachments,
          sources: attachments.map((attachment) => attachment.source),
        }
      : undefined;
    // While the agent is busy this enqueues for the next turn (handled upstream).
    onSend(sendText, dataPart);
    setHistoryIndex(-1);
    setInputValue("");
    setAttachments([]);
    // Persist to backend and prepend to local list for immediate recall.
    if (trimmed) {
      setMessageHistory((previous) => [trimmed, ...previous]);
      if (workingDirectory) {
        saveMessageHistory(workingDirectory, trimmed).catch(() => {});
      }
    }
  }

  function handleKeyDown(event: KeyboardEvent) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      handleSubmit();
      return;
    }
    if (event.key === "ArrowUp" && messageHistory.length > 0 && inputRef.current?.selectionStart === 0) {
      event.preventDefault();
      // Save the current draft when first navigating up, so it can be
      // restored when the user navigates back down past all history items.
      if (historyIndex === -1) {
        draftInputRef.current = inputValue;
      }
      const nextIndex = historyIndex === -1
        ? 0
        : Math.min(messageHistory.length - 1, historyIndex + 1);
      setHistoryIndex(nextIndex);
      setInputValue(messageHistory[nextIndex]);
      return;
    }
    if (event.key === "ArrowDown" && inputRef.current?.selectionStart === inputValue.length) {
      const nextIndex = historyIndex <= 0 ? -1 : historyIndex - 1;
      setHistoryIndex(nextIndex);
      // Restore the saved draft when navigating back to the "no history" position.
      setInputValue(nextIndex === -1 ? draftInputRef.current : messageHistory[nextIndex]);
      event.preventDefault();
      return;
    }
  }

  return (
    <Box borderTop="1px solid" borderColor="border" bg="bg.subtle" position="relative">
      {/* Top row (above the input): history button on the left, Agents button
          on the right. The Agents button is always shown; the panel renders an
          empty state when there is no activity yet. */}
      <Flex justify="space-between" align="center" rowGap={1.5} gap={{ base: 1.5 }} flexWrap="wrap" px={2} pt={2}>
        <Flex align="center" gap={{ base: 1.5 }} flexShrink={0}>
          {onToggleHistory && (
            <Button
              size="xs"
              variant={historyAppearance.variant}
              colorPalette={historyAppearance.colorPalette}
              borderRadius="sm"
              fontSize="xs"
              h="28px"
              px={2}
              bg={historyAppearance.bg}
              borderColor={historyAppearance.borderColor}
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
            px={2}
            bg="bg"
            borderColor="border"
            flexShrink={0}
            onClick={() => setSettingsOpen(true)}
          >
            <LuSettings size={13} />
            Settings
          </Button>
          <AnimatePresence initial={false}>
            {displayedThinkingLabel && (
              <MotionFlex
                key="thinking-status"
                align="center"
                gap={1.5}
                h="28px"
                px={2}
                borderRadius="sm"
                bg="bg"
                border="1px solid"
                borderColor="border"
                flexShrink={0}
                overflow="hidden"
                initial={{ opacity: 0, y: 2, width: 0 }}
                animate={{ opacity: thinkingVisible ? 1 : 0, y: thinkingVisible ? 0 : 2, width: thinkingVisible ? "auto" : 0 }}
                exit={{ opacity: 0, y: 2, width: 0 }}
                transition={{ duration: 0.2, ease: [0.4, 0, 0.2, 1] }}
              >
                <Box color="purple.fg" display="flex" alignItems="center" flexShrink={0}>
                  <LuBrain size={13} />
                </Box>
                <Text fontSize="xs" fontWeight="medium" className="running-title-shimmer" whiteSpace="nowrap">
                  {displayedThinkingLabel}
                </Text>
              </MotionFlex>
            )}
          </AnimatePresence>
        </Flex>

        <Flex align="center" gap={1.5} flexShrink={0} flexWrap="wrap" justify="flex-end">
          <Button
            size="xs"
            variant="ghost"
            borderRadius="sm"
            flexShrink={0}
            w="28px"
            h="28px"
            minW={0}
            p={0}
            onClick={() => setConfigExpanded((current) => !current)}
          >
            {configExpanded ? <LuChevronRight size={14} /> : <LuChevronLeft size={14} />}
          </Button>
          <AnimatePresence initial={false}>
            {configExpanded && (
              <motion.div
                key="config-controls"
                initial={{ opacity: 0, width: 0 }}
                animate={{ opacity: 1, width: "auto" }}
                exit={{ opacity: 0, width: 0 }}
                transition={{ duration: 0.2, ease: [0.4, 0, 0.2, 1] }}
                style={{ overflow: "hidden", display: "flex", alignItems: "center", gap: "6px" }}
              >
          <Select.Root
            collection={agentCollection}
            value={[selectedAgent]}
            onValueChange={(details) => {
              if (details.value[0]) onAgentChange(details.value[0]);
            }}
            size="sm"
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
                gap={1.5}
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
                <Box display="flex" alignItems="center" color="fg.muted" flexShrink={0}>
                  <LuUser size={13} />
                </Box>
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
                    <Select.Item item={item} key={item.value} whiteSpace="nowrap" fontWeight="medium" fontSize="xs">
                      {item.label}
                      <Select.ItemIndicator />
                    </Select.Item>
                  ))}
                </Select.Content>
              </Select.Positioner>
            </Portal>
          </Select.Root>
          <ModelSelect
            models={models}
            providers={modelProviders}
            recent={recentModels}
            value={selectedModel}
            onChange={onModelChange}
            fallbackModelId={globalModel}
            compact
          />
              </motion.div>
            )}
          </AnimatePresence>
          <ContextUsageChip tokenUsage={tokenUsage} />
          {onCompact && !!sessionId && !!tokenUsage && tokenUsage.contextTokens > 0 && compactionUserCount > compactionKeepRecentTurns && (
            <Button
              size="xs"
              variant="outline"
              borderRadius="sm"
              fontSize="xs"
              h="28px"
              px={2}
              bg="bg"
              borderColor="border"
              flexShrink={0}
              disabled={isStreaming}
              onClick={onCompact}
              title={`Summarize the older history to free up context, keeping the most recent turns verbatim`}
            >
              <LuFoldVertical size={13} />
              Compact
            </Button>
          )}
          <Button
            size="xs"
            variant={agentsAppearance.variant}
            colorPalette={agentsAppearance.colorPalette}
            borderRadius="sm"
            fontSize="xs"
            h="28px"
            px={2}
            bg={agentsAppearance.bg}
            borderColor={agentsAppearance.borderColor}
            flexShrink={0}
            onClick={onShowAgents}
          >
            <LuNetwork size={13} />
            {agentsCount > 0 ? `Agents (${agentsCount})` : "Agents"}
          </Button>
          <Button
            size="xs"
            variant={previewAppearance.variant}
            colorPalette={previewAppearance.colorPalette}
            borderRadius="sm"
            fontSize="xs"
            h="28px"
            px={2}
            bg={previewAppearance.bg}
            borderColor={previewAppearance.borderColor}
            flexShrink={0}
            onClick={onTogglePreview}
            disabled={!onTogglePreview}
          >
            <LuAppWindow size={13} />
            {previewsCount > 0 ? `Preview (${previewsCount})` : "Preview"}
          </Button>
        </Flex>
      </Flex>

      {/* Message input */}
      <Box px={2} pt={2} pb={2}>
        <Box
          bg="bg"
          border="1px solid"
          borderColor={dragActive ? "blue.muted" : directoryValid ? "border" : "red.muted"}
          borderRadius="sm"
          onDragEnter={(event) => {
            event.preventDefault();
            setDragActive(true);
          }}
          onDragOver={(event) => {
            event.preventDefault();
            setDragActive(true);
          }}
          onDragLeave={(event) => {
            event.preventDefault();
            setDragActive(false);
          }}
          onDrop={(event) => {
            event.preventDefault();
            setDragActive(false);
            // Ignore drops when the selected model can't process attachments —
            // matching the disabled attach button.
            if (!attachmentsSupported) return;
            void handleFiles(event.dataTransfer.files);
          }}
          _focusWithin={{ borderColor: "border.emphasized" }}
        >
          {attachments.length > 0 || uploadingCount > 0 ? (
            <Flex gap={1.5} px={2} pt={2} flexWrap="wrap">
              {attachments.map((attachment) => (
                <AttachmentChip
                  key={attachment.upload_id}
                  attachment={{
                    filename: attachment.filename,
                    path: attachment.path,
                    mimeType: attachment.mime_type,
                    size: attachment.size,
                  }}
                  onRemove={() => removeAttachment(attachment.upload_id)}
                />
              ))}
              {uploadingCount > 0 ? (
                <Flex align="center" gap={1.5} px={1.5} py={1} border="1px solid" borderColor="border" borderRadius="sm" bg="bg.subtle">
                  <Box color="blue.fg"><LuPaperclip size={13} /></Box>
                  <Text fontSize="xs" color="fg.subtle">Uploading {uploadingCount}</Text>
                </Flex>
              ) : null}
            </Flex>
          ) : null}
          <Flex align="flex-end" gap={2} px={1.5} py={1.5}>
            <Textarea
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
              minH="72px"
              maxH="180px"
              border="none"
              outline="none"
              px={1}
              py={1}
              resize="none"
              lineHeight="1.45"
              _focus={{ boxShadow: "none", borderColor: "transparent" }}
              _focusVisible={{ boxShadow: "none", outline: "none", borderColor: "transparent" }}
            />
            <Flex gap={1.5} flexShrink={0}>
              <input
                ref={fileInputRef}
                type="file"
                multiple
                hidden
                onChange={(event) => {
                  if (event.target.files) void handleFiles(event.target.files);
                  event.target.value = "";
                }}
              />
              <Button
                onClick={() => fileInputRef.current?.click()}
                variant="subtle"
                colorPalette="gray"
                borderRadius="sm"
                minW="70px"
                h="32px"
                px={2}
                gap={1.5}
                fontSize="xs"
                fontWeight="medium"
                disabled={disabled || !directoryValid || !attachmentsSupported}
                title={!attachmentsSupported ? "The selected model can't process file attachments — switch to a vision or file-capable model" : undefined}
              >
                <LuPaperclip size={14} />
                Attach files
              </Button>
              {isStreaming ? (
                <Button
                  onClick={onAbort}
                  colorPalette="red"
                  variant="solid"
                  borderRadius="sm"
                  minW="70px"
                  h="32px"
                  px={2}
                  gap={1.5}
                  fontSize="xs"
                  fontWeight="medium"
                >
                  <LuSquare size={14} />
                  Stop
                </Button>
              ) : (
                <Button
                  onClick={handleSubmit}
                  colorPalette="blue"
                  variant="solid"
                  borderRadius="sm"
                  minW="70px"
                  h="32px"
                  px={2}
                  gap={1.5}
                  fontSize="xs"
                  fontWeight="medium"
                  disabled={disabled || !directoryValid || !workspaceStrategyValid || uploadingCount > 0 || (!inputValue.trim() && attachments.length === 0)}
                >
                  <LuArrowUp size={16} />
                  Send
                </Button>
              )}
            </Flex>
          </Flex>
        </Box>
      </Box>

      {/* Bottom row (below the input): connection, permission, sandbox, and project controls. */}
      <Flex justify="flex-start" align="center" rowGap={1.5} columnGap={2} flexWrap="wrap" px={2} pb={2}>
        <Flex align="center" gap={2} flexWrap="wrap" flexShrink={0}>
          <ConnectionSwitcher />
          <Select.Root
            collection={permissionCollection}
            value={[permissionMode]}
            onValueChange={(details) => {
              const nextMode = details.value[0] as PermissionMode | undefined;
              if (nextMode) onPermissionModeChange(nextMode);
            }}
            size="sm"
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
                gap={1.5}
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
                <Box display="flex" alignItems="center" color={permissionAppearance.color} flexShrink={0}>
                  {permissionAppearance.icon}
                </Box>
                <Text fontSize="xs" fontWeight="medium" whiteSpace="nowrap">
                  {permissionShortLabel}
                </Text>
              </Select.Trigger>
              <Select.IndicatorGroup>
                <Select.Indicator />
              </Select.IndicatorGroup>
            </Select.Control>
            <Portal>
              <Select.Positioner>
                <Select.Content borderRadius="sm" minW="max-content" w="max-content">
                  {permissionCollection.items.map((item) => (
                    <Select.Item item={item} key={item.value} whiteSpace="nowrap" fontWeight="medium" fontSize="xs">
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
            variant={sandboxAppearance.variant}
            colorPalette={sandboxAppearance.colorPalette}
            borderRadius="sm"
            fontSize="xs"
            h="28px"
            px={2}
            fontWeight="medium"
            flexShrink={0}
            title={sandboxEnabled
              ? "Commands are confined to the working directory — access outside it needs approval"
              : "Commands can reach the whole filesystem without confinement"}
            onClick={() => onSandboxEnabledChange?.(!sandboxEnabled)}
            disabled={!onSandboxEnabledChange}
          >
            <Box display="flex" alignItems="center">
              {sandboxEnabled ? <LuLock size={13} /> : <LuLockOpen size={13} />}
            </Box>
            {sandboxAppearance.label}
          </Button>
          <Flex align="center" gap={1.5} flexWrap="wrap">
            <Select.Root
              collection={workspaceCollection}
              value={[displayedWorkspaceStrategy]}
              onValueChange={(details) => {
                const nextStrategy = details.value[0] as "none" | "branch" | "worktree" | undefined;
                if (nextStrategy) onWorkspaceStrategyChange?.(nextStrategy);
              }}
              size="sm"
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
                  gap={1.5}
                  px={2}
                  pe={7}
                  bg={workspaceAppearance.bg}
                  border="1px solid"
                  borderColor={workspaceAppearance.borderColor}
                  colorPalette={workspaceAppearance.colorPalette}
                  minW="max-content"
                  maxW="none"
                  whiteSpace="nowrap"
                  fontWeight="medium"
                  disabled={workspaceLocked}
                  title={
                    workspaceLocked
                      ? workspaceDetail?.title ?? "Workspace strategy for this session"
                      : directoryState.repositoryRoot && displayedWorkspaceStrategy !== "none"
                        ? directoryState.repositoryRoot
                        : "Session workspace strategy"
                  }
                  style={{ height: "28px", minHeight: "28px", lineHeight: "28px" }}
                >
                  <Box display="flex" alignItems="center" color={workspaceAppearance.color} flexShrink={0}>
                    {workspaceAppearance.icon}
                  </Box>
                  <Select.ValueText maxW="none" overflow="visible" textOverflow="clip" whiteSpace="nowrap" />
                </Select.Trigger>
                <Select.IndicatorGroup>
                  <Select.Indicator />
                </Select.IndicatorGroup>
              </Select.Control>
              <Portal>
                <Select.Positioner>
                  <Select.Content borderRadius="sm" minW="max-content" w="max-content">
                    {workspaceCollection.items.map((item) => {
                      const gitModeUnavailable = item.value !== "none" && !gitWorkspaceAvailable;
                      const choice = workspaceChoices.find((choice) => choice.value === item.value);
                      return (
                        <Select.Item item={item} key={item.value} whiteSpace="nowrap" fontWeight="medium" fontSize="xs" aria-disabled={gitModeUnavailable || undefined} data-disabled={gitModeUnavailable ? "" : undefined} opacity={gitModeUnavailable ? 0.4 : undefined} pointerEvents={gitModeUnavailable ? "none" : undefined}>
                          <Flex align="center" gap={1.5}>
                            {choice?.icon}
                            <Text>{item.label}</Text>
                          </Flex>
                          <Select.ItemIndicator />
                        </Select.Item>
                      );
                    })}
                  </Select.Content>
                </Select.Positioner>
              </Portal>
            </Select.Root>
            {!workspaceLocked && gitWorkspaceUnavailable && (
              <Flex
                align="center"
                gap={1.5}
                h="28px"
                px={2}
                borderRadius="sm"
                border="1px solid"
                borderColor="orange.muted"
                bg="orange.subtle"
                color="orange.fg"
                flexShrink={0}
                maxW={{ base: "100%", md: "300px" }}
                title={gitWorkspaceUnavailableLabel}
              >
                <Box display="flex" alignItems="center" flexShrink={0}>
                  <LuTriangleAlert size={13} />
                </Box>
                <Text fontSize="xs" fontWeight="medium" truncate>
                  Unconfigured workspace
                </Text>
              </Flex>
            )}
          </Flex>
        </Flex>
        <Flex align="center" justify="flex-start" gap={1.5} flex={{ base: "1 1 100%", md: "0 1 auto" }} minW={0}>
          {!folderLocked && (
            <Button
              size="xs"
              variant="ghost"
              borderRadius="sm"
              h="28px"
              w="28px"
              minW={0}
              px={0}
              flexShrink={0}
              title="Open folder"
              onClick={onBrowseFolder}
            >
              <LuFolder size={13} />
            </Button>
          )}
          {!folderLocked ? (
          <Menu.Root size="sm">
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
                <Menu.Content borderRadius="sm" minW="max-content" maxW="320px">
                  {projectItems.map((project) => (
                    <Menu.Item
                      value={project.path}
                      key={project.path}
                      title={project.path}
                      whiteSpace="nowrap"
                      fontWeight="medium"
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
                    fontWeight="medium"
                    _hover={{ bg: "blue.muted" }}
                    onClick={onBrowseFolder}
                  >
                    Open another project...
                  </Menu.Item>
                </Menu.Content>
              </Menu.Positioner>
            </Portal>
          </Menu.Root>
          ) : (
            <Flex
              align="center"
              gap={1.5}
              h="28px"
              px={2}
              borderRadius="sm"
              border="1px solid"
              borderColor="border"
              bg="bg"
              color="fg"
              flexShrink={0}
              maxW={{ base: "100%", md: "220px" }}
              title={`Project folder is locked for this session: ${currentDirectory}`}
            >
              <Box display="flex" alignItems="center" flexShrink={0} color="fg.muted">
                <LuFolder size={13} />
              </Box>
              <Text fontSize="xs" fontWeight="medium" truncate>
                {currentProjectName || "Project"}
              </Text>
            </Flex>
          )}
        </Flex>
      </Flex>

      <SettingsDialog open={settingsOpen} onOpenChange={setSettingsOpen} />
    </Box>
  );
}
