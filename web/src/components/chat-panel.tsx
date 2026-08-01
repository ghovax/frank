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
  Span,
  Text,
  VStack,
} from "@chakra-ui/react";
import { LuAppWindow, LuArrowDown, LuChevronLeft, LuChevronRight, LuClock, LuDownload, LuEllipsis, LuFile, LuFolderOpen, LuHistory, LuMaximize2, LuMinimize2, LuMessageSquare, LuMoon, LuMousePointerClick, LuPanelLeftClose, LuPanelLeftOpen, LuRotateCcw, LuRotateCw, LuSettings, LuSun, LuTerminal, LuTrash2, LuTriangleAlert, LuX } from "react-icons/lu";
import { AnimatePresence, motion } from "motion/react";
import { FadeIn } from "@/components/ui/fade-in";
import { useFormatter, useTranslations } from "next-intl";
import { toaster } from "@/components/ui/toaster";
import { PanelTiles, type TilePanel } from "./panel-tiles";
import { useColorMode } from "./ui/color-mode";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type PointerEvent } from "react";
import { useChat, type ChatMessage } from "@/lib/use-chat";
import { ChatMessageItem, ChatToolGroup } from "./chat-message";
import { InlineField } from "./ui/display";
import { PanelTab } from "./ui/panel-tab";
import { PanelCard, PanelHeader, PanelEmptyState, TOP_BAR_HEIGHT } from "@/components/ui/panel";
import { SegmentedToggle } from "@/components/ui/segmented-toggle";
import { ChatInput } from "./chat-input";
import { QuestionOverlay } from "./question-overlay";
import { SettingsDialog, type SettingsSection } from "./settings-dialog";
import { BackgroundJobsPanel } from "./background-jobs-panel";
import { GitStatusBar } from "./git-status-bar";
import { LocationChip } from "./location-status";
import { SectionHeader } from "./ui/section-header";
import { CONCEPT_ICONS } from "@/lib/concept-icons";
import { useDirectoryStatus } from "@/lib/use-directory-status";
import { Tooltip } from "./ui/tooltip";
import { ToolbarAction } from "@/components/ui/toolbar";
import { DropdownMenu } from "@/components/ui/menu";
import { PermissionOverlay } from "./permission-overlay";
import { AgentSkills } from "./agent-skills";
import { getToolCallDisplay, type ToolDisplayTranslator } from "@/lib/tool-display";
import type { ToolPermission, ToolQuestion } from "@/lib/tool-event";

import { fetchSettings, getWorkspace, revealInFinder, saveSessionDraft, saveSettings, setSessionPermissionMode, subscribeEvents, type AgentCard, type AgentSummary, type Location, type PermissionMode, type SandboxEnforce, type WorktreeStrategy } from "@/lib/api";
import { PdfDocumentView } from "./pdf-view";
import { scrollFade, scrollFadeTopBottom } from "@/lib/scroll-fade";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { playAttentionSound, playTurnEndSound } from "@/lib/sounds";
import { closePermissionNotification, notifyPermissionRequest, setPermissionNotificationHandler } from "@/lib/notify";
import { swallowed } from "@/lib/swallowed";
import { errorMessage } from "@/lib/errors";

// A Chakra Box that is also a motion component, so the right panel region can
// animate its open/close (opacity + slide) exactly like the history sidebar on
// the left — without losing its flex-layout props.
const MotionBox = motion.create(Box);

type SidePanelKey = "background";

const MAXIMUM_OPEN_SIDE_PANELS = 2;


interface ChatPanelProps {
  agent: string;
  agents: AgentSummary[];
  agentCard?: AgentCard | null;
  onAgentChange: (agent: string) => void;
  // When set, the workspace opens with the Settings dialog already showing this section
  // (the Workspaces-home cog links here via `?settings=…`).
  initialSettingsSection?: string;
  initialSessionId: string | null;
  // The session's display title (LLM-generated once the conversation has one),
  // shown in the top bar. Absent until the session names itself.
  sessionTitle?: string;
  initialInputDraft?: string;
  // Deletes the session by id (aborts it, drops its tasks and record, then routes
  // the user back to a blank chat). Absent when there is no active session.
  onDeleteSession?: (sessionId: string) => void;
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
  isConnected?: boolean;
  onStreamingChange?: (isStreaming: boolean) => void;
  historyOpen?: boolean;
  onToggleHistory?: () => void;
  models?: { id: string; name: string; provider: string; available: boolean }[];
  modelProviders?: { id: string; name: string; openai_compatible: boolean }[];
  recentModels?: { id: string; name: string; provider: string }[];
  agentModel?: string;
  onAgentModelChange: (modelIdentifier: string) => void | Promise<void>;
  compactionKeepRecentTurns: number;
}

