"use client";

import {
  Box,
  Button,
  createListCollection,
  Dialog,
  Flex,
  Menu,
  Portal,
  Select,
  Spinner,
  Text,
  Textarea,
} from "@chakra-ui/react";
import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { LuArrowUp, LuBox, LuChevronDown, LuCircleSlash, LuCoins, LuEye, LuFolder, LuFoldVertical, LuGitBranch, LuGitFork, LuGlobe, LuHardDrive, LuPaperclip, LuSlidersHorizontal, LuSquare, LuTriangleAlert, LuUser, LuZap } from "react-icons/lu";
import { fetchMessageHistory, saveMessageHistory, subscribeGitStatus, uploadResearchFile, validateWorkingDirectory, type DirectoryValidation, type ModelOption, type PermissionMode, type ProviderOption, type ResearchUpload } from "@/lib/api";
import { AttachmentChip } from "./attachment-chips";
import { Tooltip } from "./ui/tooltip";
import { ModelSelect, modelSupportsAttachments } from "./model-select";
import { ConnectionSwitcher } from "./connection-switcher";
// SettingsDialog moved to ChatPanel top bar
import type { TokenUsage } from "@/lib/use-chat";
import { InlineField } from "./tool-views/primitives";
import type { ConnectionTarget } from "@/lib/connection";

type WorkspaceStrategyValue = "none" | "branch" | "worktree";

interface ChatInputProps {
  onSend: (text: string, dataPart?: Record<string, unknown>) => void | Promise<void>;
  onAbort: () => void | Promise<void>;
  isStreaming: boolean;
  disabled?: boolean;
  sessionId?: string | null;
  currentConnectionId?: string;
  onConnectionChange?: (target: ConnectionTarget) => void;
  onOpenConnectionSettings: () => void;
  workingDirectory?: string;
  recentProjects?: { path: string; name: string }[];
  onWorkingDirectoryChange?: (dir: string) => void;
  onBrowseFolder?: () => void;
  sandboxEnabled?: boolean;
  onSandboxEnabledChange?: (enabled: boolean) => void;
  workspaceStrategy?: WorkspaceStrategyValue;
  workspaceBranch?: string;
  workspaceRuntimeDirectory?: string;
  workspaceRuntimeDirectoryName?: string;
  workspaceError?: string;
  onWorkspaceStrategyChange?: (strategy: WorkspaceStrategyValue) => void | Promise<void>;
  agents: { id: string; name: string; title?: string }[];
  selectedAgent: string;
  onAgentChange: (agent: string) => void;
  permissionMode: PermissionMode;
  onPermissionModeChange: (mode: PermissionMode) => void;
  models: ModelOption[];
  modelProviders: ProviderOption[];
  recentModels?: { id: string; name: string; provider: string }[];
  selectedModel: string;
  // The globally-selected model, shown on the chip when no per-conversation
  // override is set (selectedModel is "").
  globalModel?: string;
  // The active agent's configured model. The effective model falls back to this
  // before the global default, so the chip and the attachment gate reflect the
  // model the turn will actually run on.
  agentModel?: string;
  onModelChange: (model: string) => void;
  // Running token totals for the session, summed from the model's reported usage.
  // Null until the first turn reports usage.
  tokenUsage?: TokenUsage | null;
  // Compact the conversation now (summarize the older history). Shown once a
  // session has real context to compact.
  onCompact?: () => void;
  // True while a compaction pass is running, so the Compact control reflects the
  // in-progress state (spinner + disabled) rather than inviting another click.
  isCompacting?: boolean;
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
  currentConnectionId,
  onConnectionChange,
  onOpenConnectionSettings,
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
  models,
  modelProviders,
  recentModels = [],
  selectedModel,
  globalModel = "",
  agentModel = "",
  onModelChange,
  tokenUsage,
  onCompact,
  isCompacting = false,
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
  const [sendPending, setSendPending] = useState(false);
  const [stopPending, setStopPending] = useState(false);
  const [compactConfirmOpen, setCompactConfirmOpen] = useState(false);

