"use client";

import {
  Box,
  Button,
  EmptyState,
  Flex,
  Heading,
  IconButton,
  Menu,
  Separator,
  Text,
  VStack,
} from "@chakra-ui/react";
import { LuArrowDown, LuBookMarked, LuEllipsis, LuFolderOpen, LuGitBranch, LuMessageSquare, LuMoon, LuPanelLeftClose, LuPanelLeftOpen, LuPlugZap, LuSettings, LuSun, LuTerminal, LuTrash2, LuTriangleAlert } from "react-icons/lu";
import { AnimatePresence, motion } from "motion/react";
import { FadeIn } from "@/components/ui/fade-in";
import { useTranslations } from "next-intl";
import { toaster } from "@/components/ui/toaster";
import { PanelTiles, type TilePanel } from "./panel-tiles";
import { useColorMode } from "./ui/color-mode";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type PointerEvent } from "react";
import { useChat, type ChatMessage } from "@/lib/use-chat";
import { ChatMessageItem, ChatToolGroup, UserMessageCard } from "./chat-message";
import { TOP_BAR_HEIGHT } from "@/components/ui/panel";
import { ChatInput } from "./chat-input";
import { QuestionOverlay } from "./question-overlay";
import { SettingsDialog, type SettingsSection } from "./settings-dialog";
import { BackgroundJobsPanel } from "./background-jobs-panel";
import { MemoryPanel } from "./memory-panel";
import { CONTROL_ICON_SIZE } from "./session-controls";
import { DelegatedWorkPanel } from "./delegated-work-panel";
import type { SessionEntry } from "./session-row";
import { GitStatusBar } from "./git-status-bar";
import { GoalBar } from "./goal-bar";
import { LocationChip } from "./location-status";
import { SectionHeader } from "./ui/section-header";
import { CONCEPT_ICONS } from "@/lib/concept-icons";
import { useDirectoryStatus } from "@/lib/use-directory-status";
import { Tooltip } from "./ui/tooltip";
import { ToolbarAction } from "@/components/ui/toolbar";
import { AgentNamesProvider } from "@/lib/agent-names";
import { DropdownMenu } from "@/components/ui/menu";
import { PermissionOverlay } from "./permission-overlay";
import { AgentSkills } from "./agent-skills";
import { getToolCallDisplay, type ToolDisplayTranslator } from "@/lib/tool-display";
import { permissionReasonPaths, permissionReasonText, type ToolPermission, type ToolQuestion } from "@/lib/tool-event";

import { clearSessionGoal, fetchSettings, getWorkspace, revealInFinder, saveSessionDraft, saveAgentConfiguration, saveSettings, setSessionPermissionMode, subscribeEvents, type AgentCard, type AgentSummary, type Location, type PermissionMode, type SandboxEnforce, type WorktreeStrategy } from "@/lib/api";
import { scrollFade, scrollFadeTopBottom } from "@/lib/scroll-fade";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { playAttentionSound, playTurnEndSound } from "@/lib/sounds";
import { closePermissionNotification, notifyPermissionRequest, setPermissionNotificationHandler } from "@/lib/notify";
import { swallowed } from "@/lib/swallowed";
import { errorMessage } from "@/lib/errors";

// A Chakra Box that is also a motion component, so the right region can animate open and closed without losing its flex props.
const MotionBox = motion.create(Box);

// The panels that can share the right-hand region: the terminal and background pair, and delegated work.
export type SidePanelKey = "background" | "delegated" | "memory";

const MAXIMUM_OPEN_SIDE_PANELS = 2;

// One shared empty set for a caller tracking no unread completions, so the tree panel is not re-rendered for a fresh Set.
const EMPTY_UNSEEN_COMPLETIONS: Set<string> = new Set();


interface ChatPanelProps {
  agent: string;
  agents: AgentSummary[];
  agentCard?: AgentCard | null;
  onAgentChange: (agent: string) => void;
  // When set, the workspace opens with the Settings dialog already showing this section.
  initialSettingsSection?: string;
  initialSessionId: string | null;
  // The session's display title, shown in the top bar and absent until the session names itself.
  sessionTitle?: string;
  initialInputDraft?: string;
  // Deletes the session by id and routes back to a blank chat; absent when there is no active session.
  onDeleteSession?: (sessionId: string) => void;
  // Every session the client knows, since the delegated-work panel draws the relationships between them.
  sessions?: SessionEntry[];
  // Sessions that finished while you were looking elsewhere, so a row carries the same unread mark as the sidebar.
  unseenCompletions?: Set<string>;
  // The conversation the sidebar has selected, so reading a delegated session does not change what the panel shows.
  rootSessionId?: string | null;
  // Opens a session from the panel; the page owns which conversation is open, so the panel asks rather than switches.
  onResumeSession?: (entry: SessionEntry) => void;
  // Which side panels are open and how wide the region is, held by the page because this component remounts per conversation.
  openSidePanels: SidePanelKey[];
  onOpenSidePanelsChange: (panels: SidePanelKey[]) => void;
  sidePanelWidth: number;
  onSidePanelWidthChange: (width: number) => void;
  initialPermissionMode?: PermissionMode;
  onPermissionModeChange?: (mode: PermissionMode) => void;
  sessionRunning?: boolean;
  onSessionCreated: (sessionId: string) => void;
  onSlashCommand?: (command: string) => void;
  workingDirectory?: string;
  workspaceId?: string;
  homeDirectory?: string;
  sandboxEnforce?: SandboxEnforce;
  sandboxBackend?: { backend: string; detail: string };
  onSandboxEnforceChange?: (enforce: SandboxEnforce) => void;
  worktreeStrategy?: WorktreeStrategy;
  onWorktreeStrategyChange?: (strategy: WorktreeStrategy) => void | Promise<void>;
  // Whether this panel is ready to hold a conversation, which at startup is a wait rather than a failure.
  isConnected?: boolean;
  // Whether the daemon itself is unreachable, the one state with a remedy and so the only one that earns the error screen.
  connectionLost?: boolean;
  // Asks the page to fetch everything a lost daemon took away.
  onReconnect?: () => void;
  onStreamingChange?: (isStreaming: boolean) => void;
  historyOpen?: boolean;
  onToggleHistory?: () => void;
  models?: { id: string; name: string; provider: string; available: boolean }[];
  modelProviders?: { id: string; name: string; openai_compatible: boolean; credential_id: string }[];
  recentModels?: { id: string; name: string; provider: string }[];
  agentModel?: string;
  onAgentModelChange: (modelIdentifier: string) => void | Promise<void>;
}

