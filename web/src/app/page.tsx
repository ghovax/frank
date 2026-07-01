"use client";

import { Box, Button, EmptyState, Flex, Spinner, Text, VStack } from "@chakra-ui/react";
import { LuFolder, LuGitBranch, LuGitFork, LuGripVertical, LuMessageSquare, LuPencilLine, LuPlus, LuTriangleAlert } from "react-icons/lu";
import { AnimatePresence, motion } from "motion/react";
import { Suspense, useCallback, useEffect, useMemo, useRef, useState, type PointerEvent } from "react";

// A Chakra Flex that is also a motion component, so the history sidebar can
// animate its open/close (opacity + slide) without losing its flex-layout props.
const MotionFlex = motion.create(Flex);
import { useRouter, useSearchParams } from "next/navigation";
import { browseWorkingDirectory, fetchAgents, fetchAgentCards, fetchHomeDirectory, fetchModels, fetchRecentModels, fetchRecentProjects, fetchSessions, fetchSettings, recordRecentProject, setSandboxEnabled, setSessionModel, subscribeEvents, type AgentCard, type AgentSummary, type FilesystemLease, type ModelOption, type ProviderOption, type RecentProject } from "@/lib/api";
import { ChatPanel } from "@/components/chat-panel";
import { CollapsibleSection } from "@/components/collapsible-section";

