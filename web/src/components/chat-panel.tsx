"use client";

import {
  Box,
  Button,
  EmptyState,
  Flex,
  IconButton,
  Text,
  VStack,
} from "@chakra-ui/react";
import { LuAppWindow, LuArrowDown, LuClock, LuDownload, LuFolder, LuFolderOpen, LuHouse, LuMaximize2, LuMinimize2, LuNavigation, LuPlus, LuRotateCw, LuTrash2, LuTriangleAlert, LuX } from "react-icons/lu";
import { AnimatePresence, motion } from "motion/react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type PointerEvent } from "react";
import { useRouter } from "next/navigation";
import { useChat, isStepDone, type ChatMessage } from "@/lib/use-chat";
import { ChatMessageItem, ChatToolGroup } from "./chat-message";
import { extractToolArtifacts, externalPreviewUrl, isLivePreviewArtifact, PreviewArtifact } from "./tool-views";
import { NativeWebview } from "./native-webview";
import { nativePreviewAvailable } from "@/lib/native-preview";
import { WidgetEventProvider, type WidgetEvent } from "./widget-bridge";
import { ChatInput } from "./chat-input";
import { QuestionOverlay } from "./question-overlay";
import { AgentsPanel } from "./agents-panel";
import { AgentSkills } from "./agent-skills";

import { setPermissionMode, fetchSettings, saveSettings, type AgentCard, type AgentSummary, type PermissionMode, type WorkspaceStrategy } from "@/lib/api";

const MotionFlex = motion.create(Flex);

interface ChatPanelProps {
  agent: string;
  agents: AgentSummary[];
  agentCard?: AgentCard | null;
  onAgentChange: (agent: string) => void;
  initialSessionId: string | null;
  initialPermissionMode?: PermissionMode;
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
  onModelChange?: (model: string) => void;
  compactionKeepRecentTurns: number;
}

type TimelineItem =
  | { kind: "message"; message: ChatMessage }
  | { kind: "tool_group"; id: string; messages: ChatMessage[] };

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

function timelineItems(messages: ChatMessage[]): TimelineItem[] {
  const items: TimelineItem[] = [];
  let index = 0;
  while (index < messages.length) {
    const message = messages[index];
    if (message.role === "thinking") {
      index += 1;
      continue;
    }
    if (message.role !== "tool_call") {
      items.push({ kind: "message", message });
      index += 1;
      continue;
    }

    const toolMessages: ChatMessage[] = [];
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
        index += 1;
      } else {
        break;
      }
    }
    // Always wrap tool calls in a ToolGroup so the transition from 1→2 tools
    // is a smooth addition of a new child, not a full component swap.
    items.push({
      kind: "tool_group",
      // Key by the FIRST tool only: stays stable as more tools stream in.
      id: toolMessages[0].id,
      messages: toolMessages,
    });
  }
  return items;
}

export function ChatPanel({
  agent,
  agents,
  agentCard,
  onAgentChange,
  initialSessionId,
  initialPermissionMode = "default",
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
  onModelChange,
  compactionKeepRecentTurns,
}: ChatPanelProps) {
  const router = useRouter();
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
    send(text, dataPart, hasInputRequiredRef.current);
    scrollToBottom();
  }, [scrollToBottom, send]);

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

  // The live "Thinking" label shown beside the toolbar while the agent is
  // reasoning (no assistant text yet and no tool calls active). It stays
  // present through quick reasoning/tool/reasoning transitions, so the input
  // bar reads as one continuous active turn instead of blinking.
  const lastMessage = messages[messages.length - 1];
  const isAssistantStreaming = !!lastMessage && lastMessage.role === "assistant";
  const liveStatusLabel = !isStreaming || isAssistantStreaming ? null : "Thinking";
  // A tool call awaiting the user's approval or answer pauses the turn. While it is
  // outstanding, the composer may only queue (see handleSend) and Stop auto-denies it.
  const hasInputRequired = messages.some(
    (message) => message.role === "tool_call" && message.meta?.status === "input_required"
  );
  hasInputRequiredRef.current = hasInputRequired;
  // The first pending ask_user question, surfaced as an overlay above the input.
  const pendingQuestion = useMemo(() => {
    for (const message of messages) {
      if (message.role === "tool_call" && message.meta?.status === "input_required" && message.meta?.question) {
        return message.meta.question as import("@/lib/tool-event").ToolQuestion;
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
            <Flex direction="column" align="center" gap={6} px={4} pt={{ base: 8, md: 16 }} pb={{ base: 8, md: 12 }}>
              <Text as="h2" fontSize="2xl" fontWeight="semibold" textAlign="center">
                What should we build in {currentFolderName}?
              </Text>
              {/* Center-of-screen shortcuts for the actions a user reaches for from a
                  blank conversation, so they don't have to hunt the sidebar or the
                  composer toolbar: open a folder, switch connection, or jump straight
                  to a recent project. */}
              <Flex gap={2} wrap="wrap" justify="center" pb={2}>
                <Button size="sm" variant="outline" borderRadius="md" onClick={() => router.push("/home")}>
                  <LuHouse size={14} />
                  Home
                </Button>
                {onBrowseFolder && (
                  <Button size="sm" variant="outline" borderRadius="md" onClick={onBrowseFolder}>
                    <LuFolderOpen size={14} />
                    Open a folder
                  </Button>
                )}
                {(recentProjects ?? []).slice(0, 4).map((project) => (
                  <Button
                    key={project.path}
                    size="sm"
                    variant="subtle"
                    borderRadius="md"
                    disabled={project.path === workingDirectory}
                    onClick={() => onWorkingDirectoryChange?.(project.path)}
                    title={project.path}
                  >
                    <LuFolder size={13} />
                    {project.name}
                  </Button>
                ))}
              </Flex>
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
                  // Assistant messages stream their content in — any entrance or
                  // layout animation on the wrapper looks wrong as text grows, so
                  // they get a plain div with no motion at all.
                  const isAssistantMessage = item.kind === "message" && item.message.role === "assistant";
                  if (isAssistantMessage) {
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
            variant="subtle"
            colorPalette="gray"
            borderRadius="sm"
            position="absolute"
            bottom={3}
            left="50%"
            transform="translateX(-50%)"
            zIndex={2}
            boxShadow="md"
            px={2}
            onClick={scrollToBottom}
          >
            <LuArrowDown />
            Jump to latest
          </Button>
        )}
        </Box>

        {pendingQuestion && (
          <QuestionOverlay question={pendingQuestion} onQuestion={handleQuestion} onDismiss={declineQuestion} />
        )}
        <ChatInput
          onSend={handleSend}
          onAbort={abort}
          isStreaming={isStreaming}
          disabled={!isConnected || !!pendingQuestion}
          sessionId={sessionId}
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
          agentsCount={activeSteps}
          agentsOpen={agentsPanelOpen}
          onShowAgents={() => {
            setFocusedGroupId(null);
            setAgentsPanelOpen((current) => !current);
          }}
          previewsCount={previewTabs.length}
          previewOpen={previewPanelOpen}
          onTogglePreview={() => setPreviewPanelOpen((current) => !current)}
          historyOpen={historyOpen}
          onToggleHistory={onToggleHistory}
          models={models}
          modelProviders={modelProviders}
          recentModels={recentModels}
          selectedModel={selectedModel}
          globalModel={globalModel}
          onModelChange={(model) => onModelChange?.(model)}
          thinkingLabel={liveStatusLabel}
          tokenUsage={tokenUsage}
          onCompact={compact}
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
      </AnimatePresence>
    </Flex>
    </WidgetEventProvider>
  );
}
