"use client";

import {
  Box,
  Button,
  Dialog,
  EmptyState,
  Flex,
  IconButton,
  Menu,
  Portal,
  Text,
  VStack,
} from "@chakra-ui/react";
import { LuAppWindow, LuArrowDown, LuCheck, LuClock, LuDownload, LuEllipsis, LuFolder, LuFolderOpen, LuHistory, LuMaximize2, LuMinimize2, LuMessageSquare, LuNavigation, LuNetwork, LuRotateCw, LuSettings, LuTerminal, LuTrash2, LuTriangleAlert, LuX } from "react-icons/lu";
import { AnimatePresence, motion } from "motion/react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type PointerEvent } from "react";
import { useChat, isStepDone, type ChatMessage } from "@/lib/use-chat";
import { ChatMessageItem, ChatToolGroup } from "./chat-message";
import { extractToolArtifacts, externalPreviewUrl, isLivePreviewArtifact, PreviewArtifact } from "./tool-views";
import { NativeWebview } from "./native-webview";
import { nativePreviewAvailable } from "@/lib/native-preview";
import { WidgetEventProvider, type WidgetEvent } from "./widget-bridge";
import { ChatInput } from "./chat-input";
import { QuestionOverlay } from "./question-overlay";
import { SettingsDialog, type SettingsSection } from "./settings-dialog";
import { BackgroundTasksPanel } from "./background-tasks-panel";
import { Tooltip } from "./ui/tooltip";
import { PermissionOverlay } from "./permission-overlay";
import { AgentsPanel } from "./agents-panel";
import { AgentSkills } from "./agent-skills";
import { getToolCallDisplay } from "@/lib/tool-display";
import type { ToolPermission, ToolQuestion } from "@/lib/tool-event";

import { setPermissionMode, fetchSettings, saveSettings, revealInFinder, type AgentCard, type AgentSummary, type PermissionMode, type WorkspaceStrategy } from "@/lib/api";
import type { ConnectionTarget } from "@/lib/connection";
import { ConnectionSwitcher } from "./connection-switcher";

const MotionFlex = motion.create(Flex);

interface ChatPanelProps {
  agent: string;
  agents: AgentSummary[];
  agentCard?: AgentCard | null;
  onAgentChange: (agent: string) => void;
  initialSessionId: string | null;
  // The session's display title (LLM-generated once the conversation has one),
  // shown in the top bar. Absent until the session names itself.
  sessionTitle?: string;
  // Deletes the session by id (aborts it, drops its tasks and record, then routes
  // the user back to a blank chat). Absent when there is no active session.
  onDeleteSession?: (sessionId: string) => void;
  initialPermissionMode?: PermissionMode;
  currentConnectionId?: string;
  onConnectionChange?: (target: ConnectionTarget) => void;
  onPermissionModeChange?: (mode: PermissionMode) => void;
  sessionRunning?: boolean;
  onSessionCreated: (sessionId: string) => void;
  onSlashCommand?: (command: string) => void;
  workingDirectory?: string;
  homeDirectory?: string;
  recentProjects?: { path: string; name: string }[];
  onWorkingDirectoryChange?: (dir: string) => void;
  onBrowseFolder?: () => void;
  sandboxEnabled?: boolean;
  onSandboxEnabledChange?: (enabled: boolean) => void;
  workspaceStrategy?: WorkspaceStrategy;
  workspaceBranch?: string;
  workspaceRuntimeDirectory?: string;
  workspaceRuntimeDirectoryName?: string;
  workspaceError?: string;
  onWorkspaceStrategyChange?: (strategy: WorkspaceStrategy) => void;
  isConnected?: boolean;
  onStreamingChange?: (isStreaming: boolean) => void;
  historyOpen?: boolean;
  onToggleHistory?: () => void;
  models?: { id: string; name: string; provider: string; available: boolean; curated: boolean }[];
  modelProviders?: { id: string; name: string; openai_compatible: boolean }[];
  recentModels?: { id: string; name: string; provider: string }[];
  selectedModel?: string;
  globalModel?: string;
  // The active agent's configured model identifier, used as the default the
  // selector falls back to when no per-session override is set.
  agentModel?: string;
  onModelChange?: (model: string) => void;
  compactionKeepRecentTurns: number;
}

type TimelineItem =
  | { kind: "message"; message: ChatMessage }
  | { kind: "tool_group"; id: string; messages: ChatMessage[]; thinkingCount: number };

function folderDisplayName(workingDirectory?: string, projects: { path: string; name: string }[] = []): string {
  const directory = (workingDirectory ?? "").trim();
  if (!directory) return "this folder";
  const known = projects.find((project) => project.path === directory)?.name;
  if (known) return known;
  return directory.split(/[\\/]/).filter(Boolean).at(-1) ?? directory;
}