interface SessionEntry {
  sessionId: string;
  agent: string;
  title: string;
  createdAt: string;
  workingDirectory: string;
  workingDirectoryName: string;
  runtimeWorkingDirectory: string;
  runtimeWorkingDirectoryName: string;
  workspaceStrategy: "none" | "branch" | "worktree";
  workspacePath: string;
  workspaceBranch: string;
  workspaceError: string;
  running: boolean;
  awaitingInput: boolean;
  filesystemLeases: FilesystemLease[];
  model?: string;
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

function projectGroupKey(entry: SessionEntry): string {
  return entry.workingDirectory || "__unknown__";
}

function projectGroupName(entry: SessionEntry): string {
  return entry.workingDirectoryName || "No project";
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
  const [homeProject, setHomeProject] = useState<{ path: string; name: string } | null>(null);
  const [recentProjects, setRecentProjects] = useState<RecentProject[]>([]);
  const [sandboxEnabledState, setSandboxEnabledState] = useState(true);
  const [models, setModels] = useState<ModelOption[]>([]);
  const [modelProviders, setModelProviders] = useState<ProviderOption[]>([]);
  const [recentModels, setRecentModels] = useState<{ id: string; name: string; provider: string }[]>([]);
  // The active session's model override; "" means use the globally selected model.
  const [selectedModel, setSelectedModel] = useState<string>("");
  // The globally-selected default model (from the catalog), shown on the composer
  // chip whenever there is no per-conversation override.
  const [globalModel, setGlobalModel] = useState<string>("");
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

  // Agents, their cards (skills), MCP servers and memories are all scoped to the
  // selected folder — home globals plus that folder's own `.agents`, deduped,
  // never the server's launch directory. The ref lets the live-reload handler
  // refetch with the current folder without re-subscribing.
  const workingDirectoryRef = useRef(workingDirectory);
  useEffect(() => {
    workingDirectoryRef.current = workingDirectory;
  }, [workingDirectory]);
  const loadAgentCards = useCallback(() => {
    fetchAgentCards(workingDirectoryRef.current).then(setAgentCards).catch(() => {});
  }, []);
  const loadAgents = useCallback(() => {
    fetchAgents(workingDirectoryRef.current)
      .then((agentList) => {
        setAgents(agentList);
        // Keep the current selection if it's still available in this folder,
        // otherwise fall back to the first agent the folder offers.
        setSelectedAgent((current) =>
          agentList.some((agent) => agent.id === current) ? current : (agentList[0]?.id ?? "")
        );
        setIsConnected(true);
      })
      .catch(() => setIsConnected(false));
  }, []);

  const loadModelCatalog = useCallback(() => {
    fetchModels()
      .then((catalog) => {
        setModels(catalog.models);
        setModelProviders(catalog.providers);
        setGlobalModel(catalog.selected_model ?? "");
      })
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
        workingDirectoryName: session.working_directory_name ?? "",
        runtimeWorkingDirectory: session.runtime_working_directory ?? session.working_directory ?? "",
        runtimeWorkingDirectoryName: session.runtime_working_directory_name ?? session.working_directory_name ?? "",
        workspaceStrategy: session.workspace_strategy ?? "none",
        workspacePath: session.workspace_path ?? "",
        workspaceBranch: session.workspace_branch ?? "",
        workspaceError: session.workspace_error ?? "",
        running: session.running ?? false,
        awaitingInput: session.awaiting_input ?? false,
        filesystemLeases: session.filesystem_leases ?? [],
        model: session.model ?? "",
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
    loadSettings();
    // The model catalog (provider list + curated models) drives the composer's
    // per-session model override and the settings dialog's default-model picker.
    loadModelCatalog();
    fetchRecentModels()
      .then(setRecentModels)
      .catch(() => {});
    // Home is the default project for a brand-new chat; the restoration effect
    // below applies it (or the active session's own folder) — we don't force it
    // here, or it would clobber a session opened directly via ?session=.
    fetchHomeDirectory()
      .then(setHomeProject)
      .catch(() => {});
    loadSessions();
    refreshRecentProjects();

    // Live reload: refresh agents when they change on disk, and the session list
    // when a session's (LLM-generated) title is updated.
    const unsubscribe = subscribeEvents((event) => {
      if (event.type === "agents_changed") {
        loadAgents();
        loadAgentCards();
      }
      if (event.type === "sessions_changed" || event.type === "filesystem_leases_changed") loadSessions();
      if (event.type === "projects_changed") refreshRecentProjects();
      if (event.type === "settings_changed") {
        loadSettings();
        loadModelCatalog();
        fetchRecentModels().then(setRecentModels).catch(() => {});
      }
    });
    return unsubscribe;
  }, [applySessions, refreshRecentProjects, loadAgents, loadAgentCards, loadModelCatalog]);

  // Reload the agents and their cards whenever the selected folder changes (and
  // on first render): the available agents, skills and MCP servers are all
  // path-scoped, so picking a different project must re-derive what's actually
  // available there rather than showing the launch directory's capabilities.
  useEffect(() => {
    loadAgents();
    loadAgentCards();
  }, [workingDirectory, loadAgents, loadAgentCards]);

  // The working directory is bound to the active context: an open session is
  // restored to its own persisted folder, a brand-new chat falls back to home.
  // Adjusted during render (the sanctioned pattern) and guarded so it binds once
  // per context — it sets the initial folder without clobbering a deliberate
  // change the user makes within that session.
  const [restoredContext, setRestoredContext] = useState<string | null>(null);
  const contextKey = activeSessionId ?? "__new__";
  if (restoredContext !== contextKey) {
    if (activeSessionId) {
      const session = sessions.find((entry) => entry.sessionId === activeSessionId);
      if (session) {
        setRestoredContext(contextKey);
        setWorkingDirectory(session.workingDirectory || homeProject?.path || "");
      }
    } else if (workingDirectory || homeProject) {
      // A brand-new chat inherits the working directory the user was just in —
      // no jarring folder reset when starting a new conversation. Only fall back
      // to home if there's no directory yet (first load).
      setRestoredContext(contextKey);
      if (!workingDirectory) {
        setWorkingDirectory(homeProject?.path || "");
      }
    }
  }

  // The selector always knows a real name for the current folder: home leads as
  // the default, the active session's own folder is guaranteed present (so a
  // freshly reopened session never falls back to a generic label before recents
  // load), then the recents — all server-named, deduped by path.
  const activeSessionProject = useMemo(() => {
    const session = sessions.find((entry) => entry.sessionId === activeSessionId);
    return session?.workingDirectory
      ? { path: session.workingDirectory, name: session.workingDirectoryName }
      : null;
  }, [sessions, activeSessionId]);
  const projectsForSelector = useMemo(() => {
    const seen = new Set<string>();
    const items: { path: string; name: string }[] = [];
    const sources = [homeProject, activeSessionProject, ...recentProjects].filter(
      (project): project is { path: string; name: string } => !!project && !!project.path
    );
    for (const project of sources) {
      if (seen.has(project.path)) continue;
      seen.add(project.path);
      items.push({ path: project.path, name: project.name });
    }
    return items;
  }, [homeProject, activeSessionProject, recentProjects]);

  const selectedCard =
    agentCards.find((card) => card.url.endsWith(`/agents/${selectedAgent}`)) ?? null;
  const activeSessionRunning = sessions.find((entry) => entry.sessionId === activeSessionId)?.running ?? false;
  const groupedSessions = useMemo(() => {
    const groups = new Map<string, { key: string; name: string; path: string; sessions: SessionEntry[] }>();
    for (const session of sessions) {
      const key = projectGroupKey(session);
      const existing = groups.get(key);
      if (existing) {
        existing.sessions.push(session);
      } else {
        groups.set(key, {
          key,
          name: projectGroupName(session),
          path: session.workingDirectory,
          sessions: [session],
        });
      }
    }
    return [...groups.values()];
  }, [sessions]);

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
      // Persist the model the user picked before the first message (a new chat
      // had no session id to attach it to) so the next turn runs on it.
      if (selectedModel) {
        setSessionModel(sessionId, selectedModel).catch(() => {});
      }
      if (workingDirectory) {
        recordRecentProject(workingDirectory)
          .then(refreshRecentProjects)
          .catch(() => {});
      }
      if (isCompactViewport()) setHistoryOpen(false);
      refreshSessions();
      setTimeout(refreshSessions, 5000);
    },
    [isCompactViewport, refreshRecentProjects, refreshSessions, router, selectedModel, workingDirectory]
  );

  const handleStreamingChange = useCallback((streaming: boolean) => {
    if (!streaming) {
      setTimeout(refreshSessions, 1000);
    }
  }, [refreshSessions]);

  function handleNewChat() {
    setActiveSessionId(null);
    setSelectedModel("");
    setChatKey((current) => current + 1);
    const params = new URLSearchParams(window.location.search);
    params.delete("session");
    router.replace(`?${params.toString()}`, { scroll: false });
    if (isCompactViewport()) setHistoryOpen(false);
  }

  function handleResumeSession(entry: SessionEntry) {
    setSelectedAgent(entry.agent);
    setSelectedModel(entry.model ?? "");
    // The restoration effect rebinds the working directory to this session's
    // own persisted folder; no need to set (or re-record) it here.
    setActiveSessionId(entry.sessionId);
    setChatKey((current) => current + 1);
    const params = new URLSearchParams(window.location.search);
    params.set("session", entry.sessionId);
    router.replace(`?${params.toString()}`, { scroll: false });
    if (isCompactViewport()) setHistoryOpen(false);
  }

  async function handleModelChange(model: string) {
    const previous = selectedModel;
    setSelectedModel(model);
    // A new chat has no session id yet; the override is applied when the session
    // is created on first send (the server seeds it from the global default, and
    // the user can re-pick). For an existing session, persist immediately.
    if (activeSessionId) {
      try {
        await setSessionModel(activeSessionId, model);
        fetchRecentModels().then(setRecentModels).catch(() => {});
      } catch {
        setSelectedModel(previous);
      }
    }
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
      <AnimatePresence initial={false}>
        {historyOpen && (
          <MotionFlex
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
            display="flex"
            initial={{ opacity: 0, x: -24 }}
            animate={{ opacity: 1, x: 0 }}
            exit={{ opacity: 0, x: -24 }}
            transition={{ duration: 0.2, ease: [0.4, 0, 0.2, 1] }}
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
                    <LuFolder />
                  </EmptyState.Indicator>
                  <VStack gap={0}>
                    <EmptyState.Title fontSize="sm">No sessions</EmptyState.Title>
                    <EmptyState.Description fontSize="xs">
                      Start a conversation to see it
                    </EmptyState.Description>
                  </VStack>
                </EmptyState.Content>
              </EmptyState.Root>
            ) : (
              <VStack gap={2} align="stretch">
                {groupedSessions.map((group) => (
                  <CollapsibleSection
                    key={group.key}
                    title={group.name}
                    subtitle={group.path || undefined}
                    count={group.sessions.length}
                    icon={<LuFolder size={12} />}
                    initialVisibleCount={5}
                  >
                    {group.sessions.map((entry) => {
                      const sessionMeta = formatSessionTimestamp(entry.createdAt);
                      const activeLease = entry.filesystemLeases[0];
                      const workspaceTitle = entry.workspaceStrategy === "worktree"
                        ? `Session worktree: ${entry.workspaceBranch}\n${entry.runtimeWorkingDirectory}`
                        : entry.workspaceStrategy === "branch"
                          ? `Session branch: ${entry.workspaceBranch}\n${entry.runtimeWorkingDirectory}`
                          : entry.workspaceError || "No automatic Git workspace";

                      return (
                        <Box
                          key={entry.sessionId}
                          px={2}
                          py={1}
                          borderRadius="sm"
                          cursor="pointer"
                          bg={entry.sessionId === activeSessionId ? "bg.emphasized" : undefined}
                          _hover={{ bg: "bg.muted" }}
                          onClick={() => handleResumeSession(entry)}
                        >
                          <Flex align="center" gap={1.5}>
                            <Text fontSize="xs" fontWeight="medium" truncate flex={1}>
                              {entry.title || "Untitled conversation"}
                            </Text>
                            {entry.awaitingInput ? (
                              <Box color="yellow.fg" flexShrink={0} display="flex" alignItems="center" title="Waiting for your approval">
                                <LuTriangleAlert size={13} />
                              </Box>
                            ) : activeLease ? (
                              <Box
                                color="orange.fg"
                                flexShrink={0}
                                display="flex"
                                alignItems="center"
                                title={`Filesystem edit lease: ${activeLease.scope} ${activeLease.path}`}
                              >
                                <LuPencilLine size={13} />
                              </Box>
                            ) : entry.running ? (
                              <Spinner size="xs" color="blue.fg" flexShrink={0} borderWidth="1.5px" />
                            ) : null}
                          </Flex>
                          <Flex align="center" gap={1} minW={0}>
                            {entry.workspaceStrategy === "worktree" && (
                              <Box color="green.fg" flexShrink={0} display="flex" alignItems="center" title={workspaceTitle}>
                                <LuGitFork size={11} />
                              </Box>
                            )}
                            {entry.workspaceStrategy === "branch" && (
                              <Box color="purple.fg" flexShrink={0} display="flex" alignItems="center" title={workspaceTitle}>
                                <LuGitBranch size={11} />
                              </Box>
                            )}
                            <Text fontSize="xs" color="fg.subtle" truncate>
                              {sessionMeta}
                            </Text>
                          </Flex>
                        </Box>
                      );
                    })}
                  </CollapsibleSection>
                ))}
              </VStack>
            )}
          </Box>
          </MotionFlex>
        )}
      </AnimatePresence>

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
          homeDirectory={homeProject?.path ?? ""}
          recentProjects={projectsForSelector}
          onWorkingDirectoryChange={selectWorkingDirectory}
          onBrowseFolder={handleBrowseFolder}
          sandboxEnabled={sandboxEnabledState}
          onSandboxEnabledChange={handleSandboxEnabledChange}
          isConnected={isConnected}
          onStreamingChange={handleStreamingChange}
          historyOpen={historyOpen}
          onToggleHistory={() => setHistoryOpen((current) => !current)}
          models={models}
          modelProviders={modelProviders}
          recentModels={recentModels}
          selectedModel={selectedModel}
          globalModel={globalModel}
          onModelChange={handleModelChange}
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