  const [optimisticWorkspaceStrategy, setOptimisticWorkspaceStrategy] = useState<WorkspaceStrategyValue | null>(null);
  const [directoryState, setDirectoryState] = useState({
    path: workingDirectory ?? "",
    valid: false,
    isGitRepository: false,
    repositoryRoot: "",
    gitBranch: "",
    gitHead: "",
    gitShortHead: "",
    gitDirty: false,
    gitDetached: false,
    gitLabel: "",
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
        { label: "Manual approvals", value: "default" },
        { label: "Auto-classify approvals", value: "auto" },
        { label: "Read-only commands", value: "read_only" },
        { label: "Bypass approvals", value: "bypass" },
      ],
    }),
    []
  );
  const workspaceCollection = useMemo(
    () => createListCollection({
      items: [
        { label: "Current checkout", value: "none" },
        { label: "Create branch from main", value: "branch" },
        { label: "Create worktree from main", value: "worktree" },
      ],
    }),
    []
  );
  const permissionAppearance = {
    default: {
      icon: <LuSlidersHorizontal size={13} />,
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
      icon: <LuEye size={13} />,
      color: "green.fg",
      bg: "green.subtle",
      borderColor: "green.muted",
      colorPalette: "green",
    },
    bypass: {
      icon: <LuCircleSlash size={13} />,
      color: "red.fg",
      bg: "red.subtle",
      borderColor: "red.muted",
      colorPalette: "red",
    },
  }[permissionMode] ?? {
    icon: <LuSlidersHorizontal size={13} />,
    color: "fg.subtle",
    bg: "bg",
    borderColor: "border",
    colorPalette: undefined,
  };
  const permissionChoices: { value: PermissionMode; label: string; description: string; icon: ReactNode; colorPalette?: "blue" | "green" | "red" }[] = [
    { value: "default", label: "Manual approvals", description: "Use the configured permission rules and ask when needed.", icon: <LuSlidersHorizontal size={13} /> },
    { value: "auto", label: "Auto-classify approvals", description: "Let Daisy classify command risk before deciding.", icon: <LuZap size={13} />, colorPalette: "blue" },
    { value: "read_only", label: "Read-only commands", description: "Allow reads, block writes unless explicitly approved.", icon: <LuEye size={13} />, colorPalette: "green" },
    { value: "bypass", label: "Bypass approvals", description: "Run commands without approval prompts.", icon: <LuCircleSlash size={13} />, colorPalette: "red" },
  ];
  const permissionSelectedLabel = permissionCollection.items.find((item) => item.value === permissionMode)?.label ?? "Manual approvals";
  const sandboxAppearance = sandboxEnabled
    ? {
        label: "Restricted access",
        icon: <LuBox size={13} />,
        color: "green.fg",
        bg: "green.subtle",
        borderColor: "green.muted",
      }
    : {
        label: "Global access",
        icon: <LuGlobe size={13} />,
        color: "red.fg",
        bg: "red.subtle",
        borderColor: "red.muted",
      };
  const workspaceChoices: { value: "none" | "branch" | "worktree"; label: string; description: string; title: string; icon: ReactNode; colorPalette?: "purple" | "teal" }[] = [
    {
      value: "none",
      label: "Current checkout",
      description: "Run in the selected folder. No branch or worktree is created",
      title: "Run in the selected checkout without creating a session branch or worktree",
      icon: <LuHardDrive size={13} />,
    },
    {
      value: "branch",
      label: "Create branch from main",
      description: "Create and check out a daisy/session branch from the repo main line",
      title: "Creates a daisy/session branch from the repository main/default branch, not from the current branch",
      icon: <LuGitBranch size={13} />,
      colorPalette: "purple",
    },
    {
      value: "worktree",
      label: "Create worktree from main",
      description: "Create an isolated worktree on a daisy/session branch from the repo main line",
      title: "Creates a separate worktree and daisy/session branch from the repository main/default branch",
      icon: <LuGitFork size={13} />,
      colorPalette: "teal",
    },
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
          colorPalette: workspaceStrategy === "worktree" ? "teal" as const : "purple" as const,
        }
      : null;

  // The composer's file-attach affordance is gated on the selected model's
  // capabilities (models.dev): a text-only model cannot process attachments, so
  // offering to attach is misleading. Falls back to the active agent's configured
  // model, then to the global default, when there is no per-conversation override;
  // unknown/custom models are not blocked.
  const effectiveModelId = selectedModel || agentModel || globalModel;
  const attachmentsSupported = modelSupportsAttachments(models, effectiveModelId);

  const currentDirectory = (workingDirectory ?? "").trim();
  const validationDirectory = ((sessionId && workspaceRuntimeDirectory) ? workspaceRuntimeDirectory : workingDirectory ?? "").trim();
  const directoryStateMatchesCurrent = directoryState.path === validationDirectory;
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
  // Branch and worktree sessions only run inside a Git repository, so outside one the
  // workspace selector is replaced by a warning once validation has confirmed that
  // Git metadata is unavailable.
  const workspaceSelectorHidden = !workspaceLocked && gitWorkspaceUnavailable;
  const gitWorkspaceUnavailableLabel = "Creating a session branch or worktree requires the selected folder to be inside a Git repository.";
  const gitStatusLabel = directoryState.gitLabel
    ? `${directoryState.gitDetached ? "Detached " : ""}${directoryState.gitLabel}${directoryState.gitDirty ? " *" : ""}`
    : "";
  const gitStatusTitle = [
    directoryState.gitDetached ? "Detached HEAD" : directoryState.gitBranch ? "Current branch" : "Git repository",
    directoryState.gitBranch,
    directoryState.gitShortHead ? `Commit ${directoryState.gitShortHead}` : "",
    directoryState.gitDirty ? "Uncommitted changes present" : "Clean working tree",
    directoryState.repositoryRoot,
  ].filter(Boolean).join("\n");
  const displayedWorkspaceStrategy = optimisticWorkspaceStrategy ?? workspaceStrategy;
  const workspaceAppearance = {
    none: { icon: <LuHardDrive size={13} />, color: "fg.subtle", bg: "bg", borderColor: "border", colorPalette: undefined },
    branch: { icon: <LuGitBranch size={13} />, color: "purple.fg", bg: "purple.subtle", borderColor: "purple.muted", colorPalette: "purple" },
    worktree: { icon: <LuGitFork size={13} />, color: "teal.fg", bg: "teal.subtle", borderColor: "teal.muted", colorPalette: "teal" },
  }[displayedWorkspaceStrategy];
  const workspaceStrategyValid = workspaceLocked || displayedWorkspaceStrategy === "none" || gitWorkspaceAvailable;
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
    let unsubscribeGitStatus: (() => void) | null = null;

    function setEmptyDirectoryState(path: string, checking: boolean) {
      setDirectoryState({
        path,
        valid: false,
        isGitRepository: false,
        repositoryRoot: "",
        gitBranch: "",
        gitHead: "",
        gitShortHead: "",
        gitDirty: false,
        gitDetached: false,
        gitLabel: "",
        checking,
      });
    }

    function applyDirectoryValidation(result: DirectoryValidation) {
      setDirectoryState({
        path: validationDirectory,
        valid: result.valid,
        isGitRepository: result.is_git_repository,
        repositoryRoot: result.repository_root,
        gitBranch: result.git_branch,
        gitHead: result.git_head,
        gitShortHead: result.git_short_head,
        gitDirty: result.git_dirty,
        gitDetached: result.git_detached,
        gitLabel: result.git_label,
        checking: false,
      });
    }

    const timeout = window.setTimeout(() => {
      if (!validationDirectory) {
        setEmptyDirectoryState(validationDirectory, false);
        return;
      }
      setEmptyDirectoryState(validationDirectory, true);
      validateWorkingDirectory(validationDirectory)
        .then((result) => {
          if (cancelled) return;
          applyDirectoryValidation(result);
          if (!result.is_git_repository) return;
          unsubscribeGitStatus = subscribeGitStatus(validationDirectory, (status) => {
            if (!cancelled) applyDirectoryValidation(status);
          });
        })
        .catch(() => {
          if (!cancelled) {
            setEmptyDirectoryState(validationDirectory, false);
          }
        });
    }, 250);

    return () => {
      cancelled = true;
      window.clearTimeout(timeout);
      unsubscribeGitStatus?.();
    };
  }, [validationDirectory]);

  useEffect(() => {
    if (workspaceLocked || workspaceStrategy === "none" || directoryState.checking) return;
    if (directoryState.path !== validationDirectory || !directoryState.valid || directoryState.isGitRepository) return;
    onWorkspaceStrategyChange?.("none");
  }, [
    directoryState.checking,
    directoryState.isGitRepository,
    directoryState.path,
    directoryState.valid,
    onWorkspaceStrategyChange,
    validationDirectory,
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

  // Fetch message history when the working directory changes.
  useEffect(() => {
    let cancelled = false;
    if (!workingDirectory) {
      queueMicrotask(() => {
        if (!cancelled) setMessageHistory([]);
      });
      return () => {
        cancelled = true;
      };
    }
    fetchMessageHistory(workingDirectory)
      .then((history) => {
        if (!cancelled) setMessageHistory(history);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
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

  async function handleSubmit() {
    const trimmed = inputValue.trim();
    if (!trimmed && attachments.length === 0) return;
    if (!directoryValid) return;
    if (!workspaceStrategyValid) return;
    if (uploadingCount > 0) return;
    const startedAt = performance.now();
    setSendPending(true);
    const sendText = trimmed || "Use the attached research source(s).";
    const dataPart = attachments.length > 0
      ? {
          kind: "research_attachments",
          attachments,
          sources: attachments.map((attachment) => attachment.source),
        }
      : undefined;
    try {
      // While the agent is busy this enqueues for the next turn (handled upstream).
      await onSend(sendText, dataPart);
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
    } finally {
      window.setTimeout(() => setSendPending(false), Math.max(0, 450 - (performance.now() - startedAt)));
    }
  }

  async function handleAbortClick() {
    const startedAt = performance.now();
    setStopPending(true);
    try {
      await onAbort();
    } finally {
      window.setTimeout(() => setStopPending(false), Math.max(0, 450 - (performance.now() - startedAt)));
    }
  }

  async function handleWorkspaceStrategySelect(nextStrategy: WorkspaceStrategyValue) {
    setOptimisticWorkspaceStrategy(nextStrategy);
    try {
      await onWorkspaceStrategyChange?.(nextStrategy);
    } finally {
      setOptimisticWorkspaceStrategy(null);
    }
  }

  function handleKeyDown(event: KeyboardEvent) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void handleSubmit();
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
      {/* Controls row (above the input): the agent and model selectors are always
          visible here — no collapsible toggle — with the context-usage chip and
          Compact action on the right. History, Agents, Previews, and Settings now
          live in the ChatPanel top bar. */}
      <Flex justify="space-between" align="center" rowGap={1.5} columnGap={2} flexWrap="wrap" px={2} pt={2}>
        <Flex align="center" gap={1.5} flexWrap="wrap" minW={0}>
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
            fallbackModelId={agentModel || globalModel}
            compact
          />
        </Flex>
        <Flex align="center" gap={1.5} flexShrink={0} justify="flex-end">
          {onCompact && !!sessionId && !!tokenUsage && tokenUsage.contextTokens > 0 && (isCompacting || compactionUserCount > compactionKeepRecentTurns) && (
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
              disabled={isStreaming || isCompacting}
              onClick={() => setCompactConfirmOpen(true)}
              title={isCompacting
                ? "Compacting the context…"
                : "Summarize the older history to free up context, keeping the most recent turns verbatim"}
            >
              {isCompacting ? <Spinner size="xs" /> : <LuFoldVertical size={13} />}
              {isCompacting ? "Compacting…" : "Compact"}
            </Button>
          )}
          <ContextUsageChip tokenUsage={tokenUsage} />
        </Flex>
      </Flex>

      <Dialog.Root
        open={compactConfirmOpen}
        onOpenChange={(event) => setCompactConfirmOpen(event.open)}
        placement="center"
        role="alertdialog"
      >
        <Portal>
          <Dialog.Backdrop />
          <Dialog.Positioner>
            <Dialog.Content borderRadius="md">
              <Dialog.Header>
                <Dialog.Title>Compact the context?</Dialog.Title>
              </Dialog.Header>
              <Dialog.Body>
                <Text fontSize="sm" color="fg.muted">
                  This summarizes the older conversation into a compact handoff, keeping the{" "}
                  <b>most recent {compactionKeepRecentTurns} turns</b> verbatim. It frees up the
                  context window but the summarized detail can no longer be recalled in full.
                </Text>
              </Dialog.Body>
              <Dialog.Footer gap={2}>
                <Button size="sm" variant="outline" borderRadius="sm" onClick={() => setCompactConfirmOpen(false)}>
                  Cancel
                </Button>
                <Button
                  size="sm"
                  colorPalette="blue"
                  variant="solid"
                  borderRadius="sm"
                  onClick={() => {
                    setCompactConfirmOpen(false);
                    onCompact?.();
                  }}
                >
                  <LuFoldVertical size={14} />
                  Compact now
                </Button>
              </Dialog.Footer>
            </Dialog.Content>
          </Dialog.Positioner>
        </Portal>
      </Dialog.Root>

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
                <Box display="flex" alignItems="center" justifyContent="center" flexShrink={0}>
                  <LuPaperclip size={14} />
                </Box>
                Attach files
              </Button>
              {isStreaming ? (
                <Button
                  onClick={handleAbortClick}
                  colorPalette="red"
                  variant="solid"
                  borderRadius="sm"
                  minW="70px"
                  h="32px"
                  px={2}
                  gap={1.5}
                  fontSize="xs"
                  fontWeight="medium"
                  loading={stopPending}
                  disabled={stopPending}
                >
                  <Box display="flex" alignItems="center" justifyContent="center" flexShrink={0}>
                    <LuSquare size={14} />
                  </Box>
                  Stop
                </Button>
              ) : (
                <Button
                  onClick={() => void handleSubmit()}
                  colorPalette="blue"
                  variant="solid"
                  borderRadius="sm"
                  minW="70px"
                  h="32px"
                  px={2}
                  gap={1.5}
                  fontSize="xs"
                  fontWeight="medium"
                  loading={sendPending}
                  disabled={sendPending || disabled || !directoryValid || !workspaceStrategyValid || uploadingCount > 0 || (!inputValue.trim() && attachments.length === 0)}
                >
                  <Box display="flex" alignItems="center" justifyContent="center" flexShrink={0}>
                    <LuArrowUp size={14} />
                  </Box>
                  Send
                </Button>
              )}
            </Flex>
          </Flex>
        </Box>
      </Box>

      {/* Bottom row (below the input): connection, permission, sandbox, and project controls. */}
      <Flex justify="flex-start" align="center" rowGap={1.5} columnGap={2} flexWrap="wrap" px={2} pb={2}>
        <Flex align="center" gap={1.5} flexWrap="wrap" flexShrink={0}>
          <ConnectionSwitcher currentTargetId={currentConnectionId} onConnectionChange={onConnectionChange} onOpenConnectionSettings={onOpenConnectionSettings} />
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
                <Box display="flex" alignItems="center" justifyContent="center" w="13px" h="13px" color={permissionAppearance.color} flexShrink={0}>
                  {permissionAppearance.icon}
                </Box>
                <Text fontSize="xs" fontWeight="medium" whiteSpace="nowrap">
                  {permissionSelectedLabel}
                </Text>
              </Select.Trigger>
              <Select.IndicatorGroup>
                <Select.Indicator />
              </Select.IndicatorGroup>
            </Select.Control>
            <Portal>
              <Select.Positioner>
                <Select.Content borderRadius="sm" minW="max-content" w="max-content">
                  {permissionCollection.items.map((item) => {
                    const choice = permissionChoices.find((candidate) => candidate.value === item.value);
                    return (
                      <Select.Item item={item} key={item.value} fontWeight="medium" fontSize="xs">
                        <Flex align="center" gap={2} minW={0}>
                          <Box display="flex" alignItems="center" justifyContent="center" w="13px" h="13px" color={choice?.colorPalette ? `${choice.colorPalette}.fg` : "fg.subtle"} flexShrink={0}>
                            {choice?.icon}
                          </Box>
                          <Flex direction="column" minW={0}>
                            <Text fontSize="xs" fontWeight="medium" whiteSpace="nowrap">
                              {choice?.label ?? item.label}
                            </Text>
                            {choice?.description && (
                              <Text fontSize="2xs" color="fg.muted" lineHeight="1.2">
                                {choice.description}
                              </Text>
                            )}
                          </Flex>
                        </Flex>
                        <Select.ItemIndicator />
                      </Select.Item>
                    );
                  })}
                </Select.Content>
              </Select.Positioner>
            </Portal>
          </Select.Root>
          <Button
            size="xs"
            variant="outline"
            borderRadius="sm"
            fontSize="xs"
            h="28px"
            px={2}
            bg={sandboxAppearance.bg}
            borderColor={sandboxAppearance.borderColor}
            color={sandboxAppearance.color}
            _hover={{ bg: sandboxEnabled ? "green.muted" : "red.muted" }}
            fontWeight="medium"
            flexShrink={0}
            title={sandboxEnabled
              ? "Command filesystem access is confined to the working directory; access outside it needs approval."
              : "Commands can reach the whole filesystem without sandbox confinement."}
            onClick={() => onSandboxEnabledChange?.(!sandboxEnabled)}
            disabled={!onSandboxEnabledChange}
          >
            <Box display="flex" alignItems="center" justifyContent="center" w="13px" h="13px" flexShrink={0}>
              {sandboxAppearance.icon}
            </Box>
            {sandboxAppearance.label}
          </Button>
          <Flex align="center" gap={1.5} flexWrap="wrap">
            {gitStatusLabel && (
              <Flex
                align="center"
                gap={1.5}
                h="28px"
                px={2}
                borderRadius="sm"
                border="1px solid"
                borderColor={directoryState.gitDirty ? "orange.muted" : "green.muted"}
                bg={directoryState.gitDirty ? "orange.subtle" : "green.subtle"}
                color={directoryState.gitDirty ? "orange.fg" : "green.fg"}
                flexShrink={1}
                minW={0}
                maxW={{ base: "100%", md: "260px" }}
                title={gitStatusTitle}
              >
                <Box display="flex" alignItems="center" justifyContent="center" w="13px" h="13px" flexShrink={0}>
                  <LuGitBranch size={13} />
                </Box>
                <Text fontSize="xs" fontWeight="medium" truncate>
                  {gitStatusLabel}
                </Text>
              </Flex>
            )}
            {workspaceSelectorHidden ? (
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
                <Box display="flex" alignItems="center" justifyContent="center" w="13px" h="13px" flexShrink={0}>
                  <LuTriangleAlert size={13} />
                </Box>
                <Text fontSize="xs" fontWeight="medium" truncate>
                  Git repository required for versioning
                </Text>
              </Flex>
            ) : (
              <Select.Root
                collection={workspaceCollection}
                value={[displayedWorkspaceStrategy]}
                onValueChange={(details) => {
                  const nextStrategy = details.value[0] as WorkspaceStrategyValue | undefined;
                  if (nextStrategy) void handleWorkspaceStrategySelect(nextStrategy);
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
                        : workspaceChoices.find((choice) => choice.value === displayedWorkspaceStrategy)?.title ?? "Session isolation strategy"
                    }
                    style={{ height: "28px", minHeight: "28px", lineHeight: "28px" }}
                  >
                    <Box display="flex" alignItems="center" justifyContent="center" w="13px" h="13px" color={workspaceAppearance.color} flexShrink={0}>
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
                          <Select.Item item={item} key={item.value} fontWeight="medium" fontSize="xs" aria-disabled={gitModeUnavailable || undefined} data-disabled={gitModeUnavailable ? "" : undefined} opacity={gitModeUnavailable ? 0.4 : undefined} pointerEvents={gitModeUnavailable ? "none" : undefined}>
                            <Flex align="center" gap={2} minW={0}>
                              <Box display="flex" alignItems="center" justifyContent="center" w="13px" h="13px" flexShrink={0}>
                                {choice?.icon}
                              </Box>
                              <Flex direction="column" minW={0}>
                                <Text fontSize="xs" fontWeight="medium" whiteSpace="nowrap">
                                  {choice?.label ?? item.label}
                                </Text>
                                {choice?.description && (
                                  <Text fontSize="2xs" color="fg.muted" lineHeight="1.2">
                                    {choice.description}
                                  </Text>
                                )}
                              </Flex>
                            </Flex>
                            <Select.ItemIndicator />
                          </Select.Item>
                        );
                      })}
                    </Select.Content>
                  </Select.Positioner>
                </Portal>
              </Select.Root>
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
              bg="blue.subtle"
              color="blue.fg"
              _hover={{ bg: "blue.muted" }}
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
                w="max-content"
                maxW={{ base: "100%", md: "220px" }}
                minW={0}
                disabled={folderLocked}
                title={folderLocked
                  ? `Project folder is fixed for this session to ${currentDirectory}`
                  : currentDirectory || "Choose project"}
              >
                <Box as="span" truncate minW={0}>
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
    </Box>
  );
}