function previewArtifactAddress(artifact: Record<string, unknown>): string {
  for (const key of ["source", "src", "url", "href", "file"]) {
    const value = artifact[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}

function timelineItems(messages: ChatMessage[], isStreaming = false): TimelineItem[] {
  const items: TimelineItem[] = [];
  let index = 0;
  // Reasoning phases seen since the last non-thinking, non-tool row. They belong to
  // the tool batch they lead into (surfaced as the group's brain counter); a prose
  // or user row that isn't a tool group discards them. The first such phase's id is
  // kept too: it keys the group so the tools-less "thinking" heading and the tool
  // group it becomes are the SAME element — the tools stream into the existing card
  // instead of one card being swapped for another (which would flash a remount).
  let pendingThinking = 0;
  let pendingThinkingId: string | null = null;
  while (index < messages.length) {
    const message = messages[index];
    if (message.role === "thinking") {
      if (pendingThinking === 0) pendingThinkingId = message.id;
      pendingThinking += 1;
      index += 1;
      continue;
    }
    if (message.role !== "tool_call") {
      items.push({ kind: "message", message });
      pendingThinking = 0;
      pendingThinkingId = null;
      index += 1;
      continue;
    }

    const toolMessages: ChatMessage[] = [];
    // The leading reasoning that led into this batch counts toward it too, and its
    // id keys the group (stable from the pre-tool "thinking" heading onward).
    let thinkingCount = pendingThinking;
    const groupKey = pendingThinkingId;
    pendingThinking = 0;
    pendingThinkingId = null;
    // Gather contiguous tool calls. Reasoning ("thinking") is hidden from the
    // timeline, so it must not split a run of tool calls either — otherwise two
    // calls issued in successive iterations (each preceded by its own thinking)
    // would render as separate entries instead of one group. Each interleaved
    // reasoning phase is tallied into the group's brain counter.
    while (index < messages.length) {
      const next = messages[index];
      if (next.role === "tool_call") {
        toolMessages.push(next);
        index += 1;
      } else if (next.role === "thinking") {
        thinkingCount += 1;
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
      thinkingCount,
    });
  }
  // A reasoning phase at the tail of a live turn surfaces as a tools-less group
  // heading — the brain indicator the moment it starts, not only once the first
  // tool lands. It is keyed by the same leading-thinking id the tool group will
  // use, so when the first tool arrives the card is updated in place, not replaced.
  // Keep it through the live stream even if the backend has already sent
  // thinking_done; otherwise it flashes away in the gap before the next event.
  if (pendingThinking > 0 && pendingThinkingId) {
    const last = messages[messages.length - 1];
    if (last && last.role === "thinking" && (last.meta?.status === "running" || isStreaming)) {
      items.push({ kind: "tool_group", id: pendingThinkingId, messages: [], thinkingCount: pendingThinking });
    }
  }
  return items;
}

export function ChatPanel({
  agent,
  agents,
  agentCard,
  onAgentChange,
  initialSessionId,
  sessionTitle,
  onDeleteSession,
  initialPermissionMode = "default",
  currentConnectionId,
  onConnectionChange,
  onPermissionModeChange,
  sessionRunning = false,
  onSessionCreated,
  workingDirectory,
  homeDirectory,
  recentProjects,
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
  isConnected = false,
  onStreamingChange,
  historyOpen = false,
  onToggleHistory,
  models = [],
  modelProviders = [],
  recentModels = [],
  selectedModel = "",
  globalModel = "",
  agentModel = "",
  onModelChange,
  compactionKeepRecentTurns,
}: ChatPanelProps) {
  const [permissionMode, setPermissionModeState] = useState<PermissionMode>(initialPermissionMode);
  const { messages, agentGroups, tasks, tokenUsage, queuedMessages, sessionId, isStreaming, isHistoryLoading, historyError, reloadHistory, send, sendWidgetEvent, abort, dequeueMessage, handlePermission, handleQuestion, declineQuestion, compact } =
    useChat(agent, initialSessionId, workingDirectory, workspaceStrategy, permissionMode, selectedModel, sessionRunning);

  // On mount, fetch the stored permission mode from the server settings. This
  // overrides the "default" fallback when no session is active, so the user's
  // last choice persists across page reloads and new sessions.
  useEffect(() => {
    if (initialSessionId) return;
    let cancelled = false;
    fetchSettings().then((settings) => {
      if (cancelled || settings.permission_mode === permissionMode) return;
      setPermissionModeState(settings.permission_mode);
    }).catch(() => {});
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
  // Keys that have already been on screen — so an entrance animation plays at most
  // once per row, and only for live-appended rows (see animatedKeys below).
  const enteredKeysRef = useRef<Set<string>>(new Set());
  const notifiedSessionIdRef = useRef<string | null>(null);
  // Whether the turn is currently paused on a pending decision (a permission or
  // question prompt on a tool call). Read via a ref inside handleSend so a new
  // message is queued rather than steered while a decision is outstanding.
  const hasInputRequiredRef = useRef(false);
  const [agentsPanelOpen, setAgentsPanelOpen] = useState(false);
  const [focusedGroupId, setFocusedGroupId] = useState<string | null>(null);
  // Stable display order for the welcome screen's recent-project chips. Re-picking a
  // known project bumps its recency in the source list, which would reshuffle the
  // chips under the cursor — so the order here is held steady. But when a genuinely
  // new project appears (a folder just opened), the fresh order is adopted so it shows
  // up and, being newest, sorts to the front and reads as selected. Removals and name
  // changes are reflected in place without reordering.
  const [orderedRecentProjects, setOrderedRecentProjects] = useState<{ path: string; name: string }[]>(() => recentProjects ?? []);
  useEffect(() => {
    const live = recentProjects ?? [];
    setOrderedRecentProjects((current) => {
      const knownPaths = new Set(current.map((project) => project.path));
      const hasNewProject = live.some((project) => !knownPaths.has(project.path));
      if (hasNewProject || current.length === 0) return live;
      // No new project: keep the frozen order, but refresh names and drop any that were
      // removed. Bail out with the same reference when nothing changed, to avoid a
      // needless re-render.
      const liveByPath = new Map(live.map((project) => [project.path, project]));
      const next = current
        .filter((project) => liveByPath.has(project.path))
        .map((project) => liveByPath.get(project.path)!);
      const unchanged =
        next.length === current.length &&
        next.every((project, index) => project.path === current[index].path && project.name === current[index].name);
      return unchanged ? current : next;
    });
  }, [recentProjects]);
  const [agentsSidebarWidth, setAgentsSidebarWidth] = useState(420);
  const [previewPanelOpen, setPreviewPanelOpen] = useState(false);
  const [previewPanelWidth, setPreviewPanelWidth] = useState(560);
  // Open preview tabs. Each tab holds a preview artifact the user has opened (or was
  // auto-opened when the agent created it). The active tab's iframe is mounted; all
  // others are collapsed to save resources. New previews auto-open as tabs.
  const [previewTabs, setPreviewTabs] = useState<Array<{id: string; artifact: Record<string, unknown>; title: string; address: string}>>([]);
  const [activeTabId, setActiveTabId] = useState<string | null>(null);
  const [seenPreviewIds, setSeenPreviewIds] = useState<Set<string>>(() => new Set());
  const [previewReloadKey, setPreviewReloadKey] = useState(0);
  const [previewMaximized, setPreviewMaximized] = useState(false);
  // Whether the transcript is scrolled to (or near) the bottom. Drives the floating
  // "jump to latest" affordance so a reader who scrolled up to read history can
  // return to the live tail in one click instead of scrolling all the way down.
  const [isAtBottom, setIsAtBottom] = useState(true);
  // Top-bar surfaces: the settings dialog, the delete-session confirmation, and the
  // background-processes sheet all open from the persistent bar above the transcript.
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsSection, setSettingsSection] = useState<SettingsSection>("general");
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);
  const [backgroundPanelOpen, setBackgroundPanelOpen] = useState(false);
  const [backgroundSidebarWidth, setBackgroundSidebarWidth] = useState(420);

  // Pinned == the viewport is at (or within a hair of) the bottom. That single
  // fact drives everything: pinned means follow new content, unpinned means the
  // reader has scrolled up to read history and must never be pulled back down.
  const handleScroll = useCallback(() => {
    const container = scrollContainerRef.current;
    if (!container) return;
    const distanceFromBottom = container.scrollHeight - (container.scrollTop + container.clientHeight);
    const atBottom = distanceFromBottom <= 8;
    isPinnedRef.current = atBottom;
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

  const handleSend = useCallback((text: string, dataPart?: Record<string, unknown>) => {
    scrollToBottom();
    // Queue (never steer) while a decision prompt is outstanding — see hasInputRequiredRef.
    const result = send(text, dataPart, hasInputRequiredRef.current);
    scrollToBottom();
    return result;
  }, [scrollToBottom, send]);

  const openSettings = useCallback((section: SettingsSection) => {
    setSettingsSection(section);
    setSettingsOpen(true);
  }, []);

  // A rendered widget posted an interaction back to the agent. It travels as a
  // structured turn (a typed DataPart), so just forward it intact.
  const handleWidgetEvent = useCallback((widgetEvent: WidgetEvent) => {
    scrollToBottom();
    sendWidgetEvent(widgetEvent);
    scrollToBottom();
  }, [scrollToBottom, sendWidgetEvent]);

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

  // Late-growing content (images, widgets, streamed text) keeps the bottom in view
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

  // Reset the single-live-preview selection when the session changes, so a freshly
  // loaded transcript seeds its own "newest" preview rather than inheriting one
  // from the previous conversation. Render-phase prop-change adjustment (the same
  // pattern as previousActiveSteps below) rather than an effect, to stay lint-clean.
  const [previousInitialSession, setPreviousInitialSession] = useState({ id: initialSessionId, permissionMode: initialPermissionMode });
  if (initialSessionId !== previousInitialSession.id || initialPermissionMode !== previousInitialSession.permissionMode) {
    const sessionChanged = initialSessionId !== previousInitialSession.id;
    setPreviousInitialSession({ id: initialSessionId, permissionMode: initialPermissionMode });
    setPermissionModeState(initialPermissionMode);
    if (sessionChanged) {
      setSeenPreviewIds(new Set());
      setPreviewTabs([]);
      setActiveTabId(null);
      // Forget prior rows so the next session's first population seeds (no entrance
      // animation on load) instead of treating everything as newly appended.
      enteredKeysRef.current = new Set();
      scrollMetricsRef.current = { scrollHeight: 0, firstKey: "", count: 0 };
      isPinnedRef.current = true;
    }
  }

  // New content is followed by the layout effect above (only while pinned); this
  // just surfaces the streaming flag to the parent. The initial jump-to-bottom is
  // also handled there: pinned starts true, so the first post-load pass lands at
  // the bottom instantly.
  useEffect(() => {
    onStreamingChangeRef.current?.(isStreaming);
  }, [isStreaming]);

  async function handlePermissionModeChange(nextMode: PermissionMode) {
    const previousMode = permissionMode;
    setPermissionModeState(nextMode);
    onPermissionModeChange?.(nextMode);
    // Persist to server settings so it survives across sessions.
    saveSettings({ permission_mode: nextMode }).catch(() => {});
    if (!sessionId) return;
    try {
      await setPermissionMode(sessionId, nextMode);
    } catch {
      setPermissionModeState(previousMode);
      onPermissionModeChange?.(previousMode);
    }
  }

  const activeSteps = agentGroups.reduce(
    (sum, group) => sum + group.steps.filter((step) => !isStepDone(step)).length,
    0
   );
  const currentFolderName = folderDisplayName(workingDirectory, recentProjects);
  const renderedTimeline = useMemo(() => timelineItems(messages, isStreaming), [messages, isStreaming]);
  // Entrance animation is reserved for rows a *live turn* just appended at the
  // bottom — never the initial load or a background history prepend, which arrive
  // in bulk (and, for prepends, above the fold). The rule is purely positional and
  // so immune to state-flag timing: a row animates only if its key has never been
  // seen AND every row after it is also unseen (i.e. it is part of the trailing run
  // of brand-new rows at the end of the list). The first population seeds the set
  // with everything, so nothing animates on load. `enteredKeysRef` persists across
  // renders; the panel remounts per session (page.tsx keys it), so it resets then.
  const timelineKeys = renderedTimeline.map((item) => (item.kind === "tool_group" ? item.id : item.message.id));
  const animatedKeys = new Set<string>();
  if (enteredKeysRef.current.size > 0) {
    // Not the first population: animate only the trailing run of never-seen rows.
    for (let index = timelineKeys.length - 1; index >= 0; index -= 1) {
      if (enteredKeysRef.current.has(timelineKeys[index])) break;
      animatedKeys.add(timelineKeys[index]);
    }
  }
  // Record every rendered key as seen (after commit), so it never re-animates on a
  // later render or a background prepend. Idempotent, so StrictMode's double-invoke
  // is harmless.
  useEffect(() => {
    for (const item of renderedTimeline) {
      enteredKeysRef.current.add(item.kind === "tool_group" ? item.id : item.message.id);
    }
  }, [renderedTimeline]);
  const activeToolCount = messages.filter((message) =>
    message.role === "tool_call" && (message.meta?.status === "running" || message.meta?.status === "input_required")
  ).length;
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
  // "Reveal" opens the session's runtime directory (a worktree/branch checkout when
  // the session manages one, otherwise the plain working directory) in the OS file
  // manager. Falls back to the working directory before a session exists.
  const revealPath = (workspaceRuntimeDirectory || workingDirectory || "").trim();

  const previewEntries = useMemo(() => {
    const entries: { toolCallId: string; artifact: Record<string, unknown>; title: string; address: string }[] = [];
    for (const message of messages) {
      if (message.role !== "tool_call") continue;
      const toolCallId = String(message.meta?.toolCallId ?? "");
      if (!toolCallId) continue;
      const result = message.meta?.result;
      const resultContent = result == null ? null : typeof result === "string" ? result : JSON.stringify(result);
      if (resultContent == null) continue;
      const artifacts = extractToolArtifacts(message.content, resultContent);
      for (const artifact of artifacts) {
        if (!isLivePreviewArtifact(artifact)) continue;
        entries.push({
          toolCallId,
          artifact,
          title: String(artifact.title ?? "Preview"),
          address: previewArtifactAddress(artifact),
        });
      }
    }
    return entries;
  }, [messages]);
  const previewToolCallIds = useMemo(() => previewEntries.map((entry) => entry.toolCallId), [previewEntries]);
  const activeTabEntry = useMemo(() => {
    if (previewTabs.length === 0) return null;
    return previewTabs.find((tab) => tab.id === activeTabId) ?? previewTabs[previewTabs.length - 1];
  }, [previewTabs, activeTabId]);

  // Auto-open the newest preview as a tab the moment it appears. Runs in render
  // (same pattern as previousActiveSteps below) so the tab is added before
  // children render. State (not a ref) so the render-phase update stays lint-clean
  // and triggers a synchronous re-render before commit.
  const newPreviewCallIds = previewToolCallIds.filter((id) => !seenPreviewIds.has(id));
  if (newPreviewCallIds.length > 0) {
    const nextSeen = new Set(seenPreviewIds);
    const newTabs: Array<{id: string; artifact: Record<string, unknown>; title: string; address: string}> = [];
    for (const id of newPreviewCallIds) {
      nextSeen.add(id);
      const entry = previewEntries.find((e) => e.toolCallId === id);
      if (entry) {
        newTabs.push({ id: entry.toolCallId, artifact: entry.artifact, title: entry.title, address: entry.address });
      }
    }
    setSeenPreviewIds(nextSeen);
    setPreviewTabs((current) => [...current, ...newTabs]);
    setActiveTabId(newPreviewCallIds[newPreviewCallIds.length - 1]);
    setPreviewPanelOpen(true);
  }

  // Open a preview as a tab (called when the user clicks a collapsed preview in
  // the message area). If already open, just switch to it.
  const handleOpenTab = useCallback((toolCallId: string) => {
    if (previewTabs.some((tab) => tab.id === toolCallId)) {
      setActiveTabId(toolCallId);
      return;
    }
    const entry = previewEntries.find((entry) => entry.toolCallId === toolCallId);
    if (entry) {
      setPreviewTabs((current) => [...current, { id: entry.toolCallId, artifact: entry.artifact, title: entry.title, address: entry.address }]);
      setActiveTabId(toolCallId);
    }
  }, [previewTabs, previewEntries]);

  // Close a specific preview tab. If it was the active tab, switch to the nearest.
  const handleCloseTab = useCallback((toolCallId: string) => {
    setPreviewTabs((current) => {
      const index = current.findIndex((tab) => tab.id === toolCallId);
      if (index === -1) return current;
      const next = current.filter((tab) => tab.id !== toolCallId);
      if (activeTabId === toolCallId && next.length > 0) {
        // Switch to the tab at the same index, or the last one if index is out of range
        const nextIndex = Math.min(index, next.length - 1);
        setActiveTabId(next[nextIndex].id);
      } else if (next.length === 0) {
        setActiveTabId(null);
      }
      return next;
    });
  }, [activeTabId]);

  // For backward compatibility with threaded props (activePreviewId / onActivatePreview)
  // that flow through ChatMessageItem / ChatToolGroup but are not consumed.
  // These will be removed once the component tree is cleaned up.
  const activePreviewId = activeTabId;
  const handleActivatePreview = handleOpenTab;

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
  const hasInputRequired = messages.some(
    (message) => message.role === "tool_call" && message.meta?.status === "input_required"
  );
  hasInputRequiredRef.current = hasInputRequired;
  // The first pending input-required prompt (an ask_user question or a permission
  // approval), surfaced as an overlay above the input. Both live outside the tool
  // card so a pending decision always grabs attention at the bottom of the chat,
  // and resolving one reveals the next. A question takes precedence on the same
  // card, though in practice a card carries only one.
  const pendingPrompt = useMemo(() => {
    for (const message of messages) {
      if (message.role !== "tool_call" || message.meta?.status !== "input_required") continue;
      const question = message.meta?.question as ToolQuestion | undefined;
      if (question) return { kind: "question" as const, question };
      const permission = message.meta?.permission as ToolPermission | undefined;
      if (permission) {
        const name = message.content;
        const args = message.meta?.arguments as Record<string, unknown> | undefined;
        const command = name === "bash" && args?.command ? String(args.command) : "";
        return {
          kind: "permission" as const,
          permission,
          title: getToolCallDisplay(name, args).label,
          detail: command ? "```\n" + command + "\n```" : undefined,
        };
      }
    }
    return null;
  }, [messages]);
  // Auto-open the agents panel on desktop when agent activity begins. Tracked
  // during render (skipped on the first render, so window is only read
  // client-side after a change) rather than in an effect.
  const [previousActiveSteps, setPreviousActiveSteps] = useState(activeSteps);
  if (activeSteps !== previousActiveSteps) {
    setPreviousActiveSteps(activeSteps);
    if (activeSteps > 0 && window.matchMedia("(min-width: 768px)").matches) {
      setAgentsPanelOpen(true);
    }
  }

  const handleAgentsResizeStart = useCallback((event: PointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = agentsSidebarWidth;

    function handlePointerMove(moveEvent: globalThis.PointerEvent) {
      const nextWidth = Math.min(720, Math.max(300, startWidth + startX - moveEvent.clientX));
      setAgentsSidebarWidth(nextWidth);
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
  }, [agentsSidebarWidth]);

  const handleBackgroundResizeStart = useCallback((event: PointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = backgroundSidebarWidth;

    function handlePointerMove(moveEvent: globalThis.PointerEvent) {
      const nextWidth = Math.min(720, Math.max(300, startWidth + startX - moveEvent.clientX));
      setBackgroundSidebarWidth(nextWidth);
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
  }, [backgroundSidebarWidth]);

  const handlePreviewResizeStart = useCallback((event: PointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    setPreviewMaximized(false);
    const startX = event.clientX;
    const startWidth = previewPanelWidth;

    function handlePointerMove(moveEvent: globalThis.PointerEvent) {
      const nextWidth = Math.min(900, Math.max(340, startWidth + startX - moveEvent.clientX));
      setPreviewPanelWidth(nextWidth);
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
  }, [previewPanelWidth]);

  return (
    <WidgetEventProvider onEvent={handleWidgetEvent}>
    <Flex h="100%" minW={0} position="relative">
      <Flex direction="column" flex={1} minW={0} h="100%">
        {/* Persistent top bar: session identity on the left, session tools on the
            right. Always visible so the controls have a stable home; the title
            fills in once the session names itself, matching the sidebar default
            until then. */}
        <Flex align="center" gap={2} px={3} py={2} borderBottom="1px solid" borderColor="border" flexShrink={0} minW={0}>
          <Box color="fg.muted" flexShrink={0}><LuMessageSquare size={15} /></Box>
          <Text fontSize="sm" fontWeight="medium" truncate minW={0} flex={1}>
            {sessionId ? (sessionTitle || "Untitled conversation") : "New conversation"}
          </Text>
          <Flex align="center" gap={1.5} flexShrink={0}>
            <Tooltip content="Background processes" openDelay={300}>
              <IconButton
                aria-label="Background processes"
                size="xs"
                variant={backgroundPanelOpen ? "subtle" : "ghost"}
                colorPalette={backgroundPanelOpen ? "blue" : undefined}
                borderRadius="sm"
                position="relative"
                onClick={() => setBackgroundPanelOpen((current) => !current)}
              >
                <LuTerminal size={15} />
                {runningShellCount > 0 && (
                  <Box position="absolute" top="4px" right="5px" w="6px" h="6px" borderRadius="full" bg="green.solid" boxShadow="0 0 0 1px var(--chakra-colors-bg)" />
                )}
              </IconButton>
            </Tooltip>
            {onToggleHistory && (
              <Tooltip content="History" openDelay={300}>
                <IconButton
                  aria-label="History"
                  size="xs"
                  variant={historyOpen ? "subtle" : "ghost"}
                  colorPalette={historyOpen ? "blue" : undefined}
                  borderRadius="sm"
                  onClick={onToggleHistory}
                >
                  <LuHistory size={15} />
                </IconButton>
              </Tooltip>
            )}
            <Tooltip content={activeSteps > 0 ? `Agents (${activeSteps} active)` : "Agents"} openDelay={300}>
              <IconButton
                aria-label="Agents"
                size="xs"
                variant={agentsPanelOpen ? "subtle" : "ghost"}
                colorPalette={agentsPanelOpen ? "blue" : undefined}
                borderRadius="sm"
                position="relative"
                onClick={() => {
                  setFocusedGroupId(null);
                  setAgentsPanelOpen((current) => !current);
                }}
              >
                <LuNetwork size={15} />
                {activeSteps > 0 && (
                  <Box position="absolute" top="-2px" right="-2px" minW="13px" h="13px" px="3px" borderRadius="full" bg="bg.emphasized" color="fg.muted" border="1px solid" borderColor="border.emphasized" fontSize="8px" fontWeight="semibold" lineHeight="11px" textAlign="center">
                    {activeSteps}
                  </Box>
                )}
              </IconButton>
            </Tooltip>
            <Tooltip content={previewTabs.length > 0 ? `Previews (${previewTabs.length})` : "Previews"} openDelay={300}>
              <IconButton
                aria-label="Previews"
                size="xs"
                variant={previewPanelOpen ? "subtle" : "ghost"}
                colorPalette={previewPanelOpen ? "blue" : undefined}
                borderRadius="sm"
                position="relative"
                onClick={() => setPreviewPanelOpen((current) => !current)}
              >
                <LuAppWindow size={15} />
                {previewTabs.length > 0 && (
                  <Box position="absolute" top="-2px" right="-2px" minW="13px" h="13px" px="3px" borderRadius="full" bg="bg.emphasized" color="fg.muted" border="1px solid" borderColor="border.emphasized" fontSize="8px" fontWeight="semibold" lineHeight="11px" textAlign="center">
                    {previewTabs.length}
                  </Box>
                )}
              </IconButton>
            </Tooltip>
            <Tooltip content="Settings" openDelay={300}>
              <IconButton
                aria-label="Settings"
                size="xs"
                variant="ghost"
                borderRadius="sm"
                onClick={() => openSettings("general")}
              >
                <LuSettings size={15} />
              </IconButton>
            </Tooltip>
            <Menu.Root>
              <Menu.Trigger asChild>
                <IconButton aria-label="Session options" size="xs" variant="ghost" borderRadius="sm">
                  <LuEllipsis size={15} />
                </IconButton>
              </Menu.Trigger>
              <Portal>
                <Menu.Positioner>
                  <Menu.Content borderRadius="sm" minW="200px">
                    <Menu.Item
                      value="reveal"
                      disabled={!revealPath}
                      onClick={() => { if (revealPath) void revealInFinder(revealPath); }}
                    >
                      <LuFolderOpen size={14} />
                      <Box flex={1}>Open this folder</Box>
                    </Menu.Item>
                    <Menu.Item
                      value="delete"
                      color="red.fg"
                      _hover={{ bg: "red.subtle" }}
                      disabled={!sessionId || !onDeleteSession}
                      onClick={() => setDeleteConfirmOpen(true)}
                    >
                      <LuTrash2 size={14} />
                      <Box flex={1}>Delete session</Box>
                    </Menu.Item>
                  </Menu.Content>
                </Menu.Positioner>
              </Portal>
            </Menu.Root>
          </Flex>
        </Flex>
        <Box position="relative" flex={1} minH={0} display="flex" flexDirection="column">
        <Box ref={scrollContainerRef} flex={1} minH={0} display="flex" flexDirection="column" overflowY="auto" px={2} py={2} onScroll={handleScroll} style={{ overflowAnchor: "none", scrollbarGutter: "stable" }}>
          {isHistoryLoading ? (
            <Flex h="100%" />
          ) : historyError ? (
            <Flex direction="column" align="center" justify="center" minH="100%" gap={6} px={2}>
              <EmptyState.Root>
                <EmptyState.Content>
                  <EmptyState.Indicator>
                    <LuTriangleAlert />
                  </EmptyState.Indicator>
                  <VStack gap={1}>
                    <EmptyState.Title>Couldn’t load this conversation</EmptyState.Title>
                    <EmptyState.Description>
                      The transcript failed to load. Check the connection and try again.
                    </EmptyState.Description>
                  </VStack>
                  <Button size="xs" variant="solid" colorPalette="blue" borderRadius="sm" onClick={reloadHistory}>
                    Retry
                  </Button>
                </EmptyState.Content>
              </EmptyState.Root>
            </Flex>
          ) : messages.length === 0 ? (
            <Flex direction="column" align="center" gap={7} px={4} pt={{ base: 8, md: 14 }} pb={{ base: 8, md: 12 }}>
              {/* Brand lockup — the Daisy wordmark and flower, matching the home
                  screen's treatment, so the blank conversation is unmistakably Daisy,
                  with a short tagline underneath. */}
              <Flex direction="column" align="center" gap={2}>
                <Flex align="center" gap={2} pb={3}>
                  <Text fontSize="3xl" lineHeight="1">
                    {"🌼"}
                  </Text>
                  <Text fontSize="3xl" fontWeight="bold" fontFamily="var(--font-display)" lineHeight="1">
                    Daisy
                  </Text>
                </Flex>
                <Text fontSize="sm" color="fg.muted" textAlign="center">
                  The open-source partner for agentic engineering—built to stay yours
                </Text>
              </Flex>

              {/* Primary actions — one uniform button row (the connection switcher,
                  which also hosts connection settings, then the folder actions). */}
              <Flex direction="column" align="center" gap={2.5} w="100%" maxW="680px">
                <Flex gap={2.5} wrap="wrap" justify="center">
                  <ConnectionSwitcher
                    size="sm"
                    currentTargetId={currentConnectionId}
                    onConnectionChange={onConnectionChange}
                    onOpenConnectionSettings={() => openSettings("connection")}
                  />
                  {onBrowseFolder && (
                    <Button
                      size="sm"
                      variant="outline"
                      borderRadius="md"
                      bg="blue.subtle"
                      borderColor="blue.muted"
                      color="blue.fg"
                      _hover={{ bg: "blue.muted" }}
                      onClick={onBrowseFolder}
                    >
                      <LuFolderOpen size={14} />
                      Open a folder
                    </Button>
                  )}
                </Flex>
              </Flex>

              {/* Recent projects — a distinct, labelled group so quick-jump chips
                  don't blend into the action buttons. The order is held steady (see
                  orderedRecentProjects) so re-picking one doesn't make them jump, while
                  a newly opened folder still appears; the current project reads as
                  selected rather than disabled. */}
              {orderedRecentProjects.length > 0 && (
                <Flex direction="column" align="center" gap={2.5} w="100%" maxW="640px">
                  <Flex align="center" gap={1.5} color="fg.muted">
                    <LuHistory size={15} />
                    <Text fontSize="sm" fontWeight="bold">Recent projects</Text>
                  </Flex>
                  <Flex gap={2} wrap="wrap" justify="center">
                    {orderedRecentProjects.slice(0, 6).map((project) => {
                      const selected = project.path === workingDirectory;
                      return (
                        <Button
                          key={project.path}
                          size="sm"
                          variant={selected ? "solid" : "subtle"}
                          colorPalette={selected ? "blue" : undefined}
                          borderRadius="md"
                          onClick={() => onWorkingDirectoryChange?.(project.path)}
                          title={selected ? `${project.path} (current)` : project.path}
                        >
                          {selected ? <LuCheck size={13} /> : <LuFolder size={13} />}
                          {project.name}
                        </Button>
                      );
                    })}
                  </Flex>
                </Flex>
              )}

              {/* The build prompt sits below the connection/folder configuration, so
                  the top of the screen stays reserved for setup and this reads as the
                  lead-in to the composer rather than competing with the brand lockup. */}
              <Text as="h2" fontSize="2xl" fontWeight="semibold" textAlign="center">
                What should we build in {currentFolderName}?
              </Text>

              <AgentSkills card={agentCard ?? null} workingDirectory={workingDirectory} homeDirectory={homeDirectory} />
            </Flex>
          ) : (
            <VStack ref={scrollContentRef} gap={2} align="stretch">
              <AnimatePresence initial={false}>
                {renderedTimeline.map((item, itemIndex) => {
                  const isLastItem = itemIndex === renderedTimeline.length - 1;
                  const key = item.kind === "tool_group" ? item.id : item.message.id;
                  const inner = item.kind === "tool_group" ? (
                    <ChatToolGroup
                      messages={item.messages}
                      onPermission={handlePermission}
                      onQuestion={handleQuestion}
                      agents={agents}
                      activePreviewId={activePreviewId}
                      onActivatePreview={handleActivatePreview}
                      keepOpen={isStreaming && isLastItem}
                      thinkingCount={item.thinkingCount}
                    />
                  ) : (
                    <ChatMessageItem
                      message={item.message}
                      onPermission={handlePermission}
                      onQuestion={handleQuestion}
                      agents={agents}
                      activePreviewId={activePreviewId}
                      onActivatePreview={handleActivatePreview}
                      onRetry={item.message.role === "error" ? handleRetry : undefined}
                    />
                  );
                  // Assistant messages stream their content in, and tool groups
                  // update their heading in place as calls arrive. Any entrance or
                  // layout animation on either wrapper looks like the transcript is
                  // being pushed around, so both get a plain stable row.
                  const isAssistantMessage = item.kind === "message" && item.message.role === "assistant";
                  if (isAssistantMessage || item.kind === "tool_group") {
                    return (
                      <div key={key} style={{ display: "flex", flexDirection: "column" }}>
                        {inner}
                      </div>
                    );
                  }
                  return (
                    <motion.div
                      key={key}
                      initial={animatedKeys.has(key) ? { opacity: 0, y: 6 } : false}
                      animate={{ opacity: 1, y: 0 }}
                      exit={{ opacity: 0 }}
                      transition={{ duration: 0.18, ease: "easeOut" }}
                      style={{ display: "flex", flexDirection: "column" }}
                    >
                      {inner}
                    </motion.div>
                  );
                })}
              </AnimatePresence>
              {queuedMessages.map((message, index) => (
                <Flex key={message.id} align="flex-start" alignSelf="flex-end" maxW="80%" gap={1.5}>
                  <IconButton
                    aria-label="Delete queued message"
                    size="xs"
                    variant="ghost"
                    colorPalette="red"
                    borderRadius="sm"
                    mt="2px"
                    flexShrink={0}
                    onClick={() => dequeueMessage(index)}
                  >
                    <LuTrash2 size={11} />
                  </IconButton>
                  <Box
                    px={2}
                    py={1.5}
                    borderRadius="sm"
                    border="1px dashed"
                    borderColor="border"
                    bg="bg.subtle"
                    opacity={0.7}
                    flex={1}
                    minW={0}
                  >
                    <Flex align="center" gap={1.5}>
                      <Box as="span" display="inline-flex" alignItems="center">
                        {message.steering ? <LuNavigation size={11} /> : <LuClock size={11} />}
                      </Box>
                      <Text fontSize="xs" color="fg.subtle" fontWeight="medium">
                        {message.steering ? "Steering next opening" : "Queued"}
                      </Text>
                    </Flex>
                    <Text fontSize="sm" color="fg.muted">{message.text}</Text>
                  </Box>
                </Flex>
              ))}
            </VStack>
          )}
        </Box>
        {!isAtBottom && !isHistoryLoading && !historyError && messages.length > 0 && (
          <Button
            size="sm"
            variant="outline"
            borderRadius="sm"
            position="absolute"
            bottom={3}
            left="50%"
            transform="translateX(-50%)"
            zIndex={2}
            bg="bg.muted"
            border="1px solid"
            borderColor="border"
            color="fg"
            fontWeight="medium"
            boxShadow="sm"
            px={2}
            _hover={{ bg: "bg.emphasized" }}
            onClick={scrollToBottom}
          >
            <LuArrowDown />
            Jump to latest
          </Button>
        )}
        </Box>

        {pendingPrompt?.kind === "question" && (
          <QuestionOverlay question={pendingPrompt.question} onQuestion={handleQuestion} onDismiss={declineQuestion} />
        )}
        {pendingPrompt?.kind === "permission" && (
          <PermissionOverlay
            permission={pendingPrompt.permission}
            title={pendingPrompt.title}
            detail={pendingPrompt.detail}
            onPermission={handlePermission}
          />
        )}
        <ChatInput
          onSend={handleSend}
          onAbort={abort}
          isStreaming={isStreaming}
          disabled={!isConnected || !!pendingPrompt}
          sessionId={sessionId}
          currentConnectionId={currentConnectionId}
          onConnectionChange={onConnectionChange}
          onOpenConnectionSettings={() => openSettings("connection")}
          workingDirectory={workingDirectory}
          recentProjects={recentProjects}
          onWorkingDirectoryChange={onWorkingDirectoryChange}
          onBrowseFolder={onBrowseFolder}
          sandboxEnabled={sandboxEnabled}
          onSandboxEnabledChange={onSandboxEnabledChange}
          workspaceStrategy={workspaceStrategy}
          workspaceBranch={workspaceBranch}
          workspaceRuntimeDirectory={workspaceRuntimeDirectory}
          workspaceRuntimeDirectoryName={workspaceRuntimeDirectoryName}
          workspaceError={workspaceError}
          onWorkspaceStrategyChange={onWorkspaceStrategyChange}
          agents={agents}
          selectedAgent={agent}
          onAgentChange={onAgentChange}
          permissionMode={permissionMode}
          onPermissionModeChange={handlePermissionModeChange}
          models={models}
          modelProviders={modelProviders}
          recentModels={recentModels}
          selectedModel={selectedModel}
          globalModel={globalModel}
          agentModel={agentModel}
          onModelChange={(model) => onModelChange?.(model)}
          tokenUsage={tokenUsage}
          onCompact={compact}
          isCompacting={isCompacting}
          compactionKeepRecentTurns={compactionKeepRecentTurns}
          compactionUserCount={messages.filter((message) => message.role === "user").length}
        />
      </Flex>

      <AnimatePresence initial={false}>
        {previewPanelOpen && (
          <MotionFlex
            key="preview-panel"
            direction="column"
            w={{ base: "100%", md: previewMaximized ? "min(900px, 60vw)" : `${previewPanelWidth}px` }}
            position="relative"
            maxW={{ base: "100%", md: previewMaximized ? "90vw" : "48vw" }}
            minW={{ base: "100%", md: "340px" }}
            h="100%"
            borderLeft="1px solid"
            borderColor="border"
            bg="bg"
            flexShrink={0}
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
              left="-4px"
              w="8px"
              cursor="col-resize"
              zIndex={1}
              onPointerDown={handlePreviewResizeStart}
            />
            {/* Persistent top bar — matching agents panel style */}
            <Flex align="center" gap={2} px={3} py={2} borderBottom="1px solid" borderColor="border" flexShrink={0}>
              <Box color="fg.muted"><LuAppWindow size={15} /></Box>
              <Text fontSize="sm" fontWeight="bold" flex={1}>Previews</Text>
              <IconButton aria-label="Collapse preview sidebar" size="xs" variant="ghost" borderRadius="sm" onClick={() => setPreviewPanelOpen(false)}>
                <LuX size={15} />
              </IconButton>
            </Flex>
            {previewTabs.length === 0 ? (
              <Flex direction="column" align="center" justify="center" minH="100%" gap={6} px={2} pt={4} pb={12}>
                <EmptyState.Root>
                  <EmptyState.Content>
                    <EmptyState.Indicator>
                      <LuAppWindow />
                    </EmptyState.Indicator>
                    <VStack gap={1}>
                      <EmptyState.Title>No previews yet</EmptyState.Title>
                      <EmptyState.Description>
                        Previews will appear here
                      </EmptyState.Description>
                    </VStack>
                  </EmptyState.Content>
                </EmptyState.Root>
              </Flex>
            ) : activeTabEntry ? (
              <>
                {/* Preview tab buttons — same styling as chat-input toolbar buttons */}
                <Flex px={2} pt={2} overflowX="auto" flexShrink={0}>
                  <Flex gap={1.5}>
                    {previewTabs.map((tab) => {
                      const isActive = tab.id === activeTabId;
                      return (
                        <Flex
                          key={tab.id}
                          as="button"
                          align="center"
                          gap={1.5}
                          pl={2}
                          pr={1}
                          h="28px"
                          fontSize="xs"
                          fontWeight="medium"
                          borderRadius="sm"
                          bg={isActive ? "bg.subtle" : "bg"}
                          border="1px solid"
                          borderColor={isActive ? "border.emphasized" : "border"}
                          color="fg"
                          cursor="pointer"
                          flexShrink={0}
                          whiteSpace="nowrap"
                          onClick={() => setActiveTabId(tab.id)}
                          _hover={{ bg: isActive ? "bg.muted" : "bg.subtle" }}
                          title={tab.address || tab.title}
                        >
                          <LuAppWindow size={13} />
                          <Text truncate maxW="110px">{tab.title}</Text>
                          <Box
                            as="span"
                            display="inline-flex"
                            alignItems="center"
                            justifyContent="center"
                            borderRadius="sm"
                            w="16px"
                            h="16px"
                            flexShrink={0}
                            color="fg.subtle"
                            _hover={{ bg: "bg.muted", color: "fg" }}
                            onClick={(event: React.MouseEvent) => { event.stopPropagation(); handleCloseTab(tab.id); }}
                            aria-label={`Close ${tab.title}`}
                          >
                            <LuX size={12} />
                          </Box>
                        </Flex>
                      );
                    })}
                  </Flex>
                </Flex>
                {/* Active tab header with controls — title, reload, maximize, download, close */}
                <Flex px={2} py={1.5} align="center" gap={1} borderBottom="1px solid" borderColor="border" flexShrink={0}>
                  <Text fontSize="sm" fontWeight="medium" truncate flex={1} minW={0}>
                    {activeTabEntry.title}
                  </Text>
                  <IconButton
                    aria-label="Reload preview"
                    title="Reload"
                    size="xs"
                    variant="ghost"
                    borderRadius="sm"
                    h="22px"
                    minW="22px"
                    onClick={() => setPreviewReloadKey((current) => current + 1)}
                  >
                    <LuRotateCw size={11} />
                  </IconButton>
                  <IconButton
                    aria-label={previewMaximized ? "Minimize preview" : "Maximize preview"}
                    title={previewMaximized ? "Minimize" : "Maximize"}
                    size="xs"
                    variant="ghost"
                    borderRadius="sm"
                    h="22px"
                    minW="22px"
                    onClick={() => setPreviewMaximized((current) => !current)}
                  >
                    {previewMaximized ? <LuMinimize2 size={11} /> : <LuMaximize2 size={11} />}
                  </IconButton>
                  <IconButton
                    aria-label="Download artifact"
                    title="Download"
                    size="xs"
                    variant="ghost"
                    borderRadius="sm"
                    h="22px"
                    minW="22px"
                    onClick={() => {
                      const address = activeTabEntry.address;
                      if (address) {
                        const link = document.createElement("a");
                        link.href = address.startsWith("http") ? address : window.location.origin + "/" + address;
                        link.download = activeTabEntry.title || "preview";
                        link.target = "_blank";
                        link.rel = "noopener noreferrer";
                        link.click();
                      }
                    }}
                  >
                    <LuDownload size={11} />
                  </IconButton>
                  <IconButton
                    aria-label="Close all previews"
                    title="Close"
                    size="xs"
                    variant="ghost"
                    borderRadius="sm"
                    h="22px"
                    minW="22px"
                    colorPalette="red"
                    onClick={() => setPreviewTabs([])}
                  >
                    <LuX size={12} />
                  </IconButton>
                </Flex>
                {(() => {
                  // Desktop: an external website renders in the embedded native webview
                  // (real browser engine, top-level navigation — full fidelity). Local
                  // files, inline HTML, and the web build keep the proxied iframe.
                  const activeTab = activeTabEntry;
                  const nativeUrl = nativePreviewAvailable() ? externalPreviewUrl(activeTab.artifact) : "";
                  if (nativeUrl) {
                    return (
                      <Box key={previewReloadKey} flex={1} minH={0}>
                        <NativeWebview url={nativeUrl} />
                      </Box>
                    );
                  }
                  return (
                    <Box key={previewReloadKey} flex={1} minH={0} display="flex" flexDirection="column">
                      <PreviewArtifact artifact={activeTab.artifact} showHeader={false} fillContainer />
                    </Box>
                  );
                })()}
              </>
            ) : null}
          </MotionFlex>
        )}
        {agentsPanelOpen && (
          <AgentsPanel
            agentGroups={agentGroups}
            agents={agents}
            open={agentsPanelOpen}
            onClose={() => setAgentsPanelOpen(false)}
            focusedGroupId={focusedGroupId}
            width={agentsSidebarWidth}
            onResizeStart={handleAgentsResizeStart}
          />
        )}
        {backgroundPanelOpen && (
          <BackgroundTasksPanel
            open={backgroundPanelOpen}
            onClose={() => setBackgroundPanelOpen(false)}
            messages={messages}
            sessionId={sessionId}
            width={backgroundSidebarWidth}
            onResizeStart={handleBackgroundResizeStart}
          />
        )}
      </AnimatePresence>

      <SettingsDialog
        open={settingsOpen}
        onOpenChange={setSettingsOpen}
        section={settingsSection}
        onSectionChange={setSettingsSection}
        currentConnectionId={currentConnectionId}
        onConnectionChange={onConnectionChange}
      />

      <Dialog.Root open={deleteConfirmOpen} onOpenChange={(event) => setDeleteConfirmOpen(event.open)} placement="center" role="alertdialog">
        <Portal>
          <Dialog.Backdrop />
          <Dialog.Positioner>
            <Dialog.Content borderRadius="md">
              <Dialog.Header>
                <Dialog.Title>Delete this session?</Dialog.Title>
              </Dialog.Header>
              <Dialog.Body>
                <Text fontSize="sm" color="fg.muted">
                  This permanently removes the conversation and its tasks. This can’t be undone.
                </Text>
              </Dialog.Body>
              <Dialog.Footer gap={2}>
                <Button size="sm" variant="outline" borderRadius="sm" onClick={() => setDeleteConfirmOpen(false)}>
                  Cancel
                </Button>
                <Button
                  size="sm"
                  colorPalette="red"
                  variant="solid"
                  borderRadius="sm"
                  onClick={() => {
                    setDeleteConfirmOpen(false);
                    if (sessionId) onDeleteSession?.(sessionId);
                  }}
                >
                  <LuTrash2 size={14} />
                  Delete
                </Button>
              </Dialog.Footer>
            </Dialog.Content>
          </Dialog.Positioner>
        </Portal>
      </Dialog.Root>
    </Flex>
    </WidgetEventProvider>
  );
}
