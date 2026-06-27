"use client";

import { Box, Button, EmptyState, Flex, Text, VStack } from "@chakra-ui/react";
import { LuGripVertical, LuMessageSquare, LuPlus } from "react-icons/lu";
import { Suspense, useCallback, useEffect, useState, type PointerEvent } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { browseWorkingDirectory, fetchAgents, fetchAgentCards, fetchHomeDirectory, fetchSessions, subscribeEvents, type AgentCard, type AgentSummary } from "@/lib/api";
import { ChatPanel } from "@/components/chat-panel";

interface SessionEntry {
  sessionId: string;
  agent: string;
  title: string;
  createdAt: string;
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
  const [historyOpen, setHistoryOpen] = useState(true);
  const [historyWidth, setHistoryWidth] = useState(280);

  const isCompactViewport = useCallback(() => {
    return window.matchMedia("(max-width: 767px)").matches;
  }, []);

  async function handleBrowseFolder() {
    try {
      const result = await browseWorkingDirectory();
      if (!result.cancelled && result.path) setWorkingDirectory(result.path);
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
        .then((serverSessions) =>
          setSessions(
            serverSessions.map((session) => ({
              sessionId: session.session_id,
              agent: session.agent,
              title: session.title,
              createdAt: session.created_at,
            }))
          )
        )
        .catch(() => {});
    };
    loadAgents();
    fetchHomeDirectory()
      .then((home) => setWorkingDirectory(home))
      .catch(() => {});
    loadSessions();

    // Live reload: refresh agents when they change on disk, and the session list
    // when a session's (LLM-generated) title is updated.
    const unsubscribe = subscribeEvents((event) => {
      if (event.type === "agents_changed") loadAgents();
      if (event.type === "sessions_changed") loadSessions();
    });
    return unsubscribe;
  }, []);

  const selectedCard =
    agentCards.find((card) => card.url.endsWith(`/agents/${selectedAgent}`)) ?? null;
  const agentNames = new Map(agents.map((agent) => [agent.id, agent.title || agent.name]));

  const refreshSessions = useCallback(() => {
    fetchSessions()
      .then((serverSessions) =>
        setSessions(
          serverSessions.map((session) => ({
            sessionId: session.session_id,
            agent: session.agent,
            title: session.title,
            createdAt: session.created_at,
          }))
        )
      )
      .catch(() => {});
  }, []);

  const handleSessionCreated = useCallback(
    (sessionId: string) => {
      setActiveSessionId(sessionId);
      const params = new URLSearchParams(window.location.search);
      params.set("session", sessionId);
      router.replace(`?${params.toString()}`, { scroll: false });
      if (isCompactViewport()) setHistoryOpen(false);
      refreshSessions();
      setTimeout(refreshSessions, 5000);
    },
    [isCompactViewport, refreshSessions, router]
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
    setActiveSessionId(entry.sessionId);
    setChatKey((current) => current + 1);
    const params = new URLSearchParams(window.location.search);
    params.set("session", entry.sessionId);
    router.replace(`?${params.toString()}`, { scroll: false });
    if (isCompactViewport()) setHistoryOpen(false);
  }

  function handleAgentChange(agentName: string) {
    setSelectedAgent(agentName);
    handleNewChat();
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
                      <Text fontSize="xs" fontWeight="medium" truncate>
                        {entry.title || "Untitled conversation"}
                      </Text>
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
          onSessionCreated={handleSessionCreated}
          onSlashCommand={handleSlashCommand}
          workingDirectory={workingDirectory}
          onWorkingDirectoryChange={setWorkingDirectory}
          onBrowseFolder={handleBrowseFolder}
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
