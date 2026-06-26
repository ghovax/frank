"use client";

import { Box, Button, EmptyState, Flex, Text, VStack } from "@chakra-ui/react";
import { LuGripVertical, LuMessageSquare, LuPlus } from "react-icons/lu";
import { Suspense, useCallback, useEffect, useState, type PointerEvent } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { fetchAgents, fetchHomeDirectory, fetchSessions } from "@/lib/api";
import { ChatPanel } from "@/components/chat-panel";

interface SessionEntry {
  sessionId: string;
  agent: string;
  title: string;
  createdAt: string;
}

function HomeContent() {
  const router = useRouter();
  const searchParams = useSearchParams();

  const [agents, setAgents] = useState<{ name: string; label: string }[]>([]);
  const [selectedAgent, setSelectedAgent] = useState<string>("");
  const [isConnected, setIsConnected] = useState(false);

  const [sessions, setSessions] = useState<SessionEntry[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(() => searchParams.get("session"));
  const [chatKey, setChatKey] = useState(0);
  const [workingDirectory, setWorkingDirectory] = useState("");
  const [historyOpen, setHistoryOpen] = useState(true);
  const [historyWidth, setHistoryWidth] = useState(280);

  function isCompactViewport() {
    return window.matchMedia("(max-width: 767px)").matches;
  }

  async function handleBrowseFolder() {
    try {
      const showPicker = (window as unknown as { showDirectoryPicker: (opts: { mode: string }) => Promise<{ name: string }> }).showDirectoryPicker;
      if (showPicker) {
        const handle = await showPicker({ mode: "read" });
        setWorkingDirectory(handle.name);
      }
    } catch {
      // user cancelled
    }
  }

  useEffect(() => {
    fetchAgents()
      .then((agentList) => {
        setAgents(agentList);
        if (agentList.length > 0) setSelectedAgent(agentList[0].name);
        setIsConnected(true);
      })
      .catch(() => {
        setIsConnected(false);
      });
    fetchHomeDirectory()
      .then((home) => setWorkingDirectory(home))
      .catch(() => {});
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

  useEffect(() => {
    if (activeSessionId && isCompactViewport()) {
      setHistoryOpen(false);
    }
  }, [activeSessionId]);

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
      refreshSessions();
      setTimeout(refreshSessions, 5000);
    },
    [refreshSessions, router]
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
      if (agents.some((agent) => agent.name === agentName)) {
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
              <VStack gap={2} align="stretch">
                {sessions.map((entry) => (
                  <Box
                    key={entry.sessionId}
                    px={1.5}
                    py={1}
                    borderRadius="sm"
                    border="1px solid"
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
                      {entry.sessionId.slice(0, 8)}
                    </Text>
                  </Box>
                ))}
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
