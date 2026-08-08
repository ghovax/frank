"use client";

import { Box, Flex } from "@chakra-ui/react";
import { SessionsSidebar, type SessionSort } from "@/components/sessions-sidebar";
import type { SessionActivity, SessionEntry } from "@/components/session-row";
import { AnimatePresence, motion } from "motion/react";
import {
  Suspense,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent,
} from "react";

// A Chakra Flex that is also a motion component, so the history sidebar can animate open and closed without losing its flex props.
const MotionFlex = motion.create(Flex);
import { useRouter, useSearchParams } from "next/navigation";
import {
  deleteSession,
  fetchAccessibility,
  fetchAgents,
  fetchAgentCards,
  fetchHomeDirectory,
  fetchModels,
  fetchRecentModels,
  fetchSessionDraft,
  fetchSessions,
  fetchSettings,
  getWorkspace,
  listWorkspaces,
  rememberLastSession,
  saveAgentConfiguration,
  saveSettings,
  setSandboxEnforce,
  subscribeConnection,
  subscribeEvents,
  updateComputerControlSetting,
  type AgentCard,
  type AgentSummary,
  type ModelOption,
  type PermissionMode,
  type ProviderOption,
  type SandboxEnforce,
} from "@/lib/api";
import { ChatPanel, type SidePanelKey } from "@/components/chat-panel";
import { useTray } from "@/lib/use-tray";
import { playAttentionSound, playTurnEndSound, primeSounds } from "@/lib/sounds";
import { swallowed } from "@/lib/swallowed";
import { usePreferences } from "@/lib/preferences";

// The sessions sidebar is its own component; this page owns the data and the notification tracking.

// The last workspace the user was in and the last conversation within it, both kept by the daemon so a fresh launch reopens them.

// The conversation a session belongs to: itself, or the one at the top of the chain that created it.
function rootSessionOf(sessions: SessionEntry[], sessionId: string | null): string | null {
  if (!sessionId) return null;
  const byId = new Map(sessions.map((session) => [session.sessionId, session]));
  const seen = new Set<string>();
  let current = byId.get(sessionId);
  while (current?.parentSessionId && !seen.has(current.sessionId)) {
    seen.add(current.sessionId);
    const parent = byId.get(current.parentSessionId);
    if (!parent) break;
    current = parent;
  }
  return current?.sessionId ?? sessionId;
}

// A session that is actually working, which the daemon derives as `activity` only while a turn is in flight.
function isSessionBusy(session: SessionEntry): boolean {
  return session.activity === "working";
}

