"use client";

import { Box, Flex } from "@chakra-ui/react";
import { SessionsSidebar, type SessionEntry, type SessionSort, type SessionStatus } from "@/components/sessions-sidebar";
import { AnimatePresence, motion } from "motion/react";
import { Suspense, useCallback, useEffect, useMemo, useRef, useState, type PointerEvent } from "react";

// A Chakra Flex that is also a motion component, so the history sidebar can
// animate its open/close (opacity + slide) without losing its flex-layout props.
const MotionFlex = motion.create(Flex);
import { useRouter, useSearchParams } from "next/navigation";
import { deleteSession, fetchAccessibility, fetchAgents, fetchAgentCards, fetchHomeDirectory, fetchModels, fetchRecentModels, fetchSessionDraft, fetchSessions, fetchSettings, getProject, listProjects, saveAgentConfiguration, saveSettings, setSandboxEnabled, subscribeEvents, updateComputerControlSetting, type AgentCard, type AgentSummary, type ModelOption, type PermissionMode, type ProviderOption } from "@/lib/api";
import { ChatPanel } from "@/components/chat-panel";
import { useTray } from "@/lib/use-tray";
import { activateConnectionTarget, checkConnection, getApiBase, getLastTargetId, listConnectionTargets, LOCAL_CONNECTION_TARGET, LOCAL_TARGET_ID, resolveReachableConnectionUrl, type ConnectionTarget } from "@/lib/connection";
import { setSessionConnection } from "@/lib/connection-store";
import { playAttentionSound, playTurnEndSound, primeSounds } from "@/lib/sounds";

// SessionEntry and the sessions-sidebar UI live in the SessionsSidebar component (the
// chat history is its own unit); this page owns the data + the notification tracking.

// The last project the user was in, remembered so a fresh launch reopens it (there is no
// landing page to pick from). Best-effort localStorage — a cleared/absent value just falls
// back to the first available project.
const LAST_PROJECT_KEY = "xeac:lastProject";
function readLastProject(): string | null {
  try { return localStorage.getItem(LAST_PROJECT_KEY); } catch { return null; }
}
function writeLastProject(projectId: string): void {
  try { localStorage.setItem(LAST_PROJECT_KEY, projectId); } catch { /* ignore */ }
}


// A session whose process is still up and working. The registry reports the process's
// own lifecycle, so "busy" is exactly "not yet finished" — there is no separate per-turn
// flag to reconcile with it.
function isSessionBusy(session: SessionEntry): boolean {
  return session.status === "starting" || session.status === "running";
}

