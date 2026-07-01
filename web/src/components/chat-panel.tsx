"use client";

import {
  Box,
  Button,
  EmptyState,
  Flex,
  Text,
  VStack,
} from "@chakra-ui/react";
import { LuClock, LuMessageSquare, LuNavigation, LuTriangleAlert } from "react-icons/lu";
import { AnimatePresence, motion } from "motion/react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type PointerEvent } from "react";
import { useChat, isStepDone, type ChatMessage } from "@/lib/use-chat";
import { ChatMessageItem, ChatToolGroup } from "./chat-message";
import { extractToolArtifacts, isLivePreviewArtifact } from "./tool-views";
import { WidgetEventProvider, type WidgetEvent } from "./widget-bridge";
import { ChatInput } from "./chat-input";
import { AgentsPanel } from "./agents-panel";
import { AgentSkills } from "./agent-skills";
import { setPermissionMode, type AgentCard, type AgentSummary, type PermissionMode } from "@/lib/api";

interface ChatPanelProps {
  agent: string;
  agents: AgentSummary[];
  agentCard?: AgentCard | null;
  onAgentChange: (agent: string) => void;
  initialSessionId: string | null;
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
  isConnected?: boolean;
  onStreamingChange?: (isStreaming: boolean) => void;
  historyOpen?: boolean;
  onToggleHistory?: () => void;
  models?: { id: string; name: string; provider: string; available: boolean }[];
  modelProviders?: { id: string; name: string; openai_compatible: boolean }[];
  recentModels?: { id: string; name: string; provider: string }[];
  selectedModel?: string;
  onModelChange?: (model: string) => void;
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
    if (toolMessages.length === 1) {
      items.push({ kind: "message", message: toolMessages[0] });
    } else {
      items.push({
        kind: "tool_group",
        // Key by the FIRST tool only: the key must stay stable as more tools
        // stream into the group, or React remounts the whole group (re-running
        // every card's entrance animation and resetting its open/closed state).
        id: toolMessages[0].id,
        messages: toolMessages,
      });
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
  sessionRunning = false,
  onSessionCreated,
  workingDirectory,
  homeDirectory,
  recentProjects,
  onWorkingDirectoryChange,
  onBrowseFolder,
  sandboxEnabled = true,
  onSandboxEnabledChange,
  isConnected = false,
  onStreamingChange,
  historyOpen = false,
  onToggleHistory,
  models = [],
  modelProviders = [],
  recentModels = [],
  selectedModel = "",
  onModelChange,
}: ChatPanelProps) {
  const [permissionMode, setPermissionModeState] = useState<PermissionMode>("default");
  const { messages, agentGroups, tasks, queuedMessages, sessionId, isStreaming, isHistoryLoading, historyError, reloadHistory, send, sendWidgetEvent, abort, dequeueMessage, handlePermission, handleQuestion } =
    useChat(agent, initialSessionId, workingDirectory, permissionMode, sessionRunning);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const scrollContentRef = useRef<HTMLDivElement>(null);
  const scrollFrameRef = useRef<number | null>(null);
  // "Following" the bottom. Released the moment the user scrolls up and resumed
  // only when they return to the bottom — so auto-scroll never grabs them.
  const isPinnedRef = useRef(true);
  const lastScrollTopRef = useRef(0);
  const onStreamingChangeRef = useRef(onStreamingChange);
  const notifiedSessionIdRef = useRef<string | null>(null);
  const [agentsPanelOpen, setAgentsPanelOpen] = useState(false);
  const [focusedGroupId, setFocusedGroupId] = useState<string | null>(null);
  const [agentsSidebarWidth, setAgentsSidebarWidth] = useState(420);
  // Exactly one web preview (iframe-type artifact) is live at a time; the rest are
  // collapsed to click-to-open placeholders so their scripts/network don't pile up
  // and drag the page. Newest preview auto-activates; clicking an older one reopens
  // it (collapsing the current). See ToolArtifacts.
  const [activePreviewId, setActivePreviewId] = useState<string | null>(null);
  const [seenPreviewIds, setSeenPreviewIds] = useState<Set<string>>(() => new Set());

  const handleScroll = useCallback(() => {
    const container = scrollContainerRef.current;
    if (!container) return;
    const previousScrollTop = lastScrollTopRef.current;
    lastScrollTopRef.current = container.scrollTop;
    const distanceFromBottom = container.scrollHeight - (container.scrollTop + container.clientHeight);
    if (container.scrollTop < previousScrollTop - 1) {
      // Any upward scroll releases follow mode — never pull the user back down.
      isPinnedRef.current = false;
    } else if (distanceFromBottom <= 8) {
      // Returned to the bottom — resume following new content.
      isPinnedRef.current = true;
    }
  }, []);

  useEffect(() => {
    onStreamingChangeRef.current = onStreamingChange;
  }, [onStreamingChange]);

  const scheduleScrollToBottom = useCallback(() => {
    if (!isPinnedRef.current) return;
    if (scrollFrameRef.current != null) return;
    scrollFrameRef.current = window.requestAnimationFrame(() => {
      scrollFrameRef.current = null;
      const container = scrollContainerRef.current;
      if (!container || !isPinnedRef.current) return;
      container.scrollTop = container.scrollHeight;
      lastScrollTopRef.current = container.scrollTop;
    });
  }, []);

  const forceScrollToBottom = useCallback(() => {
    isPinnedRef.current = true;
    if (scrollFrameRef.current != null) {
      window.cancelAnimationFrame(scrollFrameRef.current);
      scrollFrameRef.current = null;
    }
    scrollFrameRef.current = window.requestAnimationFrame(() => {
      scrollFrameRef.current = null;
      const container = scrollContainerRef.current;
      if (!container) return;
      container.scrollTop = container.scrollHeight;
      lastScrollTopRef.current = container.scrollTop;
    });
  }, []);

  const handleSend = useCallback((text: string) => {
    forceScrollToBottom();
    send(text);
    forceScrollToBottom();
  }, [forceScrollToBottom, send]);

  // A rendered widget posted an interaction back to the agent. It travels as a
  // structured turn (a typed DataPart), so just forward it intact.
  const handleWidgetEvent = useCallback((widgetEvent: WidgetEvent) => {
    forceScrollToBottom();
    sendWidgetEvent(widgetEvent);
    forceScrollToBottom();
  }, [forceScrollToBottom, sendWidgetEvent]);

  useEffect(() => {
    if (!sessionId || notifiedSessionIdRef.current === sessionId) return;
    notifiedSessionIdRef.current = sessionId;
    onSessionCreated(sessionId);
  }, [sessionId, onSessionCreated]);

  useLayoutEffect(() => {
    scheduleScrollToBottom();
  }, [messages, queuedMessages, scheduleScrollToBottom]);

  useEffect(() => {
    const content = scrollContentRef.current;
    if (!content) return;
    const observer = new ResizeObserver(() => scheduleScrollToBottom());
    observer.observe(content);
    return () => observer.disconnect();
  }, [scheduleScrollToBottom]);

  // Reset the single-live-preview selection when the session changes, so a freshly
  // loaded transcript seeds its own "newest" preview rather than inheriting one
  // from the previous conversation. Render-phase prop-change adjustment (the same
  // pattern as previousActiveSteps below) rather than an effect, to stay lint-clean.
  const [previousInitialSessionId, setPreviousInitialSessionId] = useState(initialSessionId);
  if (initialSessionId !== previousInitialSessionId) {
    setPreviousInitialSessionId(initialSessionId);
    setSeenPreviewIds(new Set());
    setActivePreviewId(null);
  }

  useEffect(() => {
    return () => {
      if (scrollFrameRef.current != null) {
        window.cancelAnimationFrame(scrollFrameRef.current);
      }
    };
  }, []);

  useEffect(() => {
    // Follow new content only if the user is already at the bottom — starting a
    // turn must not yank someone who has scrolled up to read earlier messages.
    if (isStreaming) {
      scheduleScrollToBottom();
    }
    onStreamingChangeRef.current?.(isStreaming);
  }, [isStreaming, scheduleScrollToBottom]);

  // Scroll to bottom when history finishes loading. The layout effect on
  // [messages] can fire before the DOM has settled after async history load,
  // so this useEffect runs after paint, ensuring layout is final.
  useEffect(() => {
    if (!isHistoryLoading && messages.length > 0) {
      forceScrollToBottom();
    }
  }, [isHistoryLoading, messages.length, forceScrollToBottom]);

  async function handlePermissionModeChange(nextMode: PermissionMode) {
    const previousMode = permissionMode;
    setPermissionModeState(nextMode);
    if (!sessionId) return;
    try {
      await setPermissionMode(sessionId, nextMode);
    } catch {
      setPermissionModeState(previousMode);
    }
  }

  const activeSteps = agentGroups.reduce(
    (sum, group) => sum + group.steps.filter((step) => !isStepDone(step)).length,
    0
   );
  const currentFolderName = folderDisplayName(workingDirectory, recentProjects);
  const renderedTimeline = useMemo(() => timelineItems(messages), [messages]);
  const activeToolCount = messages.filter((message) =>
    message.role === "tool_call" && (message.meta?.status === "running" || message.meta?.status === "input_required")
  ).length;

  // The toolCallIds of every call in the transcript whose result includes a live
  // (iframe/html) artifact, in transcript order. Used to auto-activate the newest
  // preview — the agent's latest output becomes the live one — without re-activating
  // on in-place artifact updates (same call, same toolCallId).
  const previewToolCallIds = useMemo(() => {
    const ids: string[] = [];
    for (const message of messages) {
      if (message.role !== "tool_call") continue;
      const toolCallId = String(message.meta?.toolCallId ?? "");
      if (!toolCallId) continue;
      const result = message.meta?.result;
      const resultContent = result == null ? null : typeof result === "string" ? result : JSON.stringify(result);
      if (resultContent == null) continue;
      const artifacts = extractToolArtifacts(message.content, resultContent);
      if (artifacts.some((artifact) => isLivePreviewArtifact(artifact))) ids.push(toolCallId);
    }
    return ids;
  }, [messages]);

  // Auto-activate the newest preview the moment it appears. Runs in render (same
  // pattern as previousActiveSteps below) so the active toolCallId is set before
  // children render — users never see two live previews at once. `activePreviewId`
  // holds that toolCallId; see ToolArtifacts. State (not a ref) so the render-phase
  // update stays lint-clean and triggers a synchronous re-render before commit.
  const newPreviewCallIds = previewToolCallIds.filter((id) => !seenPreviewIds.has(id));
  if (newPreviewCallIds.length > 0) {
    const nextSeen = new Set(seenPreviewIds);
    for (const id of newPreviewCallIds) nextSeen.add(id);
    setSeenPreviewIds(nextSeen);
    setActivePreviewId(newPreviewCallIds[newPreviewCallIds.length - 1]);
  }

  const handleActivatePreview = useCallback((toolCallId: string) => {
    setActivePreviewId(toolCallId);
  }, []);

  // The live "Thinking" / "Working through N tool calls" label, shown as a chip
  // next to Settings when no tool group is active. "Thinking" only applies while
  // the model is actually reasoning (or waiting) — once it starts streaming its
  // answer (the last message is an assistant message) the chip hides, so it no
  // longer reads as "thinking" while the response is being written.
  const lastMessage = messages[messages.length - 1];
  const isAssistantStreaming = !!lastMessage && lastMessage.role === "assistant";
  const liveStatusLabel = !isStreaming
    ? null
    : activeToolCount > 0
      ? `Working through ${activeToolCount} ${activeToolCount === 1 ? "tool call" : "tool calls"}`
      : isAssistantStreaming
        ? null
        : "Thinking";
  const hasActiveToolGroup = renderedTimeline.some(
    (item) => item.kind === "tool_group" && item.messages.some((message) => {
      const status = message.meta?.status;
      return status === "running" || status === "input_required";
    })
  );

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

  return (
    <WidgetEventProvider onEvent={handleWidgetEvent}>
    <Flex h="100%" minW={0} position="relative">
      <Flex direction="column" flex={1} minW={0} h="100%">
        <Box ref={scrollContainerRef} flex={1} minH={0} overflowY="auto" px={2} py={2} onScroll={handleScroll} style={{ overflowAnchor: "none" }}>
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
            <Flex direction="column" align="center" justify="center" minH="100%" gap={6} px={2} pt={4} pb={12}>
              <Text as="h2" fontSize="xl" fontWeight="semibold" textAlign="center">
                What should we build in {currentFolderName}?
              </Text>
              <EmptyState.Root>
                <EmptyState.Content>
                  <EmptyState.Indicator>
                    <LuMessageSquare />
                  </EmptyState.Indicator>
                  <VStack gap={1}>
                    <EmptyState.Title>No messages yet</EmptyState.Title>
                    <EmptyState.Description>
                      Send a message to start this conversation.
                    </EmptyState.Description>
                  </VStack>
                </EmptyState.Content>
              </EmptyState.Root>
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
                      initial={{ opacity: 0, y: 6 }}
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
                <Box
                  key={message.id}
                  alignSelf="flex-end"
                  maxW="80%"
                  px={2}
                  py={1.5}
                  borderRadius="sm"
                  border="1px dashed"
                  borderColor="border"
                  bg="bg.subtle"
                  opacity={0.55}
                  cursor="pointer"
                  title="Queued — click to remove"
                  onClick={() => dequeueMessage(index)}
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
              ))}
            </VStack>
          )}
        </Box>

        <ChatInput
          onSend={handleSend}
          onAbort={abort}
          isStreaming={isStreaming}
          tasks={tasks}
          disabled={!isConnected}
          sessionId={sessionId}
          workingDirectory={workingDirectory}
          recentProjects={recentProjects}
          onWorkingDirectoryChange={onWorkingDirectoryChange}
          onBrowseFolder={onBrowseFolder}
          sandboxEnabled={sandboxEnabled}
          onSandboxEnabledChange={onSandboxEnabledChange}
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
          historyOpen={historyOpen}
          onToggleHistory={onToggleHistory}
          models={models}
          modelProviders={modelProviders}
          recentModels={recentModels}
          selectedModel={selectedModel}
          onModelChange={(model) => onModelChange?.(model)}
          thinkingLabel={hasActiveToolGroup ? null : liveStatusLabel}
        />
      </Flex>

      <AnimatePresence initial={false}>
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
