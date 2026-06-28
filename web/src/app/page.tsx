"use client";

import { Box, Button, EmptyState, Flex, Spinner, Text, VStack } from "@chakra-ui/react";
import { LuGripVertical, LuMessageSquare, LuPlus } from "react-icons/lu";
import { Suspense, useCallback, useEffect, useState, type PointerEvent } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { browseWorkingDirectory, fetchAgents, fetchAgentCards, fetchHomeDirectory, fetchRecentProjects, fetchSessions, fetchSettings, recordRecentProject, setSandboxEnabled, subscribeEvents, type AgentCard, type AgentSummary, type RecentProject } from "@/lib/api";
import { ChatPanel } from "@/components/chat-panel";

interface SessionEntry {
  sessionId: string;
  agent: string;
  title: string;
  createdAt: string;
  workingDirectory: string;
  running: boolean;
}

function formatSessionTimestamp(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Intl.DateTimeFormat(undefined, {
    month: "long",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

function HomeContent() {
  const router = useRouter();
  const searchParams = useSearchParams();

  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [agentCards, setAgentCards] = useState<AgentCard[]>([]);
  const [selectedAgent, setSelectedAgent] = useState<string>("");
  const [isConnected, setIsConnected] = useState(false);

  const [sessions, setSessions] = useState<SessionEntry[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(() => searchParams.get("session"));
  const [chatKey, setChatKey] = useState(0);
  const [workingDirectory, setWorkingDirectory] = useState("");
  const [recentProjects, setRecentProjects] = useState<RecentProject[]>([]);
  const [sandboxEnabledState, setSandboxEnabledState] = useState(true);
  const [historyOpen, setHistoryOpen] = useState(true);
  const [historyWidth, setHistoryWidth] = useState(280);

  const isCompactViewport = useCallback(() => {
    return window.matchMedia("(max-width: 767px)").matches;
  }, []);

  const refreshRecentProjects = useCallback(() => {
    fetchRecentProjects()
      .then(setRecentProjects)
      .catch(() => {});
  }, []);

  const selectWorkingDirectory = useCallback((directory: string) => {
    const path = directory.trim();
    setWorkingDirectory(path);
    if (!path) return;
    recordRecentProject(path)
      .then((project) => {
        // Surface the server's canonical name immediately so the selector never
        // needs to derive a folder name from the path itself.
        if (project) {
          setRecentProjects((previous) => [
            { ...project, last_used_at: new Date().toISOString() },
            ...previous.filter((entry) => entry.path !== project.path),
          ]);
        }
        refreshRecentProjects();
      })
      .catch(() => {});
  }, [refreshRecentProjects]);

  const applySessions = useCallback((serverSessions: Awaited<ReturnType<typeof fetchSessions>>) => {
    setSessions(
      serverSessions.map((session) => ({
        sessionId: session.session_id,
        agent: session.agent,
        title: session.title,
        createdAt: session.created_at,
        workingDirectory: session.working_directory ?? "",
        running: session.running ?? false,
      }))
    );
  }, []);

  async function handleBrowseFolder() {
    try {
      const result = await browseWorkingDirectory();
      if (!result.cancelled && result.path) selectWorkingDirectory(result.path);
    } catch {
      // user cancelled
    }
  }

  useEffect(() => {
    const loadAgents = () => {
      fetchAgents()
        .then((agentList) => {
          setAgents(agentList);
          setSelectedAgent((current) => current || (agentList[0]?.id ?? ""));
          setIsConnected(true);
        })
        .catch(() => setIsConnected(false));
      fetchAgentCards().then(setAgentCards).catch(() => {});
    };
    const loadSessions = () => {
      fetchSessions()
        .then(applySessions)
        .catch(() => {});
    };
    const loadSettings = () => {
      fetchSettings()
        .then((settings) => setSandboxEnabledState(settings.sandbox_enabled ?? true))
        .catch(() => {});
    };
    loadAgents();
    loadSettings();
    fetchHomeDirectory()
      .then((home) => setWorkingDirectory(home))
      .catch(() => {});
    loadSessions();
    refreshRecentProjects();

    // Live reload: refresh agents when they change on disk, and the session list
    // when a session's (LLM-generated) title is updated.
    const unsubscribe = subscribeEvents((event) => {
      if (event.type === "agents_changed") loadAgents();
      if (event.type === "sessions_changed") loadSessions();
      if (event.type === "projects_changed") refreshRecentProjects();
    });
    return unsubscribe;
  }, [applySessions, refreshRecentProjects]);

  const selectedCard =
    agentCards.find((card) => card.url.endsWith(`/agents/${selectedAgent}`)) ?? null;
  const activeSessionRunning = sessions.find((entry) => entry.sessionId === activeSessionId)?.running ?? false;
  const agentNames = new Map(agents.map((agent) => [agent.id, agent.title || agent.name]));

  const refreshSessions = useCallback(() => {
    fetchSessions()
      .then(applySessions)
      .catch(() => {});
  }, [applySessions]);

  const handleSessionCreated = useCallback(
    (sessionId: string) => {
      setActiveSessionId(sessionId);
      const params = new URLSearchParams(window.location.search);
      params.set("session", sessionId);
      router.replace(`?${params.toString()}`, { scroll: false });
      if (workingDirectory) {
        recordRecentProject(workingDirectory)
          .then(refreshRecentProjects)
          .catch(() => {});
      }
      if (isCompactViewport()) setHistoryOpen(false);
      refreshSessions();
      setTimeout(refreshSessions, 5000);
    },
    [isCompactViewport, refreshRecentProjects, refreshSessions, router, workingDirectory]
  );

  const handleStreamingChange = useCallback((streaming: boolean) => {
    if (!streaming) {
      setTimeout(refreshSessions, 1000);
    }
  }, [refreshSessions]);

  function handleNewChat() {
    setActiveSessionId(null);
    setChatKey((current) => current + 1);
    const params = new URLSearchParams(window.location.search);
    params.delete("session");
    router.replace(`?${params.toString()}`, { scroll: false });
    if (isCompactViewport()) setHistoryOpen(false);
  }

  function handleResumeSession(entry: SessionEntry) {
    setSelectedAgent(entry.agent);
    if (entry.workingDirectory) selectWorkingDirectory(entry.workingDirectory);
    setActiveSessionId(entry.sessionId);
    setChatKey((current) => current + 1);
    const params = new URLSearchParams(window.location.search);
    params.set("session", entry.sessionId);
    router.replace(`?${params.toString()}`, { scroll: false });
    if (isCompactViewport()) setHistoryOpen(false);
  }

  function handleAgentChange(agentName: string) {
    // Switching persona continues the current conversation — the new agent picks
    // up the same session (its system prompt is injected on top of the shared
    // history). Only an explicit "New conversation" starts a fresh session.
    setSelectedAgent(agentName);
  }

  async function handleSandboxEnabledChange(enabled: boolean) {
    const previous = sandboxEnabledState;
    setSandboxEnabledState(enabled);
    try {
      await setSandboxEnabled(enabled);
    } catch {
      setSandboxEnabledState(previous);
    }
  }

  function handleSlashCommand(command: string) {
    if (command === "/new" || command === "/clear") {
      handleNewChat();
    } else if (command.startsWith("/agent ")) {
      const agentName = command.slice(7).trim();
      if (agents.some((agent) => agent.id === agentName)) {
        handleAgentChange(agentName);
      }
    }
  }

  const handleHistoryResizeStart = useCallback((event: PointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = historyWidth;

    function handlePointerMove(moveEvent: globalThis.PointerEvent) {
      const nextWidth = Math.min(520, Math.max(220, startWidth + moveEvent.clientX - startX));
      setHistoryWidth(nextWidth);
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
  }, [historyWidth]);

  return (
    <Flex h="100dvh" minW={0}>
      {historyOpen && (
        <Flex
          direction="column"
          w={{ base: "100%", md: `${historyWidth}px` }}
          maxW={{ base: "100%", md: "46vw" }}
          minW={{ base: "100%", md: "220px" }}
          borderRight="1px solid"
          h={{ base: "100dvh", md: "auto" }}
          borderColor="border"
          flexShrink={0}
          position="relative"
          minH={0}
          display={{ base: historyOpen ? "flex" : "none", md: "flex" }}
        >
          <Box
            display={{ base: "none", md: "block" }}
            position="absolute"
            top={0}
            bottom={0}
            right="-4px"
            w="8px"
            cursor="col-resize"
            zIndex={1}
            onPointerDown={handleHistoryResizeStart}
          />
          <Flex align="center" gap={2} px={2.5} py={2.5} borderBottom="1px solid" borderColor="border">
            <Button
              w="100%"
              size="xs"
              variant="solid"
              colorPalette="blue"
              borderRadius="sm"
              fontSize="xs"
              onClick={handleNewChat}
            >
              <LuPlus size={12} />
              New conversation
            </Button>
          </Flex>

          <Box flex={1} minH={0} overflowY="auto" px={2} py={2}>
            <Flex align="center" gap={1.5} mb={1.5} color="fg.muted">
              <LuGripVertical size={12} />
              <Text fontSize="xs" fontWeight="bold">
                Sessions
              </Text>
            </Flex>
            {sessions.length === 0 ? (
              <EmptyState.Root size="sm">
                <EmptyState.Content>
                  <EmptyState.Indicator>
                    <LuMessageSquare />
                  </EmptyState.Indicator>
                  <VStack gap={0}>
                    <EmptyState.Title fontSize="xs">No sessions</EmptyState.Title>
                    <EmptyState.Description fontSize="xs">
                      Start a conversation
                    </EmptyState.Description>
                  </VStack>
                </EmptyState.Content>
              </EmptyState.Root>
            ) : (
              <VStack gap={1.5} align="stretch">
                {sessions.map((entry) => {
                  const sessionTimestamp = formatSessionTimestamp(entry.createdAt);
                  const sessionAgent = agentNames.get(entry.agent) ?? entry.agent;
                  const sessionMeta = [sessionTimestamp, sessionAgent].filter(Boolean).join(" — ");

                  return (
                    <Box
                      key={entry.sessionId}
                      px={2}
                      py={1}
                      borderRadius="sm"
                      borderColor="border"
                      cursor="pointer"
                      bg={entry.sessionId === activeSessionId ? "bg.emphasized" : undefined}
                      _hover={{ bg: "bg.muted" }}
                      onClick={() => handleResumeSession(entry)}
                    >
                      <Flex align="center" gap={1.5}>
                        <Text fontSize="xs" fontWeight="medium" truncate flex={1}>
                          {entry.title || "Untitled conversation"}
                        </Text>
                        {entry.running && (
                          <Spinner size="xs" color="blue.fg" flexShrink={0} borderWidth="1.5px" />
                        )}
                      </Flex>
                      <Text fontSize="xs" color="fg.subtle" truncate>
                        {sessionMeta}
                      </Text>
                    </Box>
                  );
                })}
              </VStack>
            )}
          </Box>
        </Flex>
      )}

      <Box
        flex={1}
        minW={0}
        overflow="hidden"
        display={{ base: historyOpen ? "none" : "block", md: "block" }}
      >
        <ChatPanel
          key={chatKey}
          agent={selectedAgent}
          agents={agents}
          agentCard={selectedCard}
          onAgentChange={handleAgentChange}
          initialSessionId={activeSessionId}
          sessionRunning={activeSessionRunning}
          onSessionCreated={handleSessionCreated}
          onSlashCommand={handleSlashCommand}
          workingDirectory={workingDirectory}
          recentProjects={recentProjects.map((project) => ({ path: project.path, name: project.name }))}
          onWorkingDirectoryChange={selectWorkingDirectory}
          onBrowseFolder={handleBrowseFolder}
          sandboxEnabled={sandboxEnabledState}
          onSandboxEnabledChange={handleSandboxEnabledChange}
          isConnected={isConnected}
          onStreamingChange={handleStreamingChange}
          historyOpen={historyOpen}
          onToggleHistory={() => setHistoryOpen((current) => !current)}
        />
      </Box>
    </Flex>
  );
}

export default function Home() {
  return (
    <Suspense fallback={<Flex h="100dvh" />}>
      <HomeContent />
    </Suspense>
  );
}
