"use client";

import { Box, Button, EmptyState, Flex, Text, VStack } from "@chakra-ui/react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { LuMessageSquare } from "react-icons/lu";
import { fetchAgents, fetchHomeDirectory, fetchSessions } from "@/lib/api";
import { ChatPanel } from "@/components/chat-panel";

interface SessionEntry {
  sessionId: string;
  agent: string;
  createdAt: string;
}

export default function Home() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
  }, []);

  useEffect(() => {
    const fromUrl = searchParams.get("session");
    if (fromUrl) {
      setActiveSessionId(fromUrl);
    }
  }, []);

  const [agents, setAgents] = useState<string[]>([]);
  const [selectedAgent, setSelectedAgent] = useState<string>("");
  const [isConnected, setIsConnected] = useState(false);

  const [sessions, setSessions] = useState<SessionEntry[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [chatKey, setChatKey] = useState(0);
  const [workingDirectory, setWorkingDirectory] = useState("");

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
        if (agentList.length > 0) setSelectedAgent(agentList[0]);
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
      fetchSessions()
        .then((serverSessions) =>
          setSessions(
            serverSessions.map((session) => ({
              sessionId: session.session_id,
              agent: session.agent,
              createdAt: session.created_at,
            }))
          )
        )
        .catch(() => {});
    },
    [router]
  );

  function handleNewChat() {
    setActiveSessionId(null);
    setChatKey((current) => current + 1);
    const params = new URLSearchParams(window.location.search);
    params.delete("session");
    router.replace(`?${params.toString()}`, { scroll: false });
  }

  function handleResumeSession(entry: SessionEntry) {
    setSelectedAgent(entry.agent);
    setActiveSessionId(entry.sessionId);
    setChatKey((current) => current + 1);
    const params = new URLSearchParams(window.location.search);
    params.set("session", entry.sessionId);
    router.replace(`?${params.toString()}`, { scroll: false });
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
      if (agents.includes(agentName)) {
        handleAgentChange(agentName);
      }
    }
  }

  if (!mounted) {
    return <Flex h="100vh" />;
  }

  return (
    <Flex h="100vh">
      <Flex
        direction="column"
        w="260px"
        borderRight="1px solid"
        borderColor="border"
        flexShrink={0}
      >
        <Box px={3} py={3}>
          <Button
            w="100%"
            size="sm"
            variant="surface"
            borderRadius="lg"
            fontSize="xs"
            onClick={handleNewChat}
          >
            New conversation
          </Button>
        </Box>

        <Box flex={1} overflowY="auto" px={3} pb={3}>
          <Text fontSize="sm" color="fg.muted" fontWeight="bold" mb={1.5}>
            Sessions
          </Text>
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
            <VStack gap={1} align="stretch">
              {sessions.map((entry) => (
                <Box
                  key={entry.sessionId}
                  px={2}
                  py={1.5}
                  borderRadius="lg"
                  cursor="pointer"
                  bg={
                    entry.sessionId === activeSessionId
                      ? "bg.emphasized"
                      : undefined
                  }
                  _hover={{ bg: "bg.muted" }}
                  onClick={() => handleResumeSession(entry)}
                >
                  <Text fontSize="sm" fontWeight="medium" truncate>
                    {entry.agent}
                  </Text>
                  <Text fontSize="xs" color="fg.subtle" truncate>
                    {entry.sessionId}
                  </Text>
                </Box>
              ))}
            </VStack>
          )}
        </Box>
      </Flex>

      <Box flex={1} overflow="hidden">
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
        />
      </Box>
    </Flex>
  );
}