type TimelineItem =
  | { kind: "message"; message: ChatMessage }
  // A tool_group with no messages is a reasoning ("thinking") phase. `thinkingTurns`
  // records whether reasoning exists so a standalone Thinking row can be retained.
  | { kind: "tool_group"; id: string; messages: ChatMessage[]; thinkingTurns: number };

function folderDisplayName(workingDirectory?: string): string {
  const directory = (workingDirectory ?? "").trim();
  if (!directory) return "";
  return directory.split(/[\\/]/).filter(Boolean).at(-1) ?? directory;
}

function timelineItems(messages: ChatMessage[]): TimelineItem[] {
  const items: TimelineItem[] = [];
  let index = 0;
  // The first reasoning phase seen since the last non-thinking, non-tool row. It
  // belongs to the tool batch it leads into: its id keys the group so the
  // tools-less "thinking" heading and the tool group it becomes are the SAME
  // element — the tools stream into the existing row instead of one row being
  // swapped for another (which would flash a remount). A prose or user row that
  // isn't a tool group discards it.
  let pendingThinkingId: string | null = null;
  // Count reasoning messages so a tools-less Thinking group can be distinguished from
  // an empty group.
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
    // The leading reasoning that led into this batch keys the group (stable from
    // the pre-tool "thinking" heading onward). Reasoning phases are tallied from the
    // leading ones plus any interleaved between this group's calls.
    const groupKey = pendingThinkingId;
    let thinkingTurns = pendingThinkingTurns;
    pendingThinkingId = null;
    pendingThinkingTurns = 0;
    // Gather contiguous tool calls. Reasoning ("thinking") is hidden from the
    // timeline, so it must not split a run of tool calls either — otherwise two
    // calls issued in successive iterations (each preceded by its own thinking)
    // would render as separate entries instead of one group.
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
      // Prefer the leading thinking id so the key is stable across the
      // thinking→tools transition; fall back to the first tool otherwise.
      id: groupKey ?? toolMessages[0].id,
      messages: toolMessages,
      thinkingTurns,
    });
  }
  // A reasoning phase at the tail surfaces as the live "Thinking" status line.
  // The item is emitted here unconditionally; ToolGroup renders it only while
  // the turn is live (keepOpen), so settled reasoning leaves no row behind.
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
  initialPermissionMode = "default",
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
  onStreamingChange,
  historyOpen = false,
  onToggleHistory,
  models = [],
  modelProviders = [],
  recentModels = [],
  agentModel = "",
  onAgentModelChange,
  compactionKeepRecentTurns,
}: ChatPanelProps) {
  const translation = useTranslations("ChatPanel");
  const tToolDisplay = useTranslations("ToolDisplay") as unknown as ToolDisplayTranslator;
  const format = useFormatter();
  const [permissionMode, setPermissionModeState] = useState<PermissionMode>(initialPermissionMode);
  const { messages, tokenUsage, queuedMessages, sessionId, isStreaming, isHistoryLoading, historyError, reloadHistory, send, abort, dequeueMessage, handlePermission, handleQuestion, declineQuestion, compact } =
    useChat(agent, initialSessionId, workingDirectory, worktreeStrategy, permissionMode, sessionRunning, workspaceId);

  // Single source of truth for the working directory's validity and Git status —
  // consumed by the workspace status bar (branch/dirty/ahead-behind) and passed to the
  // composer as `directoryValid` for its send-gate.
  const { status: directoryStatus, directoryValid } = useDirectoryStatus(workingDirectory);
  // Whether the chat body has resolved enough to render without flashing: connected,
  // and the working directory's validity determined (not mid-check). Until then we show
  // a neutral placeholder instead of the empty welcome and a disabled→enabled input, so
  // opening a workspace doesn't flicker.
  const trimmedWorkingDirectory = (workingDirectory ?? "").trim();
  const directoryPending = !!trimmedWorkingDirectory && (directoryStatus.checking || directoryStatus.path !== trimmedWorkingDirectory);
  const chatReady = isConnected && !directoryPending;

  // The workspace's locations, for the terminal location picker (and any location-aware
  // panels). Refreshed live when the workspace config changes.
  const [workspaceLocations, setWorkspaceLocations] = useState<Location[]>([]);
  useEffect(() => {
    let cancelled = false;
    // Resolving through a promise (even for the empty-workspace case) keeps the state
    // update off the synchronous effect path, so an empty workspace clears locations
    // on the next microtask rather than mid-render.
    const load = () => {
      const request = workspaceId ? getWorkspace(workspaceId) : Promise.resolve(null);
      request.then((workspace) => { if (!cancelled) setWorkspaceLocations(workspace?.locations ?? []); }).catch((caught) => swallowed({ component: "chat-panel", operation: "read a workspace" }, caught));
    };
    load();
    const unsubscribe = subscribeEvents((event) => { if (event.type === "workspaces_changed") load(); });
    return () => { cancelled = true; unsubscribe(); };
  }, [workspaceId]);

  // On mount, fetch the stored permission mode from the server settings. This
  // overrides the "default" fallback when no session is active, so the user's
  // last choice persists across page reloads and new sessions.
  useEffect(() => {
    if (initialSessionId) return;
    let cancelled = false;
    fetchSettings().then((settings) => {
      if (cancelled || settings.permission_mode === permissionMode) return;
      setPermissionModeState(settings.permission_mode);
    }).catch((caught) => swallowed({ component: "chat-panel", operation: "read the settings" }, caught));
    return () => { cancelled = true; };
  // Only run when there is no session — once the session is set, the session's own
  // permission_mode is authoritative.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialSessionId]);

  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const scrollContentRef = useRef<HTMLDivElement>(null);
  // "Following" the bottom. Released the moment the user scrolls up and resumed
  // only when they return to the bottom — so auto-scroll never grabs them.
  const isPinnedRef = useRef(true);
  const lastScrollTopRef = useRef(0);
  const onStreamingChangeRef = useRef(onStreamingChange);
  // Snapshot of the previous layout pass. Comparing the first key and the count
  // tells a *top* prepend (an older page loading in) from a *bottom* append (a new
  // turn), so the former can preserve the reader's exact viewport instead of
  // jumping. Seeded on the first non-loading pass.
  const scrollMetricsRef = useRef({ scrollHeight: 0, firstKey: "", count: 0 });
  const notifiedSessionIdRef = useRef<string | null>(null);
  // Whether the turn is currently paused on a pending decision (a permission or
  // question prompt on a tool call). Read via a ref inside handleSend so a new
  // message is queued rather than steered while a decision is outstanding.
  const hasInputRequiredRef = useRef(false);
  const [openSidePanels, setOpenSidePanels] = useState<SidePanelKey[]>([]);
  const backgroundPanelOpen = openSidePanels.includes("background");
  // Default right-region width: comfortable for one panel without dwarfing the
  // transcript (pairs with the sidebar default of 240 in page.tsx). Drag grows it.
  const [sidePanelWidth, setSidePanelWidth] = useState(480);
  const { colorMode, toggleColorMode } = useColorMode();
  // Whether the transcript is scrolled to (or near) the bottom. Drives the floating
  // "jump to latest" affordance so a reader who scrolled up to read history can
  // return to the live tail in one click instead of scrolling all the way down.
  const [isAtBottom, setIsAtBottom] = useState(true);
  // A strict at-bottom flag (small threshold) that drives the transcript's bottom fade:
  // the content fades above the composer only while there is more below the fold, and the
  // fade lifts the moment the reader reaches the bottom so the last line is never dimmed.
  const [transcriptPinned, setTranscriptPinned] = useState(true);
  // Top-bar surfaces: the settings dialog, the delete-session confirmation, and the
  // background-processes sheet all open from the persistent bar above the transcript.
  // Open Settings on mount when the workspace was entered with `?settings=<section>`.
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
    setOpenSidePanels((currentPanels) => {
      const remainingPanels = currentPanels.filter((openPanel) => openPanel !== panel);
      if (!open) return remainingPanels;
      return [...remainingPanels, panel].slice(-MAXIMUM_OPEN_SIDE_PANELS);
    });
  }, []);

  useEffect(() => {
    if (openSidePanels.length === 0 || !historyOpen || !window.matchMedia("(max-width: 1199px)").matches) return;
    onToggleHistory?.();
  }, [openSidePanels.length, historyOpen, onToggleHistory]);

  const markSidePanelActive = useCallback((panel: SidePanelKey) => {
    setOpenSidePanels((currentPanels) => {
      if (!currentPanels.includes(panel) || currentPanels[currentPanels.length - 1] === panel) return currentPanels;
      return [...currentPanels.filter((openPanel) => openPanel !== panel), panel];
    });
  }, []);

  // Pinned == the viewport is at (or within a hair of) the bottom. That single
  // fact drives everything: pinned means follow new content, unpinned means the
  // reader has scrolled up to read history and must never be pulled back down.
  const handleScroll = useCallback(() => {
    const container = scrollContainerRef.current;
    if (!container) return;
    const distanceFromBottom = container.scrollHeight - (container.scrollTop + container.clientHeight);
    const atBottom = distanceFromBottom <= 8;
    isPinnedRef.current = atBottom;
    setTranscriptPinned(atBottom);
    // A larger threshold for showing the jump button than for "pinned": the button
    // should not flash for a hair of scroll, but pinning must stay strict so the
    // live tail never fights a reader who nudged up a pixel.
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
    // Which agent runs is never assumed. A session cannot be created without one, and there
    // is no default to reach for, so an unchosen agent is asked for rather than guessed at.
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
    // Queue (never steer) while a decision prompt is outstanding — see hasInputRequiredRef.
    const result = send(text, dataParts, hasInputRequiredRef.current);
    scrollToBottom();
    return result;
  }, [agent, initialSessionId, scrollToBottom, send, translation]);

  const openSettings = useCallback((section: SettingsSection) => {
    setSettingsSection(section);
    setSettingsOpen(true);
  }, [setSettingsOpen, setSettingsSection]);

  useEffect(() => {
    if (!sessionId || notifiedSessionIdRef.current === sessionId) return;
    notifiedSessionIdRef.current = sessionId;
    onSessionCreated(sessionId);
  }, [sessionId, onSessionCreated]);

  // The single owner of scroll position. It runs before paint on every content
  // change and classifies it: a *top* prepend (an older page loading in) preserves
  // the reader's exact viewport by the height delta; everything else (a new turn,
  // streamed text) follows the bottom, but only while pinned. This one effect
  // replaces the old anchor + rAF tangle that fought itself and yanked the reader.
  useLayoutEffect(() => {
    const container = scrollContainerRef.current;
    if (!container || isHistoryLoading) return;
    const firstKey = messages.length > 0 ? messages[0].id : "";
    const count = messages.length;
    const previous = scrollMetricsRef.current;
    const prepended = count > previous.count && firstKey !== previous.firstKey && previous.count > 0;
    if (prepended) {
      // Older messages landed above the viewport — shift down by exactly how much
      // taller the content got, so what the reader is looking at stays fixed (and a
      // bottom-pinned view stays pinned). This is what makes background paging
      // invisible.
      const delta = container.scrollHeight - previous.scrollHeight;
      if (delta !== 0) container.scrollTop = container.scrollTop + delta;
    } else if (isPinnedRef.current) {
      container.scrollTop = container.scrollHeight;
    }
    scrollMetricsRef.current = { scrollHeight: container.scrollHeight, firstKey, count };
    lastScrollTopRef.current = container.scrollTop;
  }, [messages, queuedMessages, isHistoryLoading]);

  // Late-growing content (images, streamed text) keeps the bottom in view
  // — but only while pinned, so a reader who scrolled up is never dragged down.
  // Re-runs when the transcript container actually mounts (it is absent during the
  // loading/empty states), otherwise the observer would never attach.
  const timelineMounted = !isHistoryLoading && !historyError && messages.length > 0;
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

  // Follow the session the parent selected. A render-phase prop-change adjustment (the
  // same pattern as previousActiveSteps below) rather than an effect, to stay lint-clean.
  const [previousInitialSession, setPreviousInitialSession] = useState({ id: initialSessionId, permissionMode: initialPermissionMode });
  if (initialSessionId !== previousInitialSession.id || initialPermissionMode !== previousInitialSession.permissionMode) {
    setPreviousInitialSession({ id: initialSessionId, permissionMode: initialPermissionMode });
    setPermissionModeState(initialPermissionMode);
  }

  useLayoutEffect(() => {
    scrollMetricsRef.current = { scrollHeight: 0, firstKey: "", count: 0 };
    isPinnedRef.current = true;
  }, [initialSessionId]);

  // New content is followed by the layout effect above (only while pinned); this
  // surfaces the streaming flag to the parent. The initial jump-to-bottom is also
  // handled there: pinned starts true, so the first post-load pass lands at the
  // bottom instantly. The turn-end chime lives further down, where it can see
  // whether anything is waiting on the user.
  useEffect(() => {
    onStreamingChangeRef.current?.(isStreaming);
  }, [isStreaming]);

  // Two things at once, because the control is one control. Without a session this only
  // chooses what the next one starts under; with a session it also changes *that* session,
  // which the daemon applies to the turn in flight. The chip moves immediately and is
  // corrected if the server clamps the mode (a child is never looser than its parent).
  function handlePermissionModeChange(nextMode: PermissionMode) {
    setPermissionModeState(nextMode);
    onPermissionModeChange?.(nextMode);
    // Persist to server settings so it survives across sessions.
    saveSettings({ permission_mode: nextMode }).catch((caught) => swallowed({ component: "chat-panel", operation: "save the settings" }, caught));
    if (!sessionId) return;
    setSessionPermissionMode(sessionId, nextMode)
      .then((applied) => setPermissionModeState(applied))
      .catch((caught) => {
        // The session kept the mode it had, so the chip must go back to saying so rather
        // than showing a policy that is not being enforced.
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
  // Entrance animation is reserved for rows a *live turn* just appended at the
  // bottom — never the initial load or a background history prepend, which arrive
  // in bulk (and, for prepends, above the fold). The rule is purely positional and
  // so immune to state-flag timing: a row animates only if its key has never been
  // seen AND every row after it is also unseen (i.e. it is part of the trailing run
  // of brand-new rows at the end of the list). The first population seeds the set
  // with everything, so nothing animates on load. `enteredKeysRef` persists across
  // renders; the panel remounts per session (page.tsx keys it), so it resets then.
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
    // Only when this is the same transcript with something added. When a turn ends, the
    // snapshot replays it from the store and every row is rebuilt under a different id — the
    // live tail numbers rows by position, the replay numbers them by message. Nothing has
    // changed on screen, but no key survives, and treating that as "all new" re-animated the
    // whole conversation: the flash after an answer lands, with the thinking row fading in
    // again under the reply it already produced.
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
  // Running shell commands drive the badge on the top-bar background-processes
  // button, so a long-running bash call is visible without opening the sheet.
  const runningShellCount = messages.filter((message) =>
    message.role === "tool_call" && message.content === "bash" &&
    (message.meta?.status === "running" || message.meta?.status === "input_required")
  ).length;
  // A compaction pass is live while its timeline marker is still "running" — drives
  // the Compact control's in-progress state (spinner + disabled).
  const isCompacting = messages.some(
    (message) => message.role === "compaction" && message.meta?.status === "running"
  );
  // "Reveal" opens the session's working directory in the OS file manager.
  const revealPath = (workingDirectory || "").trim();

  // "Try again" on a turn-error box re-runs the turn that failed by resending the
  // most recent user message. The failed turn produced no lasting state, so a
  // plain resend is the correct retry (a rate limit or provider blip clears on its
  // own; a rejected request goes back through the same path).
  const handleRetry = useCallback(() => {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const candidate = messages[index];
      if (candidate.role === "user" && candidate.content.trim()) {
        send(candidate.content);
        return;
      }
    }
  }, [messages, send]);

  // A tool call awaiting the user's approval or answer pauses the turn. While it is
  // outstanding, the composer may only queue (see handleSend) and Stop auto-denies it.
  // Deliberately not gated on `isStreaming`. A turn parked on a permission is *not* running —
  // the daemon sleeps the session, because the whole turn is checkpointed and holding an
  // interpreter to wait for a person is what sleeping exists to avoid. So the one moment a
  // decision must be shown is the moment the turn stops. Requiring a live turn hid every
  // prompt behind the thing that raised it.
  const hasInputRequired = messages.some(
    (message) => message.role === "tool_call" && message.meta?.status === "input_required"
  );
  useEffect(() => {
    hasInputRequiredRef.current = hasInputRequired;
  }, [hasInputRequired]);
  // The first pending input-required prompt (an ask_user question or a permission
  // approval), surfaced as an overlay above the input. Both live outside the tool
  // card so a pending decision always grabs attention at the bottom of the chat,
  // and resolving one reveals the next. A question takes precedence on the same
  // card, though in practice a card carries only one.
  let pendingPrompt: (
    | { kind: "question"; question: ToolQuestion }
    | { kind: "permission"; permission: ToolPermission; title: string; detail?: string; command?: string; arguments?: Record<string, unknown> }
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
          // Two different answers to two different questions, and a person deciding wants
          // both: the title says what the agent is trying to do and why it wants to (the
          // model's own `explanation`, via the same display helper the tool card uses), and
          // the detail says what made this stop for approval.
          title: getToolCallDisplay(name, args, tToolDisplay).label,
          detail: permission.explanation || undefined,
          command: command || undefined,
          arguments: args,
        };
        break;
      }
    }
  }
  // Audio + system-notification side of a pending decision. The attention cue
  // plays for the first prompt in a turn, while later prompts stay silent; a
  // permission prompt additionally raises a system notification carrying the
  // overlay's own primary action ("Allow once") as its action button — shown
  // only while the window is unfocused, and retracted the moment the request
  // resolves or is superseded, so nothing stale lingers in the notification
  // center. Strings reuse the overlay's, so the two surfaces can never drift.
  const tPermission = useTranslations("PermissionOverlay");
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

  // The turn-end chime, on the transition to *actually finished* — the moment the composer
  // goes back from Stop to Send. A turn that pauses for a permission or a question is not a
  // turn that ended: `isStreaming` drops while it waits, so this used to fire the end cue and
  // then the attention cue a beat later, two sounds for one event, several times a turn.
  // Waiting counts as still running, so the transition is still there to catch when the answer
  // comes and the turn really does finish.
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
      title: tPermission("approvalNeeded"),
      body: pendingPermissionBody,
      actionLabel: tPermission("allowOnce"),
    });
  }, [pendingPermissionId, pendingPermissionBody, tPermission]);
  // The notification's action button resolves the request exactly like the
  // overlay's primary button would.
  useEffect(() => {
    setPermissionNotificationHandler((requestId) => handlePermission(requestId, "allow_once"));
    return () => setPermissionNotificationHandler(null);
  }, [handlePermission]);

  const handleSidePanelResizeStart = useCallback((event: PointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = sidePanelWidth;

    function handlePointerMove(moveEvent: globalThis.PointerEvent) {
      // Clamp to the same bounds the region's CSS enforces (minW 360 / maxW 80vw,
      // capped at 900) so the drag can never fight the styled limits.
      const nextWidth = Math.min(Math.min(900, Math.round(window.innerWidth * 0.8)), Math.max(360, startWidth + startX - moveEvent.clientX));
      setSidePanelWidth(nextWidth);
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
  }, [sidePanelWidth]);

  return (
    <Flex h="100%" minW={0} position="relative">
      <Flex direction="column" flex={1} minW={0} h="100%">
        {/* Persistent top bar: session identity on the left, session tools on the
            right. Always visible so the controls have a stable home; the title
            fills in once the session names itself, matching the sidebar default
            until then. */}
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
            <ToolbarAction
              label={translation("terminalAndBackground")}
              icon={<LuTerminal size={14} />}
              active={backgroundPanelOpen}
              colorPalette="green"
              indicator={runningShellCount > 0}
              onClick={() => setSidePanelOpen("background", !backgroundPanelOpen)}
            />
            {/* Light/dark, switched here rather than only from three screens into Settings.
                It is the one setting people change on a whim — because the room got dark, not
                because they are configuring anything — so it belongs where they already are.
                The label names what the click does, not the state it is in. */}
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
          {!chatReady || isHistoryLoading ? (
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
            // Empty welcome ↔ message timeline is a true cross-fade, so sending the first message
            // never flashes. `mode="popLayout"` pops the exiting welcome out of flow (position it
            // absolute) so the timeline — carrying the just-sent message — takes its place at once
            // and fades in while the welcome fades out over it; there is no blank frame between the
            // two (which `mode="wait"` left). `initial={false}` keeps an existing session's load
            // un-animated: only the empty→timeline transition fades.
            <AnimatePresence mode="popLayout" initial={false}>
              {messages.length === 0 ? (
                <motion.div key="empty" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.15, ease: "easeOut" }} style={{ width: "100%", flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}>
                  {/* The same `80rem` centred column as the transcript, the composer and the
                      approval overlay. Without it the welcome grew with the window while
                      everything else stayed put, so on a wide display the skills and tools ran
                      far outside the chat. No `px` of its own either: the scroller already pads,
                      and a second inset put this list 10px inside the composer instead of flush.

                      Vertically it sits in the middle of whatever room there is, rather than
                      being pushed down by a fixed inset. It used to carry 80px above and 48px
                      below on a desktop width — numbers that were only ever right for one window
                      height, so on a tall window the block hung near the top under a band of
                      nothing, and on a short one those 128px pushed the skills under the fold and
                      made an empty conversation scroll.

                      `my="auto"` rather than `justify="center"` on purpose: this lives in a
                      scroll container, and auto margins are defined to collapse to zero when the
                      free space runs out, so tall content still starts at the top and stays
                      reachable instead of being centred off the top edge. What is left is a small
                      floor, which only applies when there is nothing to spare. */}
                  {/* One rhythm for the sections, and it is `6` because that is the gap the
                      capability sections keep between themselves (`AgentSkills` puts `mt={6}`
                      between skills and tools). Set to anything else here — it was `10` — and
                      the first gap is wider than the ones after it, so the environments read as
                      belonging to the heading rather than as the first of three peers. The
                      heading keeps its own extra space below, because a title is not a peer of
                      the sections under it. */}
                  <Flex direction="column" align="stretch" gap={6} w="full" maxW="80rem" mx="auto" my="auto" py={{ base: 4, md: 6 }}>
                    {/* The blank-conversation state inside a workspace: no brand lockup (that lives
                        on the Workspaces home) — the build prompt, then what this workspace can
                        reach and what the agent can do, as sections. */}
                    <Heading as="h2" fontSize="3xl" fontWeight="semibold" textAlign="center" mb={4}>
                      {translation("buildPrompt", { folder: currentFolderName })}
                    </Heading>

                    {/* The environments read as a section, like the skills and tools under them,
                        rather than as a centred caption hanging off the heading. They are the same
                        kind of thing — what this conversation has available — so they get the same
                        grammar: the section's own icon, a left-aligned heading with a line saying
                        what it is, and the list beneath it. Centred and unlabelled, sitting tight
                        under the title, it read as a subtitle of the question instead. */}
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
                  {/* gap 2.5 (10px): tight enough that a tool-activity line and the prose
                      around it read as one document, while user bubbles — carrying their own
                      fill — still mark the turn boundaries. */}
                  <VStack ref={scrollContentRef} gap={2.5} align="stretch" w="full" maxW="80rem" mx="auto">
                    {/* No `AnimatePresence` around these rows, and that is the point rather than
                        an omission. Its whole job is to keep a removed child mounted long enough
                        to animate it out, and a transcript row must never animate out — see
                        `FadeIn`. Left in place it would do nothing except offer the next person a
                        working `exit` prop, which is how the transcript acquired its snap-back in
                        the first place: `popLayout` was added to contain a jump, the jump was
                        actually one tool call rendering as two rows, and the containment hid the
                        real bug for weeks. A row appears when it exists and is gone when it does
                        not. */}
                    {renderedTimeline.map((item, itemIndex) => {
                        const isLastItem = itemIndex === renderedTimeline.length - 1;
                        const key = item.kind === "tool_group" ? item.id : item.message.id;
                        // A tools-less group is a reasoning phase; it stays in the transcript as a
                        // persistent "Thinking" row (its own record of the planning that happened),
                        // so it is only skipped if it somehow carries neither tools nor thinking.
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
                        // The assistant message streams its content in (the markdown animates
                        // token by token), so its wrapper stays a plain, stable row — an entrance
                        // animation on top of the streaming text would fight it. User messages and
                        // tool-call groups, though, are complete the moment they appear, so they get
                        // a single gentle fade. `animatedKeys` limits it to rows a live turn
                        // just appended (never load or history), and `initial` only fires on mount,
                        // so a tool group fades once — not again as its calls fill in.
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
                    {queuedMessages.map((message, index) => (
                      <Flex key={message.id} align="flex-start" alignSelf="flex-end" maxW="80%" gap={1.5}>
                        <IconButton
                          aria-label={translation("deleteQueuedMessage")}
                          variant="ghost"
                          colorPalette="red"
                          mt={0.5}
                          flexShrink={0}
                          onClick={() => dequeueMessage(index)}
                        >
                          <LuTrash2 size={13} />
                        </IconButton>
                        <Box
                          px={2}
                          py={1.5}
                          borderRadius="md"
                          border="1px dashed"
                          borderColor="border"
                          bg="bg.subtle"
                          opacity={0.7}
                          flex={1}
                          minW={0}
                        >
                          {/* Only a queued message needs saying so — it is waiting, and the
                              label is the reason it has not been answered. A steering message
                              is one the user has just typed at a running turn, which they know,
                              so naming it told them something they had done a second earlier. */}
                          {message.steering ? null : (
                            <Flex align="center" gap={1.5}>
                              <Span display="inline-flex" alignItems="center">
                                <LuClock size={11} />
                              </Span>
                              <Text textStyle="fieldLabel" color="fg.subtle">
                                {translation("queued")}
                              </Text>
                            </Flex>
                          )}
                          <Text fontSize="sm" color="fg.muted">{message.text}</Text>
                        </Box>
                      </Flex>
                    ))}
                  </VStack>
                </motion.div>
              )}
            </AnimatePresence>
          )}
        </Box>
        {!isAtBottom && !isHistoryLoading && !historyError && messages.length > 0 && (
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

        {/* The approval/question overlay sits in the same 80rem centered column as the messages
            and composer, so it reads as a card in the chat column instead of a bar spanning the
            whole panel. No overflow clipping here — that would slice the card's drop shadow. */}
        {/* One owner for the transition, keyed by the request being asked about. The overlays
            used to wrap themselves in an `AnimatePresence` whose child had no key, while this
            parent mounted and unmounted them outright — so exit could never run, and every new
            prompt replayed the entrance from nothing. `mode="wait"` makes a replacing prompt
            wait for the outgoing one to finish, so two decisions can never be on screen at
            once; `initial={false}` keeps a prompt that is already pending on load from
            animating in as though it had just arrived. */}
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
        {/* The composer wrapper mirrors the transcript scroll container's horizontal geometry
            — same px, and the scrollbar gutter reserved via overflow:hidden + scrollbar-gutter
            stable both-edges — so the input's 80rem column co-centers with the messages above it
            at every width. `both-edges` reserves the gutter symmetrically (left AND right) so the
            centered column stays on the panel's true centre and the left/right insets match,
            rather than a single-edge gutter nudging everything off-centre. */}
        {chatReady && (
        <Box px={4} overflowY="hidden" style={{ scrollbarGutter: "stable both-edges" }}>
        <Box w="full" maxW="80rem" mx="auto">
        <ChatInput
          onSend={handleSend}
          onAbort={abort}
          isStreaming={isStreaming}
          disabled={!isConnected || !!pendingPrompt}
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
          permissionMode={permissionMode}
          onPermissionModeChange={handlePermissionModeChange}
          sandboxEnforce={sandboxEnforce}
          sandboxBackend={sandboxBackend.backend}
          onSandboxEnforceChange={onSandboxEnforceChange}
          tokenUsage={tokenUsage}
          onCompact={compact}
          isCompacting={isCompacting}
          compactionKeepRecentTurns={compactionKeepRecentTurns}
          compactionUserCount={messages.filter((message) => message.role === "user").length}
        />
        </Box>
        </Box>
        )}
      </Flex>

      {/* The right region: every open panel tiles into a resizable 2D grid. It sits flush to
          the chat (no gutter column) so the chat content keeps symmetric left/right padding;
          the resize handle overlaps the boundary as an absolute strip rather than consuming a
          column of space — mirroring the left sidebar's handle. */}
      <AnimatePresence initial={false}>
      {backgroundPanelOpen && (
        <MotionBox
          key="panel-region"
          data-layout="side-panel-region"
          flexShrink={0}
          h="100%"
          w={{ base: "100%", md: `min(${sidePanelWidth}px, 55%)` }}
          minW={{ base: "100%", md: "min(360px, 55%)" }}
          maxW={{ base: "100%", md: "80vw" }}
          pr={2}
          pb={2}
          position={{ base: "absolute", md: "relative" }}
          inset={{ base: 0, md: "auto" }}
          zIndex={{ base: 3, md: "auto" }}
          // Same slide + fade (and timing) as the history sidebar on the left, mirrored:
          // the two edges of the window open and close as one family. Only transform and
          // opacity animate — the width is applied instantly, so the resize drag never
          // fights a tween and the transcript reflows exactly once per toggle.
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
            onPointerDown={handleSidePanelResizeStart}
          />
            <PanelTiles
              gap={8}
              panels={[
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
              ].filter(Boolean) as TilePanel[]}
            />
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
  );
}