function ProjectWorkspace() {
  const router = useRouter();
  const searchParams = useSearchParams();
  // The app opens straight into a project workspace — there is no landing page. The project
  // is addressed by `?project=` (deep-linkable, static-export friendly). When none is given
  // (a fresh open), resolve one: the last project used, else the first available. The server
  // always seeds at least one project, so there is always a target. Whenever a project is
  // active, its id is remembered so the next launch reopens it.
  const projectId = searchParams.get("project") ?? "";

  // After the user grants Accessibility and the app relaunches, turn computer control on
  // automatically once. The grant flow set this flag before restarting; macOS only exposes
  // the grant to the freshly-started server, so this runs on launch (not while the previous
  // instance was live) and only when the permission is actually present.
  useEffect(() => {
    if (typeof window === "undefined" || localStorage.getItem("xeac:pendingComputerControlEnable") !== "1") return;
    let cancelled = false;
    void fetchAccessibility().then(async (granted) => {
      if (cancelled || !granted) return;
      await updateComputerControlSetting(true);
      localStorage.removeItem("xeac:pendingComputerControlEnable");
    });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (projectId) {
      writeLastProject(projectId);
      return;
    }
    let cancelled = false;
    listProjects()
      .then((projects) => {
        if (cancelled) return;
        const last = readLastProject();
        const target = last && projects.some((project) => project.id === last) ? last : projects[0]?.id;
        if (!target) return;
        const params = new URLSearchParams(window.location.search);
        params.set("project", target);
        router.replace(`?${params.toString()}`, { scroll: false });
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [projectId, router]);

  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [agentCards, setAgentCards] = useState<AgentCard[]>([]);
  const [selectedAgent, setSelectedAgent] = useState<string>("");
  const [isConnected, setIsConnected] = useState(false);
  const [currentConnection, setCurrentConnection] = useState<ConnectionTarget | null>(null);
  const connectionTargetsRef = useRef<ConnectionTarget[]>([]);
  const currentConnectionRef = useRef<ConnectionTarget | null>(null);

  const [sessions, setSessions] = useState<SessionEntry[]>([]);
  const [sessionsLoaded, setSessionsLoaded] = useState(false);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(() => searchParams.get("session"));
  // Sidebar notifications: sessions that finished a run while you weren't viewing them
  // (an "unread" completion), so the dot means "there is something new here". Detected
  // by comparing successive session snapshots; cleared when you open the session.
  const [unseenCompletions, setUnseenCompletions] = useState<Set<string>>(new Set());
  const sessionsRef = useRef<SessionEntry[]>([]);
  const attentionPlayedForRunRef = useRef<Set<string>>(new Set());
  const activeSessionIdRef = useRef<string | null>(activeSessionId);
  const [chatKey, setChatKey] = useState(0);
  // Keep the active-session id readable from callbacks (loadSessions) without adding it
  // to their deps and thrashing them on every session switch.
  useEffect(() => {
    activeSessionIdRef.current = activeSessionId;
  }, [activeSessionId]);
  // `?settings=<section>` (from the Projects home cog) opens the workspace with that
  // Settings section already showing. The param is the source of truth (no extra state);
  // once ChatPanel has opened on it, the param is dropped so a later new chat / remount
  // doesn't reopen Settings.
  const settingsSectionParam = searchParams.get("settings");
  useEffect(() => {
    if (!settingsSectionParam) return;
    const params = new URLSearchParams(window.location.search);
    params.delete("settings");
    router.replace(`?${params.toString()}`, { scroll: false });
  }, [settingsSectionParam, router]);
  // The working directory is derived from the project's first local location (for the
  // workspace/agent-resolution that still keys off a path).
  const [workingDirectory, setWorkingDirectory] = useState("");
  const [homeProject, setHomeProject] = useState<{ path: string; name: string } | null>(null);
  const [sandboxEnabledState, setSandboxEnabledState] = useState(true);
  const [workspaceStrategy, setWorkspaceStrategy] = useState<"none" | "branch" | "worktree">("none");
  const [models, setModels] = useState<ModelOption[]>([]);
  const [modelProviders, setModelProviders] = useState<ProviderOption[]>([]);
  const [recentModels, setRecentModels] = useState<{ id: string; name: string; provider: string }[]>([]);
  const [selectedPermissionMode, setSelectedPermissionMode] = useState<PermissionMode>("default");
  const [compactionKeepRecentTurns, setCompactionKeepRecentTurns] = useState(8);
  const [historyOpen, setHistoryOpen] = useState(true);
  // Default sidebar width: enough for typical session titles without eating the
  // transcript. Paired with the panel-region default in chat-panel.tsx (480) —
  // both open at their comfortable minimum and grow by drag, never the reverse.
  const [historyWidth, setHistoryWidth] = useState(268);

  const isCompactViewport = useCallback(() => {
    return window.matchMedia("(max-width: 767px)").matches;
  }, []);

  const currentConnectionId = currentConnection?.id ?? LOCAL_TARGET_ID;

  const refreshConnectionTargets = useCallback(async () => {
    const targets = await listConnectionTargets();
    connectionTargetsRef.current = targets;
    return targets;
  }, []);

  useEffect(() => {
    currentConnectionRef.current = currentConnection;
  }, [currentConnection]);

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      listConnectionTargets().catch(() => [LOCAL_CONNECTION_TARGET]),
      getLastTargetId().catch(() => null),
    ]).then(([targets, lastTarget]) => {
      if (cancelled) return;
      connectionTargetsRef.current = targets;
      const selected = targets.find((target) => target.id === lastTarget) ?? targets[0] ?? LOCAL_CONNECTION_TARGET;
      setCurrentConnection({ ...selected, url: getApiBase() || selected.url });
    });
    return () => { cancelled = true; };
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
      .then(({ agents: agentList, defaultAgent }) => {
        setAgents(agentList);
        // Keep the current selection if it's still available in this folder,
        // otherwise fall back to the server's configured default agent (and only
        // then to the first listed agent).
        setSelectedAgent((current) =>
          agentList.some((agent) => agent.id === current) ? current : (defaultAgent || agentList[0]?.id || "")
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
      .catch(() => {});
  }, []);


  const mapSessions = useCallback((serverSessions: Awaited<ReturnType<typeof fetchSessions>>, target: ConnectionTarget, apiBase: string): SessionEntry[] => {
    return serverSessions.map((session) => ({
      sessionId: session.id,
      parentSessionId: session.parent ?? "",
      projectId: session.project_id ?? "",
      connectionId: target.id,
      connectionName: target.name,
      connectionUrl: apiBase,
      connectionKind: target.kind,
      agent: session.agent,
      title: session.title,
      createdAt: session.created_at,
      workingDirectory: session.working_directory ?? "",
      status: (session.status || "starting") as SessionStatus,
      awaitingInput: session.awaiting_input ?? false,
      exitReason: session.exit_reason ?? "",
      permissionMode: session.permission_mode ?? "default",
    }));
  }, []);

  // Where a target answers, and the token that authorises talking to it. The two travel
  // together because the session list is fetched from every known daemon at once, and each
  // one holds a different secret — presenting the active connection's token to the others
  // would come back 401 and read as "that host is down".
  const sessionTargetEndpoint = useCallback(async (target: ConnectionTarget): Promise<{ url: string; token?: string } | null> => {
    const activeTarget = currentConnectionRef.current?.id === target.id;
    const url = activeTarget || target.kind === "ssh" ? await resolveReachableConnectionUrl(target) : target.url;
    const token = target.kind === "local" ? undefined : target.token ?? "";
    const ok = await checkConnection(url, { token, timeoutMs: activeTarget ? 2000 : 900 });
    return ok ? { url, token } : null;
  }, []);

  const loadSessions = useCallback(async (targetsOverride?: ConnectionTarget[]) => {
    const targets = targetsOverride ?? (connectionTargetsRef.current.length > 0 ? connectionTargetsRef.current : [currentConnectionRef.current ?? LOCAL_CONNECTION_TARGET]);
    const rows = await Promise.all(targets.map(async (target) => {
      try {
        const endpoint = await sessionTargetEndpoint(target);
        if (!endpoint) return { target, sessions: null as SessionEntry[] | null };
        const serverSessions = await fetchSessions({ apiBase: endpoint.url, token: endpoint.token });
        for (const session of serverSessions) {
          void setSessionConnection(session.id, target.id);
        }
        return { target, sessions: mapSessions(serverSessions, target, endpoint.url) };
      } catch {
        return { target, sessions: null as SessionEntry[] | null };
      }
    }));
    const previousList = sessionsRef.current;
    // A target that was transiently unreachable (a slow probe right after a send, while the
    // server is busy) returns null — keep its previous sessions rather than blanking the list.
    const merged: SessionEntry[] = [];
    for (const { target, sessions } of rows) {
      if (sessions === null) {
        merged.push(...previousList.filter((session) => session.connectionId === target.id));
      } else {
        merged.push(...sessions);
      }
    }
    // Structural sharing: reuse the previous object for any session whose data is identical, so
    // an equal refetch yields identity-stable rows and the list does not re-render or flash.
    const previousById = new Map(previousList.map((session) => [session.sessionId, session]));
    const mapped = merged
      .map((session) => {
        const previous = previousById.get(session.sessionId);
        return previous && JSON.stringify(previous) === JSON.stringify(session) ? previous : session;
      })
      .sort((left, right) => right.createdAt.localeCompare(left.createdAt));
    // Flag any non-active session that just went from busy → idle (finished a run while
    // you weren't looking) as an unread completion. Comparing against the previous
    // snapshot keeps this out of a render effect. The transition is computed outside
    // the state updater so the turn-end chime (a side effect) plays exactly once per
    // finish — the same cue the active session's own settle plays.
    const activeId = activeSessionIdRef.current;
    const finishedUnviewed = mapped
      .filter((session) => {
        const previous = previousById.get(session.sessionId);
        const wasBusy = !!previous && isSessionBusy(previous);
        return wasBusy && !isSessionBusy(session) && session.sessionId !== activeId && !session.awaitingInput && session.status !== "failed";
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
    // A background session newly waiting on a decision gets the same attention
    // cue the active session's overlay plays — the yellow dot alone is easy to
    // miss. (The active session's own prompts are handled in ChatPanel.)
    let shouldPlayAttentionSound = false;
    for (const session of mapped) {
      const previous = previousById.get(session.sessionId);
      if (!isSessionBusy(session)) attentionPlayedForRunRef.current.delete(session.sessionId);
      if (
        session.awaitingInput
        && !!previous
        && !previous.awaitingInput
        && session.sessionId !== activeId
        && !attentionPlayedForRunRef.current.has(session.sessionId)
      ) {
        attentionPlayedForRunRef.current.add(session.sessionId);
        shouldPlayAttentionSound = true;
      }
    }
    if (shouldPlayAttentionSound) playAttentionSound();
    sessionsRef.current = mapped;
    setSessions(mapped);
    setSessionsLoaded(true);
  }, [mapSessions, sessionTargetEndpoint]);

  // Coalesce the burst of sessions_changed events a single turn emits (running→true, title
  // generated, message saved, running→false) into one trailing refetch, so the session list
  // settles once instead of re-rendering three or four times in quick succession.
  const sessionsReloadTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const scheduleSessionsReload = useCallback(() => {
    if (sessionsReloadTimerRef.current) clearTimeout(sessionsReloadTimerRef.current);
    sessionsReloadTimerRef.current = setTimeout(() => { void loadSessions(); }, 350);
  }, [loadSessions]);

  useEffect(() => {
    const loadSettings = () => {
      fetchSettings()
        .then((settings) => {
          setSelectedPermissionMode(settings.permission_mode ?? "default");
          setSandboxEnabledState(settings.sandbox_enabled ?? true);
          setWorkspaceStrategy(settings.workspace_strategy ?? "none");
          setCompactionKeepRecentTurns(settings.compaction?.keep_recent_turns ?? 6);
        })
        .catch(() => {});
    };
    // Arm the audio cues on the first user interaction (browsers keep audio
    // suspended until a gesture); every later chime plays immediately.
    primeSounds();
    refreshConnectionTargets()
      .then((targets) => loadSessions(targets))
      .catch(() => loadSessions());
    loadSettings();
    // The model catalog drives the provider and agent model pickers.
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

    // Live reload: refresh agents when they change on disk, and the session list
    // when a session's (LLM-generated) title is updated.
    const unsubscribe = subscribeEvents((event) => {
      if (event.type === "agents_changed") {
        loadAgents();
        loadAgentCards();
        // A manual edit to an agent's config file (e.g. its permission mode or model)
        // also drives Settings — refetch it so the dialog reflects the on-disk change.
        loadSettings();
      }
      if (event.type === "sessions_changed") scheduleSessionsReload();
      if (event.type === "settings_changed") {
        loadSettings();
        loadModelCatalog();
        fetchRecentModels().then(setRecentModels).catch(() => {});
      }
    });
    return unsubscribe;
  }, [currentConnectionId, refreshConnectionTargets, loadSessions, scheduleSessionsReload, loadAgents, loadAgentCards, loadModelCatalog]);

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
          setSelectedPermissionMode(session.permissionMode);
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

  const selectedCard =
    agentCards.find((card) => card.url.endsWith(`/agents/${selectedAgent}`)) ?? null;
  const activeSession = sessions.find((entry) => entry.sessionId === activeSessionId);
  const activeSessionConnectionReady =
    !activeSessionId || (!sessionsLoaded && !activeSession ? false : !activeSession || activeSession.connectionId === currentConnectionId);
  const activeSessionRunning = activeSession ? isSessionBusy(activeSession) : false;

  // The composer draft belongs to the session, not to the registry listing, so it is read
  // from its own endpoint when a session is opened. The composer accepts it whenever it
  // lands, as long as the user has not started typing over it.
  const [activeSessionDraft, setActiveSessionDraft] = useState("");
  useEffect(() => {
    setActiveSessionDraft("");
    if (!activeSessionId) return;
    let cancelled = false;
    fetchSessionDraft(activeSessionId)
      .then((draft) => { if (!cancelled) setActiveSessionDraft(draft); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [activeSessionId]);

  useEffect(() => {
    if (!activeSession || activeSession.connectionId === currentConnectionId) return;
    const target: ConnectionTarget = {
      id: activeSession.connectionId,
      name: activeSession.connectionName,
      url: activeSession.connectionUrl,
      kind: activeSession.connectionKind,
    };
    activateConnectionTarget(target)
      .then(() => {
        setCurrentConnection(target);
        setChatKey((current) => current + 1);
      })
      .catch(() => {});
  }, [activeSession, currentConnectionId]);
  // Sidebar sort: "recent" (newest first, the load order) or "active" (sessions
  // needing attention or running float to the top, then newest). The sidebar groups
  // these conversations under every project; the filtered subset remains useful to
  // the native tray, which intentionally follows the current project.
  const [sessionSort, setSessionSort] = useState<SessionSort>("recent");
  const sortedSessions = useMemo(() => {
    if (sessionSort !== "active") return sessions;
    const rank = (session: SessionEntry) =>
      session.awaitingInput ? 0 : isSessionBusy(session) ? 1 : 2;
    return [...sessions].sort((left, right) => rank(left) - rank(right) || right.createdAt.localeCompare(left.createdAt));
  }, [sessions, sessionSort]);
  const projectSessions = useMemo(
    () => sortedSessions.filter((session) => session.projectId === projectId),
    [sortedSessions, projectId],
  );

  const refreshSessions = useCallback(() => {
    loadSessions()
      .catch(() => {});
  }, [loadSessions]);

  const handleSessionCreated = useCallback(
    (sessionId: string) => {
      setActiveSessionId(sessionId);
      void setSessionConnection(sessionId, currentConnectionId);
      const params = new URLSearchParams(window.location.search);
      params.set("session", sessionId);
      router.replace(`?${params.toString()}`, { scroll: false });
      if (isCompactViewport()) setHistoryOpen(false);
      refreshSessions();
      setTimeout(refreshSessions, 5000);
    },
    [currentConnectionId, isCompactViewport, refreshSessions, router]
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

  // Switch the active project from its sidebar row: remember it, start a fresh chat in
  // it, and swap the `?project=` param (which re-derives the session list, agents, and working
  // directory for that project). A no-op when it's already the current project.
  function handleSwitchProject(nextProjectId: string) {
    if (!nextProjectId || nextProjectId === projectId) return;
    writeLastProject(nextProjectId);
    setActiveSessionId(null);
    setChatKey((current) => current + 1);
    setWorkingDirectory("");
    setRestoredContext(null);
    const params = new URLSearchParams(window.location.search);
    params.set("project", nextProjectId);
    params.delete("session");
    router.replace(`?${params.toString()}`, { scroll: false });
    if (isCompactViewport()) setHistoryOpen(false);
  }

  // Open a project's real Settings dialog from its sidebar menu. Keep the current chat intact
  // when its own project is selected; only reset the workspace when settings belongs to a
  // different project. The query-param signal is consumed by ChatPanel after navigation.
  function openProjectSettings(nextProjectId: string, section: string = "locations") {
    const switchingProjects = nextProjectId !== projectId;
    writeLastProject(nextProjectId);
    if (switchingProjects) {
      setActiveSessionId(null);
      setChatKey((current) => current + 1);
      setWorkingDirectory("");
      setRestoredContext(null);
    }
    const params = new URLSearchParams(window.location.search);
    params.set("project", nextProjectId);
    if (switchingProjects) params.delete("session");
    params.set("settings", section);
    router.replace(`?${params.toString()}`, { scroll: false });
    if (isCompactViewport()) setHistoryOpen(false);
  }

  async function handleDeleteSession(sessionId: string) {
    const ok = await deleteSession(sessionId);
    if (ok) {
      refreshSessions();
      // Only reset the open conversation when it's the one being deleted; deleting a
      // background session from the sidebar must not disturb what you're looking at.
      if (sessionId === activeSessionId) handleNewChat();
    }
  }

  function handleConnectionChange(target: ConnectionTarget) {
    setCurrentConnection(target);
    setSelectedAgent("");
    setHomeProject(null);
    handleNewChat();
    void refreshConnectionTargets().then((targets) => loadSessions(targets)).catch(() => {});
  }


  async function handleResumeSession(entry: SessionEntry) {
    // Opening a session acknowledges its notification.
    setUnseenCompletions((current) => {
      if (!current.has(entry.sessionId)) return current;
      const next = new Set(current);
      next.delete(entry.sessionId);
      return next;
    });
    if (entry.connectionId !== currentConnectionId || getApiBase() !== entry.connectionUrl) {
      const target: ConnectionTarget = {
        id: entry.connectionId,
        name: entry.connectionName,
        url: entry.connectionUrl,
        kind: entry.connectionKind,
      };
      await activateConnectionTarget(target);
      setCurrentConnection(target);
    }
    setSelectedAgent(entry.agent);
    setSelectedPermissionMode(entry.permissionMode);
    // The restoration effect rebinds the working directory to this session's
    // own persisted folder; no need to set (or re-record) it here.
    setActiveSessionId(entry.sessionId);
    setChatKey((current) => current + 1);
    const params = new URLSearchParams(window.location.search);
    if (entry.projectId) {
      writeLastProject(entry.projectId);
      params.set("project", entry.projectId);
      setWorkingDirectory("");
      setRestoredContext(null);
    }
    params.set("session", entry.sessionId);
    router.replace(`?${params.toString()}`, { scroll: false });
    if (isCompactViewport()) setHistoryOpen(false);
  }

  // Keep the native menu-bar tray's recent list in sync, and let its "New Chat"
  // and recent-conversation entries drive the app (desktop only).
  const trayRecents = useMemo(
    () =>
      projectSessions.slice(0, 10).map((entry) => ({
        id: entry.sessionId,
        title: entry.title || "New conversation",
      })),
    [projectSessions]
  );
  useTray({
    recents: trayRecents,
    onNewChat: handleNewChat,
    onOpenSession: (sessionId) => {
      const entry = sessions.find((candidate) => candidate.sessionId === sessionId);
      if (entry) void handleResumeSession(entry);
    },
  });

  // The active agent's configured model identifier (provider/model), shown on the
  // composer chip and used for attachment/vision gating. The chip rewrites the
  // active agent's model via handleAgentModelChange, so a change applies to every
  // session running that agent — not just this conversation.
  const agentModel = agents.find((agent) => agent.id === selectedAgent)?.model ?? "";

  function handleAgentChange(agentName: string) {
    // Switching persona continues the current conversation — the new agent picks
    // up the same session (its system prompt is injected on top of the shared
    // history). Only an explicit "New conversation" starts a fresh session.
    setSelectedAgent(agentName);
  }

  // The composer's model chip reconfigures the active agent's model through the
  // same agent-config endpoint Settings uses. It splits the provider/model
  // identifier, persists it optimistically (so the chip updates at once), then
  // reconciles with the server's authoritative agent list and refreshes recents.
  // A change affects every session running this agent, not just the current one.
  async function handleAgentModelChange(modelIdentifier: string) {
    if (!selectedAgent) return;
    const [provider = "", ...modelParts] = modelIdentifier.split("/");
    const model = modelParts.join("/");
    setAgents((current) =>
      current.map((agent) =>
        agent.id === selectedAgent ? { ...agent, model: modelIdentifier } : agent
      )
    );
    try {
      await saveAgentConfiguration(selectedAgent, { provider, model }, workingDirectory);
      fetchRecentModels().then(setRecentModels).catch(() => {});
      loadAgents();
    } catch {
      loadAgents();
    }
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

  async function handleWorkspaceStrategyChange(strategy: "none" | "branch" | "worktree") {
    if (activeSessionId) return;
    const previous = workspaceStrategy;
    setWorkspaceStrategy(strategy);
    try {
      const settings = await fetchSettings();
      await saveSettings({
        exa_api_key: settings.exa_api_key ?? "",
        composio_api_key: settings.composio_api_key ?? "",
        provider_keys: {},
        provider_base_urls: {},
        workspace_strategy: strategy,
      });
    } catch {
      setWorkspaceStrategy(previous);
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
  }, [historyWidth]);

  // Derive the working directory from the project's first local location (the
  // workspace/agent-resolution still keys off a path). A deep-link to a project that
  // no longer exists bounces back to the Projects home rather than rendering an empty
  // workspace. A thrown request (e.g. mid connection-switch) is left alone.
  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;
    getProject(projectId).then((project) => {
      if (cancelled) return;
      if (!project) {
        router.replace("/");
        return;
      }
      const local = (project.locations ?? []).find((location) => location.kind === "local");
      setWorkingDirectory(local?.base_directory || homeProject?.path || "");
    }).catch(() => {});
    return () => { cancelled = true; };
  }, [projectId, homeProject, router]);

  return (
    // Floating-panel shell: the chat is the base surface — plain white (bg) that fills the
    // whole window; only the SIDE panels are elevated cards. The sessions sidebar here — and
    // the agents/artifacts/terminal panels inside ChatPanel — carry their own bg.panel +
    // border + shadow and inset themselves with a small margin so they read as floating above
    // the chat rather than being co-equal boxes. On white-on-white (light mode) it's the
    // shadow that makes the cards lift, so they use a soft md elevation. The top inset is the
    // titlebar height PLUS the same 8px gap the cards carry on their other three sides, so the
    // whole shell starts on one line: the chat's top bar and the floating cards' top edges land
    // together (aligned top row) while the cards keep an even gap all the way around. The
    // titlebar part (0 in a browser, 30px in the Tauri app) also clears the native traffic lights.
    <Flex
      h="100dvh"
      minW={0}
      bg="bg"
      overflow="hidden"
      pt="calc(var(--titlebar-height, 0px) + 8px)"
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
            h={{ base: "100dvh", md: "auto" }}
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
            activeSessionId={activeSessionId}
            sessionSort={sessionSort}
            onSessionSortChange={setSessionSort}
            unseenCompletions={unseenCompletions}
            currentProjectId={projectId}
            connectionId={currentConnectionId}
            onSwitchProject={handleSwitchProject}
            onOpenProjectSettings={openProjectSettings}
            onNewChat={handleNewChat}
            onResume={(entry) => void handleResumeSession(entry)}
            onDeleteSession={(entry) => void handleDeleteSession(entry.sessionId)}
          />
          </MotionFlex>
        )}
      </AnimatePresence>

      {/* The chat is the base surface, not a card — it fills the remaining space flush to
          the window edges while the side panels float above it. Overflow stays visible so the
          right-hand panels (which live inside here and sit flush to this box's top edge) can
          render their top drop-shadow up into the shell's top padding instead of having it
          clipped — mirroring the left sidebar, which floats in the non-clipping shell. */}
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
          initialSessionId={activeSessionConnectionReady ? activeSessionId : null}
          initialPermissionMode={activeSession?.permissionMode ?? selectedPermissionMode}
          sessionTitle={activeSession?.title}
          initialInputDraft={activeSessionDraft}
          onDeleteSession={activeSessionId ? handleDeleteSession : undefined}
          currentConnectionId={currentConnectionId}
          onConnectionChange={handleConnectionChange}
          onPermissionModeChange={setSelectedPermissionMode}
          sessionRunning={activeSessionRunning}
          onSessionCreated={handleSessionCreated}
          onSlashCommand={handleSlashCommand}
          workingDirectory={workingDirectory}
          projectId={projectId}
          homeDirectory={homeProject?.path ?? ""}
          sandboxEnabled={sandboxEnabledState}
          onSandboxEnabledChange={handleSandboxEnabledChange}
          workspaceStrategy={workspaceStrategy}
          onWorkspaceStrategyChange={handleWorkspaceStrategyChange}
          isConnected={isConnected && activeSessionConnectionReady}
          onStreamingChange={handleStreamingChange}
          historyOpen={historyOpen}
          onToggleHistory={() => setHistoryOpen((current) => !current)}
          models={models}
          modelProviders={modelProviders}
          recentModels={recentModels}
          agentModel={agentModel}
          onAgentModelChange={handleAgentModelChange}
          compactionKeepRecentTurns={compactionKeepRecentTurns}
        />
      </Box>
    </Flex>
  );
}

export default function ProjectWorkspacePage() {
  return (
    <Suspense fallback={<Flex h="100dvh" />}>
      <ProjectWorkspace />
    </Suspense>
  );
}
