"use client";

import {
  Box,
  Button,
  EmptyState,
  Flex,
  Text,
  VStack,
} from "@chakra-ui/react";
import { LuClock, LuSend, LuTriangleAlert } from "react-icons/lu";
import { useCallback, useEffect, useLayoutEffect, useRef, useState, type PointerEvent } from "react";
import { useChat, isStepDone } from "@/lib/use-chat";
import { ChatMessageItem } from "./chat-message";
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
  recentProjects?: { path: string; name: string }[];
  onWorkingDirectoryChange?: (dir: string) => void;
  onBrowseFolder?: () => void;
  sandboxEnabled?: boolean;
  onSandboxEnabledChange?: (enabled: boolean) => void;
  isConnected?: boolean;
  onStreamingChange?: (isStreaming: boolean) => void;
  historyOpen?: boolean;
  onToggleHistory?: () => void;
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
  recentProjects,
  onWorkingDirectoryChange,
  onBrowseFolder,
  sandboxEnabled = true,
  onSandboxEnabledChange,
  isConnected = false,
  onStreamingChange,
  historyOpen = false,
  onToggleHistory,
}: ChatPanelProps) {
  const [permissionMode, setPermissionModeState] = useState<PermissionMode>("default");
  const { messages, agentGroups, queuedMessages, sessionId, isStreaming, isHistoryLoading, historyError, reloadHistory, send, sendWidgetEvent, abort, dequeueMessage, handlePermission } =
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
              <EmptyState.Root>
                <EmptyState.Content>
                  <EmptyState.Indicator>
                    <LuSend />
                  </EmptyState.Indicator>
                  <VStack gap={1}>
                    <EmptyState.Title>No messages yet</EmptyState.Title>
                    <EmptyState.Description>
                      Send a message to start
                    </EmptyState.Description>
                  </VStack>
                </EmptyState.Content>
              </EmptyState.Root>
              <AgentSkills card={agentCard ?? null} workingDirectory={workingDirectory} />
            </Flex>
          ) : (
            <VStack ref={scrollContentRef} gap={2} align="stretch">
              {messages.map((message) => (
                <ChatMessageItem
                  key={message.id}
                  message={message}
                  onPermission={handlePermission}
                  agents={agents}
                />
              ))}
              {queuedMessages.map((text, index) => (
                <Box
                  key={`queued-${index}`}
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
                    <LuClock size={11} />
                    <Text fontSize="xs" color="fg.subtle" fontWeight="medium">Queued</Text>
                  </Flex>
                  <Text fontSize="sm" color="fg.muted">{text}</Text>
                </Box>
              ))}
            </VStack>
          )}
        </Box>

        <ChatInput
          onSend={handleSend}
          onAbort={abort}
          isStreaming={isStreaming}
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
        />
      </Flex>

      <AgentsPanel
        agentGroups={agentGroups}
        agents={agents}
        open={agentsPanelOpen}
        onClose={() => setAgentsPanelOpen(false)}
        focusedGroupId={focusedGroupId}
        width={agentsSidebarWidth}
        onResizeStart={handleAgentsResizeStart}
      />
    </Flex>
    </WidgetEventProvider>
  );
}