function Workspace() {
  const router = useRouter();
  const searchParams = useSearchParams();
  // Where this interface opens and what it looks like, as the daemon has it, so the phone and this window agree.
  const { preferences, updatePreferences } = usePreferences();
  const readLastWorkspace = () => preferences.last_workspace_id;
  const writeLastWorkspace = (workspaceId: string) =>
    updatePreferences({ last_workspace_id: workspaceId });
  // The app opens straight into a workspace addressed by `?workspace=`, resolving the last used or the first available.
  const workspaceId = searchParams.get("workspace") ?? "";

  // After the user grants Accessibility and the app relaunches, turn computer control on once, since macOS only tells the fresh server.
  useEffect(() => {
    if (!preferences.computer_control_awaiting_grant) return;
    let cancelled = false;
    void fetchAccessibility().then(async (granted) => {
      if (cancelled || !granted) return;
      await updateComputerControlSetting(true);
      updatePreferences({ computer_control_awaiting_grant: false });
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [preferences.computer_control_awaiting_grant]);

  // Which conversation the daemon last saw this workspace opened at, waited for rather than raced.
  const [rememberedSession, setRememberedSession] = useState<string | null>(null);

  useEffect(() => {
    if (workspaceId) {
      writeLastWorkspace(workspaceId);
      return;
    }
    let cancelled = false;
    listWorkspaces()
      .then((workspaces) => {
        if (cancelled) return;
        const last = readLastWorkspace();
        const target =
          last && workspaces.some((workspace) => workspace.id === last) ? last : workspaces[0]?.id;
        // Nothing to open into, so release the restore below to settle on the empty composer.
        if (!target) {
          setRememberedSession("");
          return;
        }
        const params = new URLSearchParams(window.location.search);
        params.set("workspace", target);
        router.replace(`?${params.toString()}`, { scroll: false });
      })
      .catch((caught) =>
        swallowed({ component: "workspace-page", operation: "read the home directory" }, caught),
      );
    return () => {
      cancelled = true;
    };
    // The workspace readers close over preferences, so naming them here would re-run this constantly.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceId, router]);

  useEffect(() => {
    // No workspace yet means the effect above is still choosing one, and will set this itself if there is none.
    if (!workspaceId) return;
    let cancelled = false;
    getWorkspace(workspaceId)
      .then((workspace) => {
        if (!cancelled) setRememberedSession(workspace?.last_session_id ?? "");
      })
      .catch((caught) => {
        // A workspace that would not load is no reason to hang on the loading screen.
        if (!cancelled) setRememberedSession("");
        swallowed({ component: "workspace-page", operation: "read the last conversation" }, caught);
      });
    return () => {
      cancelled = true;
    };
  }, [workspaceId]);

  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [agentCards, setAgentCards] = useState<AgentCard[]>([]);
  const [selectedAgent, setSelectedAgent] = useState<string>("");
  const [isConnected, setIsConnected] = useState(true);

  const [sessions, setSessions] = useState<SessionEntry[]>([]);
  const [sessionsLoaded, setSessionsLoaded] = useState(false);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(() =>
    searchParams.get("session"),
  );
  // Sessions that finished a run while you were not viewing them, detected by comparing snapshots and cleared when you open one.
  const [unseenCompletions, setUnseenCompletions] = useState<Set<string>>(new Set());
  const sessionsRef = useRef<SessionEntry[]>([]);
  const attentionPlayedForRunRef = useRef<Set<string>>(new Set());
  const activeSessionIdRef = useRef<string | null>(activeSessionId);
  const [chatKey, setChatKey] = useState(0);
  // Keep the active-session id readable from callbacks without adding it to their dependencies.
  useEffect(() => {
    activeSessionIdRef.current = activeSessionId;
  }, [activeSessionId]);
  // `?settings=<section>` opens the workspace with that section showing, and is dropped once consumed.
  const settingsSectionParam = searchParams.get("settings");
  useEffect(() => {
    if (!settingsSectionParam) return;
    const params = new URLSearchParams(window.location.search);
    params.delete("settings");
    router.replace(`?${params.toString()}`, { scroll: false });
  }, [settingsSectionParam, router]);
  // The working directory is derived from the workspace's first local location.
  const [workingDirectory, setWorkingDirectory] = useState("");
  const [homeWorkspace, setHomeWorkspace] = useState<{ path: string; name: string } | null>(null);
  const [sandboxEnforceState, setSandboxEnforceState] = useState<SandboxEnforce>("required");
  const [sandboxBackend, setSandboxBackend] = useState({ backend: "", detail: "" });
  const [worktreeStrategy, setWorktreeStrategy] = useState<"none" | "branch" | "worktree">("none");
  const [models, setModels] = useState<ModelOption[]>([]);
  const [modelProviders, setModelProviders] = useState<ProviderOption[]>([]);
  const [recentModels, setRecentModels] = useState<
    { id: string; name: string; provider: string }[]
  >([]);
  const [selectedPermissionMode, setSelectedPermissionMode] = useState<PermissionMode>("ask");
  const [historyOpen, setHistoryOpen] = useState(true);
  // The right-hand panels, held here because ChatPanel remounts on every conversation switch.
  const [openSidePanels, setOpenSidePanels] = useState<SidePanelKey[]>([]);
  // Default right-region width: comfortable for one panel without dwarfing the transcript.
  const [sidePanelWidth, setSidePanelWidth] = useState(480);
  // Default sidebar width: enough for typical session titles, growing by drag and never the reverse.
  const [historyWidth, setHistoryWidth] = useState(268);

  const isCompactViewport = useCallback(() => {
    return window.matchMedia("(max-width: 767px)").matches;
  }, []);

  // Agents, cards, servers and memories are scoped to the selected folder, and the ref lets live reload refetch with the current one.
  const workingDirectoryRef = useRef(workingDirectory);
  useEffect(() => {
    workingDirectoryRef.current = workingDirectory;
  }, [workingDirectory]);
  const loadAgentCards = useCallback(() => {
    fetchAgentCards(workingDirectoryRef.current)
      .then(setAgentCards)
      .catch((caught) =>
        swallowed({ component: "workspace-page", operation: "list the workspaces" }, caught),
      );
  }, []);
  const loadAgents = useCallback(() => {
    fetchAgents(workingDirectoryRef.current)
      .then((agentList) => {
        setAgents(agentList);
        // Keep the current selection when the folder still offers it, otherwise take the first agent rather than leave sending impossible.
        setSelectedAgent((current) =>
          agentList.some((agent) => agent.id === current) ? current : (agentList[0]?.id ?? ""),
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
      })
      .catch((caught) =>
        swallowed({ component: "workspace-page", operation: "list the models" }, caught),
      );
  }, []);

  const mapSessions = useCallback(
    (serverSessions: Awaited<ReturnType<typeof fetchSessions>>): SessionEntry[] => {
      return serverSessions.map((session) => ({
        sessionId: session.id,
        parentSessionId: session.parent,
        workspaceId: session.workspace_id,
        agent: session.agent,
        title: session.title,
        createdAt: session.created_at,
        workingDirectory: session.working_directory,
        activity: (session.activity || "idle") as SessionActivity,
        ended: session.lifecycle === "ended",
        failed: session.outcome === "failed",
        awaitingInput: session.awaiting_input ?? false,
        recordingMemory: session.recording_memory ?? false,
        exitReason: session.exit_reason,
        permissionMode: session.permission_mode,
        goal: session.goal ?? null,
      }));
    },
    [],
  );

  const loadSessions = useCallback(async () => {
    const previousList = sessionsRef.current;
    // One daemon, one fetch, since a folder now says where its work runs.
    let merged: SessionEntry[];
    try {
      merged = mapSessions(await fetchSessions());
    } catch (caught) {
      // A transient failure keeps what we already have rather than blanking the list.
      swallowed({ component: "workspace-page", operation: "list the sessions" }, caught);
      return;
    }
    // Reuse the previous object for any unchanged session, so an equal refetch does not re-render the list.
    const previousById = new Map(previousList.map((session) => [session.sessionId, session]));
    const mapped = merged
      .map((session) => {
        const previous = previousById.get(session.sessionId);
        return previous && JSON.stringify(previous) === JSON.stringify(session)
          ? previous
          : session;
      })
      .sort((left, right) => right.createdAt.localeCompare(left.createdAt));
    // Flag any non-active session that just went from busy to idle, computed outside the state updater so the chime plays once.
    const activeId = activeSessionIdRef.current;
    const finishedUnviewed = mapped
      .filter((session) => {
        const previous = previousById.get(session.sessionId);
        const wasBusy = !!previous && isSessionBusy(previous);
        return (
          wasBusy &&
          !isSessionBusy(session) &&
          session.sessionId !== activeId &&
          !session.awaitingInput &&
          !session.failed
        );
      })
      .map((session) => session.sessionId);
    if (finishedUnviewed.length > 0) {
      playTurnEndSound();
      setUnseenCompletions((current) => {
        const additions = finishedUnviewed.filter((id) => !current.has(id));
        if (additions.length === 0) return current;
        const next = new Set(current);
        for (const id of additions) next.add(id);
        return next;
      });
    }
    // A background session newly waiting on a decision gets the same attention cue the active session's overlay plays.
    let shouldPlayAttentionSound = false;
    for (const session of mapped) {
      const previous = previousById.get(session.sessionId);
      if (!isSessionBusy(session)) attentionPlayedForRunRef.current.delete(session.sessionId);
      if (
        session.awaitingInput &&
        !!previous &&
        !previous.awaitingInput &&
        session.sessionId !== activeId &&
        !attentionPlayedForRunRef.current.has(session.sessionId)
      ) {
        attentionPlayedForRunRef.current.add(session.sessionId);
        shouldPlayAttentionSound = true;
      }
    }
    if (shouldPlayAttentionSound) playAttentionSound();
    sessionsRef.current = mapped;
    setSessions(mapped);
    setSessionsLoaded(true);
  }, [mapSessions]);

  // Everything a lost connection took away, asked for again, so the daemon going away is recoverable without a relaunch.
  useEffect(
    () =>
      subscribeConnection((connected) => {
        setIsConnected(connected);
        if (connected) reconnectRef.current?.();
      }),
    [],
  );

  const reconnectRef = useRef<(() => void) | null>(null);
  const reconnect = useCallback(() => {
    loadAgents();
    loadAgentCards();
    loadModelCatalog();
    loadSessions();
  }, [loadAgents, loadAgentCards, loadModelCatalog, loadSessions]);
  reconnectRef.current = reconnect;

  // Coalesce the burst of events a single turn emits into one trailing refetch, so the list settles once.
  const sessionsReloadTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const scheduleSessionsReload = useCallback(() => {
    if (sessionsReloadTimerRef.current) clearTimeout(sessionsReloadTimerRef.current);
    sessionsReloadTimerRef.current = setTimeout(() => {
      void loadSessions();
    }, 350);
  }, [loadSessions]);

  useEffect(() => {
    const loadSettings = () => {
      fetchSettings()
        .then((settings) => {
          setSelectedPermissionMode(settings.permission_mode);
          setSandboxEnforceState(settings.sandbox.enforce);
          setSandboxBackend(settings.sandbox_backend);
          setWorktreeStrategy(settings.worktree_strategy);
        })
        .catch((caught) =>
          swallowed({ component: "workspace-page", operation: "list the agents" }, caught),
        );
    };
    // Arm the audio cues on the first user interaction, since browsers keep audio suspended until a gesture.
    primeSounds();
    loadSessions().catch(() => loadSessions());
    loadSettings();
    // The model catalog drives the provider and agent model pickers.
    loadModelCatalog();
    fetchRecentModels()
      .then(setRecentModels)
      .catch((caught) =>
        swallowed({ component: "workspace-page", operation: "list the agent cards" }, caught),
      );
    // Home is the default workspace for a brand-new chat, applied by the restoration effect rather than forced here.
    fetchHomeDirectory()
      .then(setHomeWorkspace)
      .catch((caught) =>
        swallowed({ component: "workspace-page", operation: "read the settings" }, caught),
      );

    // Live reload: refresh agents when they change on disk, and the session list when a title is generated.
    const unsubscribe = subscribeEvents((event) => {
      if (event.type === "agents_changed") {
        loadAgents();
        loadAgentCards();
        // A manual edit to an agent's configuration file also drives Settings, so refetch it.
        loadSettings();
      }
      if (event.type === "sessions_changed") scheduleSessionsReload();
      if (event.type === "settings_changed") {
        loadSettings();
        loadModelCatalog();
        fetchRecentModels()
          .then(setRecentModels)
          .catch((caught) =>
            swallowed({ component: "workspace-page", operation: "read the recent models" }, caught),
          );
      }
    });
    return unsubscribe;
  }, [loadSessions, scheduleSessionsReload, loadAgents, loadAgentCards, loadModelCatalog]);

  // Reload the agents and cards whenever the selected folder changes, since what is available is path-scoped.
  useEffect(() => {
    loadAgents();
    loadAgentCards();
  }, [workingDirectory, loadAgents, loadAgentCards]);

  // The working directory is bound to the active context once, without clobbering a deliberate change.
  const [restoredContext, setRestoredContext] = useState<string | null>(null);
  const contextKey = activeSessionId ?? "__new__";
  if (restoredContext !== contextKey) {
    if (activeSessionId) {
      const session = sessions.find((entry) => entry.sessionId === activeSessionId);
      if (session) {
        setRestoredContext(contextKey);
        setWorkingDirectory(session.workingDirectory || homeWorkspace?.path || "");
        setSelectedPermissionMode(session.permissionMode);
      }
    } else if (workingDirectory || homeWorkspace) {
      // A brand-new chat inherits the directory the user was just in, falling back to home only on first load.
      setRestoredContext(contextKey);
      if (!workingDirectory) {
        setWorkingDirectory(homeWorkspace?.path || "");
      }
    }
  }

  const selectedCard =
    agentCards.find((card) => card.url.endsWith(`/agents/${selectedAgent}`)) ?? null;
  const activeSession = sessions.find((entry) => entry.sessionId === activeSessionId);
  // There is one backend now, so a session is openable as soon as it is known.
  const activeSessionKnown = !activeSessionId || sessionsLoaded || !!activeSession;
  const activeSessionRunning = activeSession ? isSessionBusy(activeSession) : false;

  // The composer draft belongs to the session, so it is read from its own endpoint when one is opened.
  const [activeSessionDraft, setActiveSessionDraft] = useState("");
  // Cleared while rendering rather than in an effect, so opening a session never shows the previous draft for a frame.
  const [draftBelongsTo, setDraftBelongsTo] = useState(activeSessionId);
  if (draftBelongsTo !== activeSessionId) {
    setDraftBelongsTo(activeSessionId);
    setActiveSessionDraft("");
  }
  useEffect(() => {
    if (!activeSessionId) return;
    let cancelled = false;
    fetchSessionDraft(activeSessionId)
      .then((draft) => {
        if (!cancelled) setActiveSessionDraft(draft);
      })
      .catch((caught) =>
        swallowed(
          { component: "workspace-page", operation: "read the accessibility state" },
          caught,
        ),
      );
    return () => {
      cancelled = true;
    };
  }, [activeSessionId]);

  // Open the last conversation once, and only on a launch that arrived without a `session` in the URL.
  const restoredInitialSession = useRef(false);
  useEffect(() => {
    if (restoredInitialSession.current || !sessionsLoaded || rememberedSession === null) return;
    restoredInitialSession.current = true;
    if (activeSessionId) return;
    const candidates = sessions.filter(
      (entry) => !workspaceId || entry.workspaceId === workspaceId,
    );
    if (candidates.length === 0) return;
    const target =
      candidates.find((entry) => entry.sessionId === rememberedSession) ?? candidates[0];
    void handleResumeSession(target);
    // Redefined every render, and the ref above is what bounds this to running once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionsLoaded, sessions, workspaceId, activeSessionId, rememberedSession]);

  // Sidebar sort: newest first, or attention and running floated to the top; the filtered subset still serves the tray.
  const [sessionSort, setSessionSort] = useState<SessionSort>("recent");
  const sortedSessions = useMemo(() => {
    if (sessionSort !== "active") return sessions;
    const rank = (session: SessionEntry) =>
      session.awaitingInput ? 0 : isSessionBusy(session) ? 1 : 2;
    return [...sessions].sort(
      (left, right) => rank(left) - rank(right) || right.createdAt.localeCompare(left.createdAt),
    );
  }, [sessions, sessionSort]);
  const workspaceSessions = useMemo(
    () => sortedSessions.filter((session) => session.workspaceId === workspaceId),
    [sortedSessions, workspaceId],
  );

  // Which sidebar row is lit, and what the delegated-work panel is about.
  const rootSessionId = useMemo(
    () => rootSessionOf(sessions, activeSessionId),
    [sessions, activeSessionId],
  );

  const refreshSessions = useCallback(() => {
    loadSessions().catch((caught) =>
      swallowed({ component: "workspace-page", operation: "save the settings" }, caught),
    );
  }, [loadSessions]);

  const handleSessionCreated = useCallback(
    (sessionId: string) => {
      setActiveSessionId(sessionId);
      // A conversation you just started is the one you were last in.
      void rememberLastSession(workspaceId, sessionId);
      const params = new URLSearchParams(window.location.search);
      params.set("session", sessionId);
      router.replace(`?${params.toString()}`, { scroll: false });
      if (isCompactViewport()) setHistoryOpen(false);
      refreshSessions();
      setTimeout(refreshSessions, 5000);
    },
    [isCompactViewport, refreshSessions, router, workspaceId],
  );

  const handleStreamingChange = useCallback(
    (streaming: boolean) => {
      if (!streaming) {
        setTimeout(refreshSessions, 1000);
      }
    },
    [refreshSessions],
  );

  function handleNewChat() {
    setActiveSessionId(null);
    setChatKey((current) => current + 1);
    const params = new URLSearchParams(window.location.search);
    params.delete("session");
    router.replace(`?${params.toString()}`, { scroll: false });
    if (isCompactViewport()) setHistoryOpen(false);
  }

  // Switch the active workspace from its sidebar row, starting a fresh chat and swapping the `?workspace=` param.
  function handleSwitchWorkspace(nextWorkspaceId: string) {
    if (!nextWorkspaceId || nextWorkspaceId === workspaceId) return;
    writeLastWorkspace(nextWorkspaceId);
    setActiveSessionId(null);
    setChatKey((current) => current + 1);
    setWorkingDirectory("");
    setRestoredContext(null);
    const params = new URLSearchParams(window.location.search);
    params.set("workspace", nextWorkspaceId);
    params.delete("session");
    router.replace(`?${params.toString()}`, { scroll: false });
    if (isCompactViewport()) setHistoryOpen(false);
  }

  // Open a workspace's Settings from its sidebar menu, resetting the workspace only when it is a different one.
  function openWorkspaceSettings(nextWorkspaceId: string, section: string = "locations") {
    const switchingWorkspaces = nextWorkspaceId !== workspaceId;
    writeLastWorkspace(nextWorkspaceId);
    if (switchingWorkspaces) {
      setActiveSessionId(null);
      setChatKey((current) => current + 1);
      setWorkingDirectory("");
      setRestoredContext(null);
    }
    const params = new URLSearchParams(window.location.search);
    params.set("workspace", nextWorkspaceId);
    if (switchingWorkspaces) params.delete("session");
    params.set("settings", section);
    router.replace(`?${params.toString()}`, { scroll: false });
    if (isCompactViewport()) setHistoryOpen(false);
  }

  async function handleDeleteSession(sessionId: string) {
    const ok = await deleteSession(sessionId);
    if (ok) {
      refreshSessions();
      // Only reset the open conversation when it is the one being deleted.
      if (sessionId === activeSessionId) handleNewChat();
    }
  }

  async function handleResumeSession(entry: SessionEntry) {
    // Opening a session acknowledges its notification.
    setUnseenCompletions((current) => {
      if (!current.has(entry.sessionId)) return current;
      const next = new Set(current);
      next.delete(entry.sessionId);
      return next;
    });
    setSelectedAgent(entry.agent);
    setSelectedPermissionMode(entry.permissionMode);
    // The restoration effect rebinds the working directory to this session's own folder.
    setActiveSessionId(entry.sessionId);
    void rememberLastSession(entry.workspaceId || workspaceId, entry.sessionId);
    setChatKey((current) => current + 1);
    const params = new URLSearchParams(window.location.search);
    if (entry.workspaceId) {
      writeLastWorkspace(entry.workspaceId);
      params.set("workspace", entry.workspaceId);
      setWorkingDirectory("");
      setRestoredContext(null);
    }
    params.set("session", entry.sessionId);
    router.replace(`?${params.toString()}`, { scroll: false });
    if (isCompactViewport()) setHistoryOpen(false);
  }

  // Opening a session from the delegated-work panel, deliberately without the remount `handleResumeSession` does.
  function handleOpenDelegatedSession(entry: SessionEntry) {
    setUnseenCompletions((current) => {
      if (!current.has(entry.sessionId)) return current;
      const next = new Set(current);
      next.delete(entry.sessionId);
      return next;
    });
    setActiveSessionId(entry.sessionId);
    void rememberLastSession(entry.workspaceId || workspaceId, entry.sessionId);
    const params = new URLSearchParams(window.location.search);
    params.set("session", entry.sessionId);
    router.replace(`?${params.toString()}`, { scroll: false });
  }

  // Keep the native tray's recent list in sync and let its entries drive the app, on desktop only.
  const trayRecents = useMemo(
    () =>
      workspaceSessions.slice(0, 10).map((entry) => ({
        id: entry.sessionId,
        title: entry.title || "New conversation",
      })),
    [workspaceSessions],
  );
  useTray({
    recents: trayRecents,
    onNewChat: handleNewChat,
    onOpenSession: (sessionId) => {
      const entry = sessions.find((candidate) => candidate.sessionId === sessionId);
      if (entry) void handleResumeSession(entry);
    },
  });

  // The active agent's configured model, shown on the composer chip; changing it applies to every session running that agent.
  const agentModel = agents.find((agent) => agent.id === selectedAgent)?.model ?? "";

  function handleAgentChange(agentName: string) {
    // Switching persona continues the current conversation; only an explicit new conversation starts a fresh session.
    setSelectedAgent(agentName);
  }

  // The model chip writes through the same agent-configuration endpoint Settings uses, optimistically and then reconciled.
  async function handleAgentModelChange(modelIdentifier: string) {
    if (!selectedAgent) return;
    const [provider = "", ...modelParts] = modelIdentifier.split("/");
    const model = modelParts.join("/");
    setAgents((current) =>
      current.map((agent) =>
        agent.id === selectedAgent ? { ...agent, model: modelIdentifier } : agent,
      ),
    );
    try {
      await saveAgentConfiguration(selectedAgent, { provider, model }, workingDirectory);
      fetchRecentModels()
        .then(setRecentModels)
        .catch((caught) =>
          swallowed({ component: "workspace-page", operation: "read a workspace" }, caught),
        );
      loadAgents();
    } catch (caught) {
      // Reloading the agents puts the real value back on screen, which is the only signal the user gets.
      swallowed({ component: "workspace-page", operation: "save the agent's model" }, caught);
      loadAgents();
    }
  }

  async function handleSandboxEnforceChange(enforce: SandboxEnforce) {
    const previous = sandboxEnforceState;
    setSandboxEnforceState(enforce);
    try {
      await setSandboxEnforce(enforce);
    } catch (caught) {
      // Roll the toggle back so it reflects the daemon rather than the click.
      swallowed(
        { component: "workspace-page", operation: "change the sandbox enforcement" },
        caught,
      );
      setSandboxEnforceState(previous);
    }
  }

  async function handleWorktreeStrategyChange(strategy: "none" | "branch" | "worktree") {
    if (activeSessionId) return;
    const previous = worktreeStrategy;
    setWorktreeStrategy(strategy);
    try {
      const settings = await fetchSettings();
      await saveSettings({
        exa_api_key: settings.exa_api_key,
        composio_api_key: settings.composio_api_key,
        provider_keys: {},
        provider_base_urls: {},
        worktree_strategy: strategy,
      });
    } catch (caught) {
      swallowed({ component: "workspace-page", operation: "change the worktree strategy" }, caught);
      setWorktreeStrategy(previous);
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

  const handleHistoryResizeStart = useCallback(
    (event: PointerEvent<HTMLDivElement>) => {
      event.preventDefault();
      const startX = event.clientX;
      const startWidth = historyWidth;

      function handlePointerMove(moveEvent: globalThis.PointerEvent) {
        const nextWidth = Math.min(600, Math.max(240, startWidth + moveEvent.clientX - startX));
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
    },
    [historyWidth],
  );

  // Derive the working directory from the workspace's first local location, bouncing a dead deep link back to the home screen.
  useEffect(() => {
    if (!workspaceId) return;
    let cancelled = false;
    getWorkspace(workspaceId)
      .then((workspace) => {
        if (cancelled) return;
        if (!workspace) {
          router.replace("/");
          return;
        }
        const local = (workspace.locations ?? []).find((location) => location.kind === "local");
        setWorkingDirectory(local?.base_directory || homeWorkspace?.path || "");
      })
      .catch((caught) =>
        swallowed({ component: "workspace-page", operation: "list the sessions" }, caught),
      );
    return () => {
      cancelled = true;
    };
  }, [workspaceId, homeWorkspace, router]);

  return (
    // The chat is the base surface and only the side panels are elevated cards, each carrying its own background and inset.
    <Flex
      h="100dvh"
      minW={0}
      bg="bg"
      overflow="hidden"
      pt="var(--app-inset-top, 8px)"
      pl="var(--safe-left, 0px)"
      pr="var(--safe-right, 0px)"
      boxSizing="border-box"
    >
      <AnimatePresence initial={false}>
        {historyOpen && (
          <MotionFlex
            direction="column"
            w={{ base: "100%", md: `${historyWidth}px` }}
            maxW={{ base: "100%", md: "46vw" }}
            minW={{ base: "100%", md: "240px" }}
            ml={{ md: 2 }}
            mb={{ md: 2 }}
            // On a phone this panel is the screen, so its bottom edge is the device's.
            pb={{ base: "var(--safe-bottom, 0px)", md: 0 }}
            // `100%` rather than `100dvh`, because the parent has already reserved the top inset.
            h={{ base: "100%", md: "auto" }}
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
              right={-1}
              w={2}
              cursor="col-resize"
              zIndex={1}
              onPointerDown={handleHistoryResizeStart}
            />
            <SessionsSidebar
              sessions={sortedSessions}
              sessionsLoaded={sessionsLoaded}
              activeSessionId={rootSessionId}
              sessionSort={sessionSort}
              onSessionSortChange={setSessionSort}
              unseenCompletions={unseenCompletions}
              currentWorkspaceId={workspaceId}
              onSwitchWorkspace={handleSwitchWorkspace}
              onOpenWorkspaceSettings={openWorkspaceSettings}
              onNewChat={handleNewChat}
              onResume={(entry) => void handleResumeSession(entry)}
              onDeleteSession={(entry) => void handleDeleteSession(entry.sessionId)}
              agents={agents}
            />
          </MotionFlex>
        )}
      </AnimatePresence>

      {/* The chat is the base surface rather than a card, with overflow visible so the panels' shadows are not clipped. */}
      <Box
        flex={1}
        minW={0}
        overflow="visible"
        display={{ base: historyOpen ? "none" : "block", md: "block" }}
      >
        <ChatPanel
          key={chatKey}
          agent={selectedAgent}
          agents={agents}
          agentCard={selectedCard}
          onAgentChange={handleAgentChange}
          initialSettingsSection={settingsSectionParam ?? undefined}
          initialSessionId={activeSessionKnown ? activeSessionId : null}
          initialPermissionMode={activeSession?.permissionMode ?? selectedPermissionMode}
          sessionTitle={activeSession?.title}
          initialInputDraft={activeSessionDraft}
          onDeleteSession={activeSessionId ? handleDeleteSession : undefined}
          sessions={sessions}
          unseenCompletions={unseenCompletions}
          rootSessionId={rootSessionId}
          onResumeSession={handleOpenDelegatedSession}
          openSidePanels={openSidePanels}
          onOpenSidePanelsChange={setOpenSidePanels}
          sidePanelWidth={sidePanelWidth}
          onSidePanelWidthChange={setSidePanelWidth}
          onPermissionModeChange={setSelectedPermissionMode}
          sessionRunning={activeSessionRunning}
          initialRecordingMemory={activeSession?.recordingMemory ?? false}
          onSessionCreated={handleSessionCreated}
          onSlashCommand={handleSlashCommand}
          workingDirectory={workingDirectory}
          workspaceId={workspaceId}
          homeDirectory={homeWorkspace?.path ?? ""}
          sandboxEnforce={sandboxEnforceState}
          sandboxBackend={sandboxBackend}
          onSandboxEnforceChange={handleSandboxEnforceChange}
          worktreeStrategy={worktreeStrategy}
          onWorktreeStrategyChange={handleWorktreeStrategyChange}
          isConnected={isConnected}
          connectionLost={!isConnected}
          onReconnect={reconnect}
          onStreamingChange={handleStreamingChange}
          historyOpen={historyOpen}
          onToggleHistory={() => setHistoryOpen((current) => !current)}
          models={models}
          modelProviders={modelProviders}
          recentModels={recentModels}
          agentModel={agentModel}
          onAgentModelChange={handleAgentModelChange}
        />
      </Box>
    </Flex>
  );
}

export default function WorkspacePage() {
  return (
    <Suspense fallback={<Flex h="100dvh" />}>
      <Workspace />
    </Suspense>
  );
}