type TimelineItem =
  | { kind: "message"; message: ChatMessage }
  // A tool_group with no messages is a reasoning phase, and `thinkingTurns` says so.
  | { kind: "tool_group"; id: string; messages: ChatMessage[]; thinkingTurns: number };

function folderDisplayName(workingDirectory?: string): string {
  const directory = (workingDirectory ?? "").trim();
  if (!directory) return "";
  return directory.split(/[\\/]/).filter(Boolean).at(-1) ?? directory;
}

function timelineItems(messages: ChatMessage[]): TimelineItem[] {
  const items: TimelineItem[] = [];
  let index = 0;
  // The first reasoning phase since the last plain row, keying the group so thinking and its tools stay one element.
  let pendingThinkingId: string | null = null;
  // Count reasoning messages so a tools-less Thinking group is distinguishable from an empty one.
  let pendingThinkingTurns = 0;
  while (index < messages.length) {
    const message = messages[index];
    if (message.role === "thinking") {
      pendingThinkingId ??= message.id;
      pendingThinkingTurns += 1;
      index += 1;
      continue;
    }
    if (message.role === "assistant" && !message.content.trim()) {
      index += 1;
      continue;
    }
    if (message.role !== "tool_call") {
      if (pendingThinkingId) {
        items.push({ kind: "tool_group", id: pendingThinkingId, messages: [], thinkingTurns: pendingThinkingTurns });
      }
      items.push({ kind: "message", message });
      pendingThinkingId = null;
      pendingThinkingTurns = 0;
      index += 1;
      continue;
    }

    const toolMessages: ChatMessage[] = [];
    // The leading reasoning keys the group, and reasoning phases are tallied from it plus any interleaved between calls.
    const groupKey = pendingThinkingId;
    let thinkingTurns = pendingThinkingTurns;
    pendingThinkingId = null;
    pendingThinkingTurns = 0;
    // Gather contiguous tool calls, letting hidden reasoning pass so it never splits one batch into two.
    while (index < messages.length) {
      const next = messages[index];
      if (next.role === "tool_call") {
        toolMessages.push(next);
        index += 1;
      } else if (next.role === "thinking") {
        thinkingTurns += 1;
        index += 1;
      } else {
        break;
      }
    }
    items.push({
      kind: "tool_group",
      // Prefer the leading thinking id so the key survives the thinking-to-tools transition.
      id: groupKey ?? toolMessages[0].id,
      messages: toolMessages,
      thinkingTurns,
    });
  }
  // A trailing reasoning phase is emitted here and rendered only while the turn is live, so settled reasoning leaves no row.
  if (pendingThinkingId) {
    items.push({ kind: "tool_group", id: pendingThinkingId, messages: [], thinkingTurns: pendingThinkingTurns });
  }
  return items;
}

export function ChatPanel({
  agent,
  agents,
  agentCard,
  onAgentChange,
  initialSettingsSection,
  initialSessionId,
  sessionTitle,
  initialInputDraft = "",
  onDeleteSession,
  sessions = [],
  unseenCompletions,
  rootSessionId = null,
  onResumeSession,
  openSidePanels,
  onOpenSidePanelsChange,
  sidePanelWidth,
  onSidePanelWidthChange,
  initialPermissionMode = "ask",
  onPermissionModeChange,
  sessionRunning = false,
  onSessionCreated,
  workingDirectory,
  workspaceId = "",
  homeDirectory,
  sandboxEnforce = "required" as SandboxEnforce,
  sandboxBackend = { backend: "", detail: "" },
  onSandboxEnforceChange,
  worktreeStrategy = "none",
  onWorktreeStrategyChange,
  isConnected = false,
  connectionLost = false,
  onReconnect,
  onStreamingChange,
  historyOpen = false,
  onToggleHistory,
  models = [],
  modelProviders = [],
  recentModels = [],
  agentModel = "",
  onAgentModelChange,
}: ChatPanelProps) {
  const translation = useTranslations("ChatPanel");
  const toolDisplayTranslation = useTranslations("ToolDisplay") as unknown as ToolDisplayTranslator;
  const [permissionMode, setPermissionModeState] = useState<PermissionMode>(initialPermissionMode);
  const { messages, tokenUsage, queuedMessages, sessionId, isStreaming, isHistoryLoading, historyError, reloadHistory, send, abort, dequeueMessage, outboxHold, deliveringMessage, grantedPermissionMode, retryOutbox, handlePermission, handleQuestion, declineQuestion, compact } =
    useChat(agent, initialSessionId, workingDirectory, worktreeStrategy, permissionMode, sessionRunning, workspaceId);

  // One source of truth for the working directory's validity and Git status, read by the status bar and the composer's send gate.
  const { status: directoryStatus, directoryValid } = useDirectoryStatus(workingDirectory);
  // Whether the chat body has resolved enough to render without flashing, so opening a workspace does not flicker.
  const trimmedWorkingDirectory = (workingDirectory ?? "").trim();
  const directoryPending = !!trimmedWorkingDirectory && (directoryStatus.checking || directoryStatus.path !== trimmedWorkingDirectory);
  // Mounted whenever a daemon is there: a directory check runs on every switch, and unmounting on it jumped.
  const chatReady = isConnected;
  // The one condition under which the transcript is in the DOM, read by the render and by everything touching scroll.
  const transcriptVisible = chatReady && !isHistoryLoading;

  // The workspace's locations for the terminal picker, refreshed when the workspace configuration changes.
  const [workspaceLocations, setWorkspaceLocations] = useState<Location[]>([]);
  useEffect(() => {
    let cancelled = false;
    // Resolving through a promise keeps the state update off the synchronous effect path.
    const load = () => {
      const request = workspaceId ? getWorkspace(workspaceId) : Promise.resolve(null);
      request.then((workspace) => { if (!cancelled) setWorkspaceLocations(workspace?.locations ?? []); }).catch((caught) => swallowed({ component: "chat-panel", operation: "read a workspace" }, caught));
    };
    load();
    const unsubscribe = subscribeEvents((event) => { if (event.type === "workspaces_changed") load(); });
    return () => { cancelled = true; unsubscribe(); };
  }, [workspaceId]);

  // On mount, read the stored permission mode so the user's last choice survives a reload when no session is active.
  useEffect(() => {
    if (initialSessionId) return;
    let cancelled = false;
    fetchSettings().then((settings) => {
      if (cancelled || settings.permission_mode === permissionMode) return;
      setPermissionModeState(settings.permission_mode);
    }).catch((caught) => swallowed({ component: "chat-panel", operation: "read the settings" }, caught));
    return () => { cancelled = true; };
  // Only without a session, since a session's own permission mode is the one that governs it.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialSessionId]);

  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const scrollContentRef = useRef<HTMLDivElement>(null);
  // Following the bottom, released the moment the user scrolls up and resumed when they return.
  const isPinnedRef = useRef(true);
  const lastScrollTopRef = useRef(0);
  const onStreamingChangeRef = useRef(onStreamingChange);
  // The previous layout pass, so a top prepend can be told from a bottom append and the reader's viewport preserved.
  const scrollMetricsRef = useRef({ scrollHeight: 0, firstKey: "", count: 0 });
  const notifiedSessionIdRef = useRef<string | null>(null);
  const backgroundPanelOpen = openSidePanels.includes("background");
  const memoryPanelOpen = openSidePanels.includes("memory");
  const delegatedPanelOpen = openSidePanels.includes("delegated");
  const { colorMode, toggleColorMode } = useColorMode();
  // Whether the transcript is near the bottom, driving the jump-to-latest affordance.
  const [isAtBottom, setIsAtBottom] = useState(true);
  // A strict at-bottom flag driving the transcript's bottom fade, so the last line is never dimmed.
  const [transcriptPinned, setTranscriptPinned] = useState(true);
  // Top-bar surfaces, opened on mount when the workspace was entered with `?settings=<section>`.
  const validInitialSection: SettingsSection | null =
    initialSettingsSection === "general" || initialSettingsSection === "locations"
      || initialSettingsSection === "agents" || initialSettingsSection === "connection"
      ? initialSettingsSection : null;
  const [settingsOpen, setSettingsOpen] = useState(!!validInitialSection);
  const [settingsSection, setSettingsSection] = useState<SettingsSection>(validInitialSection ?? "general");
  const [appliedInitialSettingsSection, setAppliedInitialSettingsSection] = useState(validInitialSection);
  if (appliedInitialSettingsSection !== validInitialSection) {
    setAppliedInitialSettingsSection(validInitialSection);
    if (validInitialSection) {
      setSettingsSection(validInitialSection);
      setSettingsOpen(true);
    }
  }
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);

  const setSidePanelOpen = useCallback((panel: SidePanelKey, open: boolean) => {
    const remainingPanels = openSidePanels.filter((openPanel) => openPanel !== panel);
    onOpenSidePanelsChange(open ? [...remainingPanels, panel].slice(-MAXIMUM_OPEN_SIDE_PANELS) : remainingPanels);
  }, [openSidePanels, onOpenSidePanelsChange]);

  useEffect(() => {
    if (openSidePanels.length === 0 || !historyOpen || !window.matchMedia("(max-width: 1199px)").matches) return;
    onToggleHistory?.();
  }, [openSidePanels.length, historyOpen, onToggleHistory]);

  const markSidePanelActive = useCallback((panel: SidePanelKey) => {
    if (!openSidePanels.includes(panel) || openSidePanels[openSidePanels.length - 1] === panel) return;
    onOpenSidePanelsChange([...openSidePanels.filter((openPanel) => openPanel !== panel), panel]);
  }, [openSidePanels, onOpenSidePanelsChange]);

  // Pinned means the viewport is at the bottom: pinned follows new content, unpinned never pulls the reader back down.
  const handleScroll = useCallback(() => {
    const container = scrollContainerRef.current;
    if (!container) return;
    const distanceFromBottom = container.scrollHeight - (container.scrollTop + container.clientHeight);
    const atBottom = distanceFromBottom <= 8;
    isPinnedRef.current = atBottom;
    setTranscriptPinned(atBottom);
    // A larger threshold for the jump button than for pinning, so the button does not flash while pinning stays strict.
    setIsAtBottom(distanceFromBottom <= 120);
    lastScrollTopRef.current = container.scrollTop;
  }, []);

  useEffect(() => {
    onStreamingChangeRef.current = onStreamingChange;
  }, [onStreamingChange]);

  const scrollToBottom = useCallback(() => {
    isPinnedRef.current = true;
    setIsAtBottom(true);
    const container = scrollContainerRef.current;
    if (!container) return;
    container.scrollTop = container.scrollHeight;
    lastScrollTopRef.current = container.scrollTop;
    scrollMetricsRef.current.scrollHeight = container.scrollHeight;
  }, []);

  const handleSend = useCallback((text: string, dataParts?: Record<string, unknown>[]) => {
    // Which agent runs is never assumed, so an unchosen agent is asked for rather than guessed at.
    if (!agent && !initialSessionId) {
      toaster.create({
        type: "error",
        title: translation("chooseAnAgent"),
        description: translation("chooseAnAgentDescription"),
        closable: true,
      });
      return undefined;
    }
    scrollToBottom();
    // Whether this can be sent at all is `useChat`'s to answer rather than the composer's.
    const result = send(text, dataParts);
    scrollToBottom();
    return result;
  }, [agent, initialSessionId, scrollToBottom, send, translation]);

  // A clamped session says so once, and the chip follows the truth rather than the request.
  const effectivePermissionMode = grantedPermissionMode ?? permissionMode;
  const announcedClampRef = useRef<string>("");
  useEffect(() => {
    if (!grantedPermissionMode || announcedClampRef.current === grantedPermissionMode) return;
    announcedClampRef.current = grantedPermissionMode;
    onPermissionModeChange?.(grantedPermissionMode);
    toaster.create({
      type: "info",
      title: translation("permissionClampedTitle"),
      description: translation("permissionClampedBody", { mode: grantedPermissionMode }),
      closable: true,
    });
  }, [grantedPermissionMode, onPermissionModeChange, translation]);

  // The session's goal, read from the session list the daemon already pushes rather than kept as a second copy.
  const activeGoal = useMemo(
    () => sessions.find((entry) => entry.sessionId === sessionId)?.goal ?? null,
    [sessions, sessionId],
  );
  const handleClearGoal = useCallback(() => {
    if (!sessionId) return;
    // The bar clears when the daemon says it cleared, since whether the goal was still there is the session's answer.
    clearSessionGoal(sessionId).catch((caught) =>
      swallowed({ component: "chat-panel", operation: "call off the goal" }, caught));
  }, [sessionId]);

  const openSettings = useCallback((section: SettingsSection) => {
    setSettingsSection(section);
    setSettingsOpen(true);
  }, [setSettingsOpen, setSettingsSection]);

  useEffect(() => {
    if (!sessionId || notifiedSessionIdRef.current === sessionId) return;
    notifiedSessionIdRef.current = sessionId;
    onSessionCreated(sessionId);
  }, [sessionId, onSessionCreated]);

  // The single owner of scroll position: a top prepend preserves the viewport, everything else follows the bottom while pinned.
  useLayoutEffect(() => {
    const container = scrollContainerRef.current;
    if (!container || !transcriptVisible) return;
    const firstKey = messages.length > 0 ? messages[0].id : "";
    const count = messages.length;
    const previous = scrollMetricsRef.current;
    const prepended = count > previous.count && firstKey !== previous.firstKey && previous.count > 0;
    if (prepended) {
      // Shift down by exactly how much taller the content got, so background paging is invisible.
      const delta = container.scrollHeight - previous.scrollHeight;
      if (delta !== 0) container.scrollTop = container.scrollTop + delta;
    } else if (isPinnedRef.current) {
      container.scrollTop = container.scrollHeight;
    }
    scrollMetricsRef.current = { scrollHeight: container.scrollHeight, firstKey, count };
    lastScrollTopRef.current = container.scrollTop;
  }, [messages, queuedMessages, transcriptVisible]);

  // Late-growing content keeps the bottom in view while pinned, re-running when the transcript container actually mounts.
  const timelineMounted = transcriptVisible && !historyError && messages.length > 0;
  useEffect(() => {
    const content = scrollContentRef.current;
    const container = scrollContainerRef.current;
    if (!content || !container) return;
    const observer = new ResizeObserver(() => {
      if (!isPinnedRef.current) return;
      container.scrollTop = container.scrollHeight;
      lastScrollTopRef.current = container.scrollTop;
      scrollMetricsRef.current.scrollHeight = container.scrollHeight;
    });
    observer.observe(content);
    return () => observer.disconnect();
  }, [timelineMounted]);

  // Follow the session the parent selected, as a render-phase adjustment rather than an effect.
  const [previousInitialSession, setPreviousInitialSession] = useState({ id: initialSessionId, permissionMode: initialPermissionMode });
  if (initialSessionId !== previousInitialSession.id || initialPermissionMode !== previousInitialSession.permissionMode) {
    setPreviousInitialSession({ id: initialSessionId, permissionMode: initialPermissionMode });
    setPermissionModeState(initialPermissionMode);
  }

  useLayoutEffect(() => {
    scrollMetricsRef.current = { scrollHeight: 0, firstKey: "", count: 0 };
    isPinnedRef.current = true;
  }, [initialSessionId]);

  // Surface the streaming flag to the parent; new content is followed by the layout effect above.
  useEffect(() => {
    onStreamingChangeRef.current?.(isStreaming);
  }, [isStreaming]);

  // One control doing two things: without a session it chooses what the next one starts under, with one it also changes that session.
  async function handlePermissionModeChange(nextMode: PermissionMode) {
    setPermissionModeState(nextMode);
    onPermissionModeChange?.(nextMode);
    // Persist to server settings so it survives across sessions.
    saveSettings({ permission_mode: nextMode }).catch((caught) => swallowed({ component: "chat-panel", operation: "save the settings" }, caught));
    // Raise the agent's own ceiling first, since the daemon meets any request against it and would otherwise clamp silently.
    try {
      await saveAgentConfiguration(agent, { permission_mode: nextMode }, workingDirectory);
    } catch (caught) {
      swallowed({ component: "chat-panel", operation: "raise the agent's permission ceiling" }, caught);
    }
    if (!sessionId) return;
    setSessionPermissionMode(sessionId, nextMode)
      .then((applied) => {
        setPermissionModeState(applied);
        // Told to the parent as well, because the server clamps against the parent session and the agent's ceiling.
        if (applied !== nextMode) onPermissionModeChange?.(applied);
      })
      .catch((caught) => {
        // The session kept the mode it had, so the chip goes back to saying so.
        setPermissionModeState(permissionMode);
        onPermissionModeChange?.(permissionMode);
        toaster.create({
          type: "error",
          title: translation("permissionModeFailedTitle"),
          description: errorMessage(caught),
          closable: true,
        });
      });
  }

  const handleInputDraftChange = useCallback((nextDraft: string) => {
    if (!sessionId) return;
    saveSessionDraft(sessionId, nextDraft).catch((caught) => swallowed({ component: "chat-panel", operation: "save the session draft" }, caught));
  }, [sessionId]);

  const currentFolderName = folderDisplayName(workingDirectory) || translation("thisFolder");
  const renderedTimeline = useMemo(() => timelineItems(messages), [messages]);
  const hasInheritedContext = Boolean(
    initialSessionId && sessions.some((entry) => entry.sessionId === initialSessionId && entry.parentSessionId),
  );
  // Entrance animation is reserved for rows a live turn just appended at the bottom, by a purely positional rule.
  const timelineKeys = renderedTimeline.map((item) => (item.kind === "tool_group" ? item.id : item.message.id));
  const timelineSessionKey = initialSessionId ?? "__new__";
  const [timelineAnimationState, setTimelineAnimationState] = useState<{
    sessionKey: string;
    keys: string[];
    seen: string[];
    animated: string[];
  }>(() => ({ sessionKey: timelineSessionKey, keys: [], seen: [], animated: [] }));
  let animatedKeys = new Set(timelineAnimationState.animated);
  const timelineKeysChanged =
    timelineAnimationState.sessionKey !== timelineSessionKey ||
    timelineAnimationState.keys.length !== timelineKeys.length ||
    timelineAnimationState.keys.some((key, index) => key !== timelineKeys[index]);
  if (timelineKeysChanged) {
    const previousSeen = timelineAnimationState.sessionKey === timelineSessionKey
      ? new Set(timelineAnimationState.seen)
      : new Set<string>();
    const nextAnimated = new Set<string>();
    // Only when this is the same transcript with something added, since a replay rebuilds every row under a different id.
    const survived = timelineKeys.some((key) => previousSeen.has(key));
    if (previousSeen.size > 0 && survived) {
      for (let index = timelineKeys.length - 1; index >= 0; index -= 1) {
        if (previousSeen.has(timelineKeys[index])) break;
        nextAnimated.add(timelineKeys[index]);
      }
    }
    const nextSeen = Array.from(new Set([...previousSeen, ...timelineKeys]));
    setTimelineAnimationState({
      sessionKey: timelineSessionKey,
      keys: timelineKeys,
      seen: nextSeen,
      animated: Array.from(nextAnimated),
    });
    animatedKeys = nextAnimated;
  }
  // Running shell commands drive the badge on the background-processes button.
  const runningShellCount = messages.filter((message) =>
    message.role === "tool_call" && message.content === "bash" &&
    (message.meta?.status === "running" || message.meta?.status === "input_required")
  ).length;
  // How many sessions the open conversation has delegated, at any depth, counted against that conversation.
  const delegatedSessionCount = useMemo(() => {
    if (!rootSessionId) return 0;
    const childrenOf = new Map<string, string[]>();
    for (const session of sessions) {
      if (!session.parentSessionId) continue;
      childrenOf.set(session.parentSessionId, [...(childrenOf.get(session.parentSessionId) ?? []), session.sessionId]);
    }
    let total = 0;
    const pending = [rootSessionId];
    while (pending.length > 0) {
      const next = childrenOf.get(pending.pop() as string) ?? [];
      total += next.length;
      pending.push(...next);
    }
    return total;
  }, [sessions, rootSessionId]);
  // A compaction pass is live while its marker is still running, driving the Compact control's in-progress state.
  const isCompacting = messages.some(
    (message) => message.role === "compaction" && message.meta?.status === "running"
  );
  // "Reveal" opens the session's working directory in the OS file manager.
  const revealPath = (workingDirectory || "").trim();

  // Try again re-runs a failed turn by resending the most recent user message, which produced no lasting state.
  const handleRetry = useCallback(() => {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const candidate = messages[index];
      if (candidate.role === "user" && candidate.content.trim()) {
        send(candidate.content);
        return;
      }
    }
  }, [messages, send]);

  // The first pending prompt, surfaced as an overlay above the input so a decision always grabs attention.
  let pendingPrompt: (
    | { kind: "question"; question: ToolQuestion }
    | { kind: "permission"; permission: ToolPermission; title: string; detail?: string; detailPaths?: string[]; command?: string; arguments?: Record<string, unknown> }
    | null
  ) = null;
  {
    for (const message of messages) {
      if (message.role !== "tool_call" || message.meta?.status !== "input_required") continue;
      const question = message.meta?.question as ToolQuestion | undefined;
      if (question) {
        pendingPrompt = { kind: "question", question };
        break;
      }
      const permission = message.meta?.permission as ToolPermission | undefined;
      if (permission) {
        const name = message.content;
        const args = message.meta?.arguments as Record<string, unknown> | undefined;
        const command = name === "bash" && args?.command ? String(args.command) : "";
        pendingPrompt = {
          kind: "permission",
          permission,
          // The title says what the agent is trying to do, and the detail says what made this stop for approval.
          title: getToolCallDisplay(name, args, toolDisplayTranslation).label,
          // The structured reason wins, because it is the only one this interface can say in the reader's language.
          detail: permissionReasonText(permission.reason, translation) || permission.explanation || undefined,
          // The paths travel as data so the overlay lists them, rather than being joined into a sentence.
          detailPaths: permissionReasonPaths(permission.reason),
          command: command || undefined,
          arguments: args,
        };
        break;
      }
    }
  }
  // The attention cue plays for the first prompt in a turn, and a permission prompt also raises a system notification.
  const permissionTranslation = useTranslations("PermissionOverlay");
  const pendingPermissionId = pendingPrompt?.kind === "permission" ? pendingPrompt.permission.requestId : "";
  const pendingQuestionId = pendingPrompt?.kind === "question" ? pendingPrompt.question.requestId : "";
  const pendingPermissionBody = pendingPrompt?.kind === "permission" ? pendingPrompt.command || pendingPrompt.title : "";
  const notifiedPermissionRef = useRef("");
  const attentionSoundPlayedRef = useRef(false);
  const pendingPromptId = pendingPermissionId || pendingQuestionId;
  useEffect(() => {
    if (!isStreaming && !pendingPromptId) {
      attentionSoundPlayedRef.current = false;
      return;
    }
    if (!pendingPromptId || attentionSoundPlayedRef.current) return;
    attentionSoundPlayedRef.current = true;
    playAttentionSound();
  }, [isStreaming, pendingPromptId]);

  // The turn-end chime, on the transition to actually finished rather than to a pause for a decision.
  const wasRunningRef = useRef(false);
  useEffect(() => {
    const running = isStreaming || !!pendingPromptId;
    if (wasRunningRef.current && !running) playTurnEndSound();
    wasRunningRef.current = running;
  }, [isStreaming, pendingPromptId]);
  useEffect(() => {
    const previous = notifiedPermissionRef.current;
    if (previous && previous !== pendingPermissionId) void closePermissionNotification(previous);
    notifiedPermissionRef.current = pendingPermissionId;
    if (!pendingPermissionId) return;
    void notifyPermissionRequest({
      requestId: pendingPermissionId,
      title: permissionTranslation("approvalNeeded"),
      body: pendingPermissionBody,
      actionLabel: permissionTranslation("allowOnce"),
    });
  }, [pendingPermissionId, pendingPermissionBody, permissionTranslation]);
  // The notification's action button resolves the request exactly like the overlay's primary button.
  useEffect(() => {
    setPermissionNotificationHandler((requestId) => handlePermission(requestId, "allow_once"));
    return () => setPermissionNotificationHandler(null);
  }, [handlePermission]);

  const startSidePanelResize = useCallback((event: PointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = sidePanelWidth;

    function handlePointerMove(moveEvent: globalThis.PointerEvent) {
      // Clamp to the same bounds the region's CSS enforces, so the drag never fights the styled limits.
      const nextWidth = Math.min(Math.min(900, Math.round(window.innerWidth * 0.8)), Math.max(360, startWidth + startX - moveEvent.clientX));
      onSidePanelWidthChange(nextWidth);
    }

    function handlePointerUp() {
      window.removeEventListener("pointermove", handlePointerMove);
      window.removeEventListener("pointerup", handlePointerUp);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    }

    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    window.addEventListener("pointermove", handlePointerMove);
    window.addEventListener("pointerup", handlePointerUp, { once: true });
  }, [sidePanelWidth, onSidePanelWidthChange]);

  // Built before the return so the region can be gated on the tiles themselves: an open panel with
  // nothing to render would otherwise reserve a full-width column of empty space.
  const sidePanels = ([
      backgroundPanelOpen && {
        key: "background",
        onActivate: () => markSidePanelActive("background"),
        content: (
          <BackgroundJobsPanel
            open={backgroundPanelOpen}
            onClose={() => setSidePanelOpen("background", false)}
            messages={messages}
            sessionId={sessionId}
            workingDirectory={workingDirectory || homeDirectory || ""}
            locations={workspaceLocations}
          />
        ),
      },
      memoryPanelOpen && {
        key: "memory",
        onActivate: () => markSidePanelActive("memory"),
        content: (
          <MemoryPanel
            key={sessionId}
            sessionId={sessionId}
            onClose={() => setSidePanelOpen("memory", false)}
          />
        ),
      },
      delegatedPanelOpen && {
        key: "delegated",
        onActivate: () => markSidePanelActive("delegated"),
        content: (
          <DelegatedWorkPanel
            sessions={sessions}
            rootSessionId={rootSessionId}
            activeSessionId={sessionId}
            unseenCompletions={unseenCompletions ?? EMPTY_UNSEEN_COMPLETIONS}
            agents={agents}
            onResume={(entry) => onResumeSession?.(entry)}
            onDeleteSession={(entry) => onDeleteSession?.(entry.sessionId)}
            onClose={() => setSidePanelOpen("delegated", false)}
          />
        ),
      },
    ].filter(Boolean) as TilePanel[]);

  return (
    // Every profile name is resolved from this one catalogue, so the transcript and the sidebar cannot disagree.
    <AgentNamesProvider agents={agents}>
    <Flex h="100%" minW={0} position="relative">
      <Flex direction="column" flex={1} minW={0} h="100%">
        {/* Persistent top bar: session identity on the left, session tools on the right. */}
        <Flex align="center" gap={2} px={2} h={TOP_BAR_HEIGHT} flexShrink={0} minW={0}>
          {onToggleHistory ? (
            <Tooltip content={historyOpen ? translation("hideConversations") : translation("showConversations")} openDelay={300}>
              <IconButton
                aria-label={historyOpen ? translation("hideConversationsSidebar") : translation("showConversationsSidebar")}
                variant="ghost"
                colorPalette="gray"
                flexShrink={0}
                onClick={onToggleHistory}
              >
                {historyOpen ? <LuPanelLeftClose size={14} /> : <LuPanelLeftOpen size={14} />}
              </IconButton>
            </Tooltip>
          ) : (
            <Box color="fg.muted" flexShrink={0}><LuMessageSquare size={14} /></Box>
          )}
          <Text textStyle="panelTitle" fontWeight="medium" truncate minW={0} flex={1}>
            {sessionId ? (sessionTitle || translation("untitledConversation")) : translation("newConversation")}
          </Text>
          <GitStatusBar status={directoryStatus} />
          <Flex align="center" gap={1} flexShrink={0}>
            {/* What this workspace's conversations have handed to other sessions, with a dot only when something has. */}
            <ToolbarAction
              label={translation("delegatedWork")}
              icon={<LuGitBranch size={14} />}
              active={delegatedPanelOpen}
              colorPalette="purple"
              indicator={delegatedSessionCount > 0}
              onClick={() => setSidePanelOpen("delegated", !delegatedPanelOpen)}
            />
            {/* What this conversation remembers of the turns that have left its window. */}
            <ToolbarAction
              label={translation("memory")}
              icon={<LuBookMarked size={CONTROL_ICON_SIZE} />}
              active={memoryPanelOpen}
              colorPalette="orange"
              onClick={() => setSidePanelOpen("memory", !memoryPanelOpen)}
            />
            <ToolbarAction
              label={translation("terminalAndBackground")}
              icon={<LuTerminal size={14} />}
              active={backgroundPanelOpen}
              colorPalette="green"
              indicator={runningShellCount > 0}
              onClick={() => setSidePanelOpen("background", !backgroundPanelOpen)}
            />
            {/* Light and dark, switched here because it is the one setting people change on a whim. */}
            <ToolbarAction
              label={colorMode === "dark" ? translation("switchToLight") : translation("switchToDark")}
              icon={colorMode === "dark" ? <LuSun size={14} /> : <LuMoon size={14} />}
              onClick={toggleColorMode}
            />
            <ToolbarAction
              label={translation("settings")}
              icon={<LuSettings size={14} />}
              onClick={() => openSettings("general")}
            />
            <DropdownMenu
              trigger={
                <IconButton aria-label={translation("sessionOptions")} variant="ghost">
                  <LuEllipsis size={14} />
                </IconButton>
              }
              minW="200px"
            >
              <Menu.Item
                value="reveal"
                fontSize="xs"
                disabled={!revealPath}
                onClick={() => { if (revealPath) void revealInFinder(revealPath); }}
              >
                <LuFolderOpen size={13} />
                <Box flex={1}>{translation("openThisFolder")}</Box>
              </Menu.Item>
              <Menu.Item
                value="delete"
                fontSize="xs"
                color="red.fg"
                _hover={{ bg: "red.subtle" }}
                disabled={!sessionId || !onDeleteSession}
                onClick={() => setDeleteConfirmOpen(true)}
              >
                <LuTrash2 size={13} />
                <Box flex={1}>{translation("deleteSession")}</Box>
              </Menu.Item>
            </DropdownMenu>
          </Flex>
        </Flex>
        <Box position="relative" flex={1} minH={0} display="flex" flexDirection="column">
        <Box ref={scrollContainerRef} flex={1} minH={0} display="flex" flexDirection="column" overflowY="auto" px={4} py={3} onScroll={handleScroll} css={transcriptPinned ? scrollFade : scrollFadeTopBottom} style={{ overflowAnchor: "none", scrollbarGutter: "stable both-edges" }}>
          {connectionLost ? (
            // A lost daemon is a state worth naming, and the only one here whose remedy is a single button.
            <Flex direction="column" align="center" justify="center" minH="100%" gap={6} px={2}>
              <EmptyState.Root>
                <EmptyState.Content>
                  <EmptyState.Indicator>
                    <LuPlugZap />
                  </EmptyState.Indicator>
                  <VStack gap={1}>
                    <EmptyState.Title>{translation("disconnectedTitle")}</EmptyState.Title>
                    <EmptyState.Description>{translation("disconnectedDescription")}</EmptyState.Description>
                  </VStack>
                  <Button variant="solid" colorPalette="blue" onClick={onReconnect}>
                    {translation("reconnect")}
                  </Button>
                </EmptyState.Content>
              </EmptyState.Root>
            </Flex>
          ) : !transcriptVisible ? (
            <Flex h="100%" />
          ) : historyError ? (
            <Flex direction="column" align="center" justify="center" minH="100%" gap={6} px={2}>
              <EmptyState.Root>
                <EmptyState.Content>
                  <EmptyState.Indicator>
                    <LuTriangleAlert />
                  </EmptyState.Indicator>
                  <VStack gap={1}>
                    <EmptyState.Title>{translation("loadConversationErrorTitle")}</EmptyState.Title>
                    <EmptyState.Description>
                      {translation("loadConversationErrorDescription")}
                    </EmptyState.Description>
                  </VStack>
                  <Button variant="solid" colorPalette="blue" onClick={reloadHistory}>
                    {translation("retry")}
                  </Button>
                </EmptyState.Content>
              </EmptyState.Root>
            </Flex>
          ) : (
            // The welcome and the timeline cross-fade out of flow, so sending the first message never flashes.
            <AnimatePresence mode="popLayout" initial={false}>
              {messages.length === 0 && !hasInheritedContext ? (
                <motion.div key="empty" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.15, ease: "easeOut" }} style={{ width: "100%", flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}>
                  {/* The same centred column as the transcript, sitting in the middle of whatever room there is. */}
                  {/* One rhythm for the sections, matching the gap the capability sections keep between themselves. */}
                  <Flex direction="column" align="stretch" gap={6} w="full" maxW="80rem" mx="auto" my="auto" py={{ base: 4, md: 6 }}>
                    {/* The blank-conversation state inside a workspace: the prompt, then what it can reach and what the agent can do. */}
                    <Heading as="h2" fontSize="3xl" fontWeight="semibold" textAlign="center" mb={4}>
                      {translation("buildPrompt", { folder: currentFolderName })}
                    </Heading>

                    {/* The locations read as a section like the ones under them, with the same icon, heading and list. */}
                    {workspaceLocations.length > 0 && (
                      <Box w="100%" minW={0}>
                        <SectionHeader
                          icon={<CONCEPT_ICONS.environment size={14} />}
                          title={translation("environmentsAvailable")}
                          description={translation("environmentsDescription")}
                        />
                        <Flex align="center" gap={2.5} wrap="wrap">
                          {workspaceLocations.map((location) => (
                            <LocationChip key={location.id} location={location} />
                          ))}
                        </Flex>
                      </Box>
                    )}

                    <AgentSkills card={agentCard ?? null} workingDirectory={workingDirectory} />
                  </Flex>
                </motion.div>
              ) : (
                <motion.div key="timeline" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.15, ease: "easeOut" }} style={{ width: "100%" }}>
                  {/* Tight enough that a tool line and the prose around it read as one document, with bubbles marking turns. */}
                  <VStack ref={scrollContentRef} gap={2.5} align="stretch" w="full" maxW="80rem" mx="auto">
                    {hasInheritedContext ? (
                      <Flex align="center" gap={3} py={2} color="fg.muted">
                        <Separator flex={1} />
                        <Text fontSize="xs" whiteSpace="nowrap">{translation("inheritedContextBoundary")}</Text>
                        <Separator flex={1} />
                      </Flex>
                    ) : null}
                    {/* No presence wrapper, deliberately: a transcript row appears when it exists and is gone when it does not. */}
                    {renderedTimeline.map((item, itemIndex) => {
                        const isLastItem = itemIndex === renderedTimeline.length - 1;
                        const key = item.kind === "tool_group" ? item.id : item.message.id;
                        // A tools-less group stays as a persistent Thinking row, so it is skipped only when it carries neither tools nor thinking.
                        if (item.kind === "tool_group" && item.messages.length === 0 && item.thinkingTurns === 0) {
                          return null;
                        }
                        const inner = item.kind === "tool_group" ? (
                          <ChatToolGroup
                            messages={item.messages}
                            keepOpen={isStreaming && isLastItem}
                          />
                        ) : (
                          <ChatMessageItem
                            message={item.message}
                            onRetry={item.message.role === "error" ? handleRetry : undefined}
                            streaming={isStreaming && isLastItem}
                          />
                        );
                        // Assistant messages stream, so their wrapper stays stable; complete rows get a single gentle fade.
                        const isAssistantMessage = item.kind === "message" && item.message.role === "assistant";
                        if (isAssistantMessage) {
                          return (
                            <Box key={key} display="flex" flexDirection="column">
                              {inner}
                            </Box>
                          );
                        }
                        return (
                          <FadeIn
                            key={key}
                            animate={animatedKeys.has(key)}
                            style={{ display: "flex", flexDirection: "column" }}
                          >
                            {inner}
                          </FadeIn>
                        );
                    })}
                    {/* A queued message, drawn as itself; the one being handed over is skipped, being a row already. */}
                    {queuedMessages.map((message, index) => message.id === deliveringMessage ? null : (
                      <UserMessageCard
                        key={message.id}
                        message={{ id: message.id, role: "user", content: message.text, timestamp: "" }}
                        queued={{
                          status: translation(
                            outboxHold === "unreachable" ? "queuedUnreachable"
                              : outboxHold === "decision" ? "queuedForDecision"
                                : "queued"
                          ),
                          failed: outboxHold === "unreachable",
                          onDelete: () => dequeueMessage(index),
                          ...(outboxHold === "unreachable" && index === 0
                            ? { onRetry: retryOutbox, retryLabel: translation("queuedRetry") }
                            : {}),
                        }}
                      />
                    ))}
                  </VStack>
                </motion.div>
              )}
            </AnimatePresence>
          )}
        </Box>
        {!isAtBottom && timelineMounted && (
          <Button
            variant="outline"
            position="absolute"
            bottom={3}
            left="50%"
            transform="translateX(-50%)"
            zIndex={2}
            bg="bg.subtle"
            color="fg"
            fontWeight="medium"
            px={2}
            onClick={scrollToBottom}
          >
            <LuArrowDown />
            {translation("jumpToLatest")}
          </Button>
        )}
        </Box>

        {/* The overlay sits in the same centred column as the messages, with no clipping to slice its shadow. */}
        {/* One owner for the transition, keyed by the request, so two decisions are never on screen at once. */}
        <AnimatePresence mode="wait" initial={false}>
          {pendingPrompt && (
            <FadeIn key={pendingPromptId} seconds={0.15}>
              <Box px={4}>
                <Box w="full" maxW="80rem" mx="auto">
                  {pendingPrompt.kind === "question" && (
                    <QuestionOverlay question={pendingPrompt.question} onQuestion={handleQuestion} onDismiss={declineQuestion} />
                  )}
                  {pendingPrompt.kind === "permission" && (
                    <PermissionOverlay
                      permission={pendingPrompt.permission}
                      title={pendingPrompt.title}
                      detail={pendingPrompt.detail}
                      detailPaths={pendingPrompt.detailPaths}
                      command={pendingPrompt.command}
                      arguments={pendingPrompt.arguments}
                      onPermission={handlePermission}
                    />
                  )}
                </Box>
              </Box>
            </FadeIn>
          )}
        </AnimatePresence>
        {/* The composer mirrors the transcript's horizontal geometry, with the scrollbar gutter reserved on both edges. */}
        {/* The bottom padding is the phone's home indicator, and zero everywhere else. */}
        {chatReady && (
        <Box
          px={4}
          pb="var(--safe-bottom, 0px)"
          overflowY="hidden"
          // Never gives up its height, so a growing composer cannot push itself past the bottom of the window.
          flexShrink={0}
          style={{ scrollbarGutter: "stable both-edges" }}
        >
        <Box w="full" maxW="80rem" mx="auto">
        {/* Above the composer, because a session with a goal is one the person typing should see and be able to end. */}
        {activeGoal && <GoalBar goal={activeGoal} onClear={handleClearGoal} />}
        <ChatInput
          onSend={handleSend}
          onAbort={abort}
          isStreaming={isStreaming}
          // Two reasons the composer is closed, kept apart because they read as different things to the person looking at it.
          disabled={!isConnected || directoryPending}
          awaitingDecision={!!pendingPrompt}
          sessionId={sessionId}
          initialDraft={initialInputDraft}
          onDraftChange={handleInputDraftChange}
          workingDirectory={workingDirectory}
          directoryValid={directoryValid}
          agents={agents}
          selectedAgent={agent}
          onAgentChange={onAgentChange}
          models={models}
          modelProviders={modelProviders}
          recentModels={recentModels}
          agentModel={agentModel}
          onAgentModelChange={onAgentModelChange}
          permissionMode={effectivePermissionMode}
          onPermissionModeChange={handlePermissionModeChange}
          sandboxEnforce={sandboxEnforce}
          sandboxBackend={sandboxBackend.backend}
          onSandboxEnforceChange={onSandboxEnforceChange}
          tokenUsage={tokenUsage}
          onCompact={compact}
          isCompacting={isCompacting}
        />
        </Box>
        </Box>
        )}
      </Flex>

      {/* The right region tiles every open panel into a resizable grid, flush to the chat with an overlapping handle. */}
      <AnimatePresence initial={false}>
      {sidePanels.length > 0 && (
        <MotionBox
          key="panel-region"
          data-layout="side-panel-region"
          flexShrink={0}
          h="100%"
          w={{ base: "100%", md: `min(${sidePanelWidth}px, 55%)` }}
          minW={{ base: "100%", md: "min(360px, 55%)" }}
          maxW={{ base: "100%", md: "80vw" }}
          pr={2}
          // Full-screen at `base`, so its bottom edge is the device's rather than the panel's.
          pb={{ base: "calc(var(--safe-bottom, 0px) + 0.5rem)", md: 2 }}
          position={{ base: "absolute", md: "relative" }}
          inset={{ base: 0, md: "auto" }}
          zIndex={{ base: 3, md: "auto" }}
          // The same slide and fade as the history sidebar, mirrored, with only transform and opacity animating.
          initial={{ opacity: 0, x: 24 }}
          animate={{ opacity: 1, x: 0 }}
          exit={{ opacity: 0, x: 24 }}
          transition={{ duration: 0.2, ease: [0.4, 0, 0.2, 1] }}
        >
          <Box
            display={{ base: "none", md: "block" }}
            position="absolute"
            top={0}
            bottom={0}
            left={-1}
            w={2}
            cursor="col-resize"
            zIndex={1}
            onPointerDown={startSidePanelResize}
          />
            <PanelTiles gap={8} panels={sidePanels} />
        </MotionBox>
      )}
      </AnimatePresence>

      <SettingsDialog
        open={settingsOpen}
        onOpenChange={setSettingsOpen}
        section={settingsSection}
        onSectionChange={setSettingsSection}
        workspaceId={workspaceId}
        workingDirectory={workingDirectory}
        models={models}
        modelProviders={modelProviders}
        recentModels={recentModels}
        agents={agents}
        selectedAgent={agent}
        onAgentChange={onAgentChange}
        livePermissionMode={permissionMode}
        onPermissionModeChange={handlePermissionModeChange}
        liveSandboxEnforce={sandboxEnforce}
        sandboxBackend={sandboxBackend}
        onSandboxEnforceChange={onSandboxEnforceChange}
        liveWorktreeStrategy={worktreeStrategy}
        onWorktreeStrategyChange={onWorktreeStrategyChange}
      />

      <ConfirmDialog
        open={deleteConfirmOpen}
        onOpenChange={setDeleteConfirmOpen}
        title={translation("deleteSessionConfirmTitle")}
        confirmLabel={translation("delete")}
        confirmIcon={<LuTrash2 size={14} />}
        danger
        onConfirm={() => { if (sessionId) onDeleteSession?.(sessionId); }}
      >
        {translation("deleteSessionConfirmBody")}
      </ConfirmDialog>
    </Flex>
    </AgentNamesProvider>
  );
}
