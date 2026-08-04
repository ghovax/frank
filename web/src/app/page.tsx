"use client";

import { Box, Flex } from "@chakra-ui/react";
import { SessionsSidebar, type SessionSort } from "@/components/sessions-sidebar";
import type { SessionActivity, SessionEntry } from "@/components/session-row";
import { AnimatePresence, motion } from "motion/react";
import { Suspense, useCallback, useEffect, useMemo, useRef, useState, type PointerEvent } from "react";

// A Chakra Flex that is also a motion component, so the history sidebar can
// animate its open/close (opacity + slide) without losing its flex-layout props.
const MotionFlex = motion.create(Flex);
import { useRouter, useSearchParams } from "next/navigation";
import { deleteSession, fetchAccessibility, fetchAgents, fetchAgentCards, fetchHomeDirectory, fetchModels, fetchRecentModels, fetchSessionDraft, fetchSessions, fetchSettings, getWorkspace, listWorkspaces, rememberLastSession, saveAgentConfiguration, saveSettings, setSandboxEnforce, subscribeConnection, subscribeEvents, updateComputerControlSetting, type AgentCard, type AgentSummary, type ModelOption, type PermissionMode, type ProviderOption, type SandboxEnforce } from "@/lib/api";
import { ChatPanel, type SidePanelKey } from "@/components/chat-panel";
import { useTray } from "@/lib/use-tray";
import { playAttentionSound, playTurnEndSound, primeSounds } from "@/lib/sounds";
import { swallowed } from "@/lib/swallowed";
import { usePreferences } from "@/lib/preferences";

// SessionEntry and the sessions-sidebar UI live in the SessionsSidebar component (the
// chat history is its own unit); this page owns the data + the notification tracking.

// The last workspace the user was in, remembered so a fresh launch reopens it (there is no
// landing page to pick from) — and the last conversation within it, which the daemon keeps on
// the workspace record itself. Opening onto an empty composer is the right answer exactly once,
// the first time, when there is nothing to return to; every launch after that the thing you
// almost certainly want is the conversation you were in, and having to find it in the sidebar
// is a step that exists only because the application forgot.
//
// Both live on the daemon. Neither is a fact about a browser: the desktop app, a tab and the
// phone are three views of one daemon, and kept per-client each would reopen somewhere
// different. The phone settles it — its storage goes whenever the webview is cleared, so a
// memory that lived there would not survive the thing it exists to survive.


// The conversation a session belongs to: itself if you started it, otherwise the one at the top
// of the chain that created it. It is what the sidebar highlights and what the delegated-work
// panel is scoped to, so opening a delegated session leaves the sidebar exactly where it was —
// the conversation you picked is still the conversation you are in, you are just reading one of
// the sessions underneath it.
//
// Derived rather than stored. A second piece of state saying "which row is selected" would be a
// second answer to a question the open session already answers, free to disagree with it.
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

// A session that is actually working. The daemon derives this for us now: `activity` is
// "working" only while a turn is in flight, where the old process-lifecycle field reported
// every live session as busy forever, because a session outlives its turns.
function isSessionBusy(session: SessionEntry): boolean {
  return session.activity === "working";
}

function Workspace() {
  const router = useRouter();
  const searchParams = useSearchParams();
  // Where this interface opens and what it looks like, as the daemon has it. `readLastWorkspace`
  // and `writeLastWorkspace` used to be two functions over `localStorage`; they are a field and
  // a write now, and the phone agrees with this window about both.
  const { preferences, updatePreferences } = usePreferences();
  const readLastWorkspace = () => preferences.last_workspace_id;
  const writeLastWorkspace = (workspaceId: string) => updatePreferences({ last_workspace_id: workspaceId });
  // The app opens straight into a workspace workspace — there is no landing page. The workspace
  // is addressed by `?workspace=` (deep-linkable, static-export friendly). When none is given
  // (a fresh open), resolve one: the last workspace used, else the first available. The server
  // always seeds at least one workspace, so there is always a target. Whenever a workspace is
  // active, its id is remembered so the next launch reopens it.
  const workspaceId = searchParams.get("workspace") ?? "";

  // After the user grants Accessibility and the app relaunches, turn computer control on
  // automatically once. The grant flow set this flag before restarting; macOS only exposes
  // the grant to the freshly-started server, so this runs on launch (not while the previous
  // instance was live) and only when the permission is actually present.
  useEffect(() => {
    if (!preferences.computer_control_awaiting_grant) return;
    let cancelled = false;
    void fetchAccessibility().then(async (granted) => {
      if (cancelled || !granted) return;
      await updateComputerControlSetting(true);
      updatePreferences({ computer_control_awaiting_grant: false });
    });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [preferences.computer_control_awaiting_grant]);

  // Which conversation the daemon last saw this workspace opened at: `null` until it has said,
  // then an id or `""` for none. The restore below waits for it rather than racing it — arriving
  // late and switching the conversation out from under someone is worse than opening a moment
  // later on the right one.
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
        const target = last && workspaces.some((workspace) => workspace.id === last) ? last : workspaces[0]?.id;
        // Nothing to open into — release the restore below, which is waiting on a workspace that
        // is never going to arrive, so it can settle on the empty composer rather than on nothing.
        if (!target) { setRememberedSession(""); return; }
        const params = new URLSearchParams(window.location.search);
        params.set("workspace", target);
        router.replace(`?${params.toString()}`, { scroll: false });
      })
      .catch((caught) => swallowed({ component: "workspace-page", operation: "read the home directory" }, caught));
    return () => { cancelled = true; };
    // `readLastWorkspace`/`writeLastWorkspace` close over the preferences and are rebuilt every
    // render; naming them here would re-run this on every render rather than when the workspace
    // it is resolving actually changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceId, router]);

  useEffect(() => {
    // No workspace yet means the effect above is still choosing one; it will set this itself if
    // it turns out there is none to choose.
    if (!workspaceId) return;
    let cancelled = false;
    getWorkspace(workspaceId)
      .then((workspace) => { if (!cancelled) setRememberedSession(workspace?.last_session_id ?? ""); })
      .catch((caught) => {
        // A workspace that would not load is not a reason to hang on the loading screen; the
        // empty composer is a fine place to land.
        if (!cancelled) setRememberedSession("");
        swallowed({ component: "workspace-page", operation: "read the last conversation" }, caught);
      });
    return () => { cancelled = true; };
  }, [workspaceId]);

  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [agentCards, setAgentCards] = useState<AgentCard[]>([]);
  const [selectedAgent, setSelectedAgent] = useState<string>("");
  const [isConnected, setIsConnected] = useState(false);

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
  // `?settings=<section>` (from the Workspaces home cog) opens the workspace with that
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
  // The working directory is derived from the workspace's first local location (for the
  // workspace/agent-resolution that still keys off a path).
  const [workingDirectory, setWorkingDirectory] = useState("");
  const [homeWorkspace, setHomeWorkspace] = useState<{ path: string; name: string } | null>(null);
  const [sandboxEnforceState, setSandboxEnforceState] = useState<SandboxEnforce>("required");
  const [sandboxBackend, setSandboxBackend] = useState({ backend: "", detail: "" });
  const [worktreeStrategy, setWorktreeStrategy] = useState<"none" | "branch" | "worktree">("none");
  const [models, setModels] = useState<ModelOption[]>([]);
  const [modelProviders, setModelProviders] = useState<ProviderOption[]>([]);
  const [recentModels, setRecentModels] = useState<{ id: string; name: string; provider: string }[]>([]);
  const [selectedPermissionMode, setSelectedPermissionMode] = useState<PermissionMode>("default");
  const [compactionReclaimAtFraction, setCompactionReclaimAtFraction] = useState(0.85);
  const [historyOpen, setHistoryOpen] = useState(true);
  // The right-hand panels, held here rather than inside ChatPanel because that component is
  // remounted on every conversation switch. Opening a delegated session from its panel used to
  // close the panel it was opened from; now the transcript changes and nothing else does.
  const [openSidePanels, setOpenSidePanels] = useState<SidePanelKey[]>([]);
  // Default right-region width: comfortable for one panel without dwarfing the transcript
  // (pairs with the sidebar default below). Drag grows it.
  const [sidePanelWidth, setSidePanelWidth] = useState(480);
  // Default sidebar width: enough for typical session titles without eating the
  // transcript. Paired with the panel-region default in chat-panel.tsx (480) —
  // both open at their comfortable minimum and grow by drag, never the reverse.
  const [historyWidth, setHistoryWidth] = useState(268);

  const isCompactViewport = useCallback(() => {
    return window.matchMedia("(max-width: 767px)").matches;
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
    fetchAgentCards(workingDirectoryRef.current).then(setAgentCards).catch((caught) => swallowed({ component: "workspace-page", operation: "list the workspaces" }, caught));
  }, []);
  const loadAgents = useCallback(() => {
    fetchAgents(workingDirectoryRef.current)
      .then((agentList) => {
        setAgents(agentList);
        // Keep the current selection when the folder still offers it, otherwise take the first
        // agent on offer. Selecting nothing was the honest answer — no profile is configured as
        // the default, so any pick is this code's rather than the user's — but it left a fresh
        // connection unable to send at all, which is a worse kind of dishonesty: the box accepts
        // a message and silently drops it. The picker is one click away and shows what is set.
        setSelectedAgent((current) =>
          agentList.some((agent) => agent.id === current) ? current : agentList[0]?.id ?? ""
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
      .catch((caught) => swallowed({ component: "workspace-page", operation: "list the models" }, caught));
  }, []);


  const mapSessions = useCallback((serverSessions: Awaited<ReturnType<typeof fetchSessions>>): SessionEntry[] => {
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
      exitReason: session.exit_reason,
      permissionMode: session.permission_mode,
    }));
  }, []);

  const loadSessions = useCallback(async () => {
    const previousList = sessionsRef.current;
    // One daemon, one fetch. This used to fan out across every saved backend and merge the
    // answers, because a session could live on any of them; a folder now says where its work
    // runs, so there is one place to ask.
    let merged: SessionEntry[];
    try {
      merged = mapSessions(await fetchSessions());
    } catch (caught) {
      // A transient failure (a slow probe right after a send, while the server is busy) keeps
      // what we already have rather than blanking the list.
      swallowed({ component: "workspace-page", operation: "list the sessions" }, caught);
      return;
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
    // Flag any non-active session that just went from busy to idle (finished a run while
    // you weren't looking) as an unread completion. Comparing against the previous
    // snapshot keeps this out of a render effect. The transition is computed outside
    // the state updater so the turn-end chime (a side effect) plays exactly once per
    // finish — the same cue the active session's own settle plays.
    const activeId = activeSessionIdRef.current;
    const finishedUnviewed = mapped
      .filter((session) => {
        const previous = previousById.get(session.sessionId);
        const wasBusy = !!previous && isSessionBusy(previous);
        return wasBusy && !isSessionBusy(session) && session.sessionId !== activeId && !session.awaitingInput && !session.failed;
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
  }, [mapSessions]);

  // Everything a lost connection took away, asked for again.
  //
  // `isConnected` had exactly one writer and no reader that could do anything about it: the
  // daemon going away disabled the composer and left the transcript blank, with nothing on
  // screen saying why and no way to try again short of relaunching. A flag that reports a
  // failure without offering the remedy is half a feature.
  // The daemon's reachability, from the one thing already watching it continuously. A failed
  // `fetchAgents` used to be the only writer, so a daemon that died after a successful start
  // was never noticed at all — the composer simply stopped working. The stream drops the moment
  // the daemon does, and reconnects by itself, so this both raises the notice and clears it.
  useEffect(() => subscribeConnection((connected) => {
    setIsConnected(connected);
    if (connected) reconnectRef.current?.();
  }), []);

  const reconnectRef = useRef<(() => void) | null>(null);
  const reconnect = useCallback(() => {
    loadAgents();
    loadAgentCards();
    loadModelCatalog();
    loadSessions();
  }, [loadAgents, loadAgentCards, loadModelCatalog, loadSessions]);
  reconnectRef.current = reconnect;

  // Coalesce the burst of sessions_changed events a single turn emits (running true, title
  // generated, message saved, running false) into one trailing refetch, so the session list
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
          setSelectedPermissionMode(settings.permission_mode);
          setSandboxEnforceState(settings.sandbox.enforce);
          setSandboxBackend(settings.sandbox_backend);
          setWorktreeStrategy(settings.worktree_strategy);
          setCompactionReclaimAtFraction(settings.compaction?.reclaim_at_fraction ?? 0.85);
        })
        .catch((caught) => swallowed({ component: "workspace-page", operation: "list the agents" }, caught));
    };
    // Arm the audio cues on the first user interaction (browsers keep audio
    // suspended until a gesture); every later chime plays immediately.
    primeSounds();
    loadSessions()
      .catch(() => loadSessions());
    loadSettings();
    // The model catalog drives the provider and agent model pickers.
    loadModelCatalog();
    fetchRecentModels()
      .then(setRecentModels)
      .catch((caught) => swallowed({ component: "workspace-page", operation: "list the agent cards" }, caught));
    // Home is the default workspace for a brand-new chat; the restoration effect
    // below applies it (or the active session's own folder) — we don't force it
    // here, or it would clobber a session opened directly via ?session=.
    fetchHomeDirectory()
      .then(setHomeWorkspace)
      .catch((caught) => swallowed({ component: "workspace-page", operation: "read the settings" }, caught));

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
        fetchRecentModels().then(setRecentModels).catch((caught) => swallowed({ component: "workspace-page", operation: "read the recent models" }, caught));
      }
    });
    return unsubscribe;
  }, [loadSessions, scheduleSessionsReload, loadAgents, loadAgentCards, loadModelCatalog]);

  // Reload the agents and their cards whenever the selected folder changes (and
  // on first render): the available agents, skills and MCP servers are all
  // path-scoped, so picking a different workspace must re-derive what's actually
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
          setWorkingDirectory(session.workingDirectory || homeWorkspace?.path || "");
          setSelectedPermissionMode(session.permissionMode);
        }
    } else if (workingDirectory || homeWorkspace) {
      // A brand-new chat inherits the working directory the user was just in —
      // no jarring folder reset when starting a new conversation. Only fall back
      // to home if there's no directory yet (first load).
      setRestoredContext(contextKey);
      if (!workingDirectory) {
        setWorkingDirectory(homeWorkspace?.path || "");
      }
    }
  }

  const selectedCard =
    agentCards.find((card) => card.url.endsWith(`/agents/${selectedAgent}`)) ?? null;
  const activeSession = sessions.find((entry) => entry.sessionId === activeSessionId);
  // A session used to have to wait for its backend to be activated before it could be
  // opened. There is one backend now, so a session is openable as soon as it is known.
  const activeSessionKnown = !activeSessionId || sessionsLoaded || !!activeSession;
  const activeSessionRunning = activeSession ? isSessionBusy(activeSession) : false;

  // The composer draft belongs to the session, not to the registry listing, so it is read
  // from its own endpoint when a session is opened. The composer accepts it whenever it
  // lands, as long as the user has not started typing over it.
  const [activeSessionDraft, setActiveSessionDraft] = useState("");
  // Cleared while rendering rather than in the effect below, which is React's own answer to
  // "reset state when something changes": an effect runs after the paint, so opening a
  // session would show the previous session's draft in its composer for a frame first.
  const [draftBelongsTo, setDraftBelongsTo] = useState(activeSessionId);
  if (draftBelongsTo !== activeSessionId) {
    setDraftBelongsTo(activeSessionId);
    setActiveSessionDraft("");
  }
  useEffect(() => {
    if (!activeSessionId) return;
    let cancelled = false;
    fetchSessionDraft(activeSessionId)
      .then((draft) => { if (!cancelled) setActiveSessionDraft(draft); })
      .catch((caught) => swallowed({ component: "workspace-page", operation: "read the accessibility state" }, caught));
    return () => { cancelled = true; };
  }, [activeSessionId]);

  // Open the last conversation when nothing else says which to open.
  //
  // Once, and only on a launch that arrived without a `session` in the URL — a deep link says
  // which conversation it means, and a person who has just pressed "New conversation" is asking
  // for the empty composer, not to be sent back where they came from. The ref is what keeps this
  // to the first opportunity rather than every time the session list refreshes.
  //
  // The remembered one if it still exists, and otherwise the most recent, which is what the list
  // is already sorted by. Both can miss — a machine you have never opened here has neither — and
  // that lands on the empty composer, which is the right answer the first time.
  const restoredInitialSession = useRef(false);
  useEffect(() => {
    if (restoredInitialSession.current || !sessionsLoaded || rememberedSession === null) return;
    restoredInitialSession.current = true;
    if (activeSessionId) return;
    const candidates = sessions.filter((entry) => !workspaceId || entry.workspaceId === workspaceId);
    if (candidates.length === 0) return;
    const target = candidates.find((entry) => entry.sessionId === rememberedSession) ?? candidates[0];
    void handleResumeSession(target);
    // `handleResumeSession` is redefined every render and is not a dependency anything wants to
    // re-run on; the ref above is what bounds this to once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionsLoaded, sessions, workspaceId, activeSessionId, rememberedSession]);

  // Sidebar sort: "recent" (newest first, the load order) or "active" (sessions
  // needing attention or running float to the top, then newest). The sidebar groups
  // these conversations under every workspace; the filtered subset remains useful to
  // the native tray, which intentionally follows the current workspace.
  const [sessionSort, setSessionSort] = useState<SessionSort>("recent");
  const sortedSessions = useMemo(() => {
    if (sessionSort !== "active") return sessions;
    const rank = (session: SessionEntry) =>
      session.awaitingInput ? 0 : isSessionBusy(session) ? 1 : 2;
    return [...sessions].sort((left, right) => rank(left) - rank(right) || right.createdAt.localeCompare(left.createdAt));
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
    loadSessions()
      .catch((caught) => swallowed({ component: "workspace-page", operation: "save the settings" }, caught));
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
    [isCompactViewport, refreshSessions, router, workspaceId]
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

  // Switch the active workspace from its sidebar row: remember it, start a fresh chat in
  // it, and swap the `?workspace=` param (which re-derives the session list, agents, and working
  // directory for that workspace). A no-op when it's already the current workspace.
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

  // Open a workspace's real Settings dialog from its sidebar menu. Keep the current chat intact
  // when its own workspace is selected; only reset the workspace when settings belongs to a
  // different workspace. The query-param signal is consumed by ChatPanel after navigation.
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
      // Only reset the open conversation when it's the one being deleted; deleting a
      // background session from the sidebar must not disturb what you're looking at.
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
    // The restoration effect rebinds the working directory to this session's
    // own persisted folder; no need to set (or re-record) it here.
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

  // Opening a session from the delegated-work panel.
  //
  // Deliberately not `handleResumeSession`. That one is the sidebar's: it remounts the chat (a
  // fresh `chatKey`), which is right when you are moving between conversations you started and
  // wrong here — the remount tears down the panel region and rebuilds it, and the panel you
  // clicked flashes as it goes. `useChat` follows `initialSessionId` on its own, so changing
  // which session is open is enough. The workspace does not change either: a delegated session
  // is inside the workspace you are already in.
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

  // Keep the native menu-bar tray's recent list in sync, and let its "New Chat"
  // and recent-conversation entries drive the app (desktop only).
  const trayRecents = useMemo(
    () =>
      workspaceSessions.slice(0, 10).map((entry) => ({
        id: entry.sessionId,
        title: entry.title || "New conversation",
      })),
    [workspaceSessions]
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
      fetchRecentModels().then(setRecentModels).catch((caught) => swallowed({ component: "workspace-page", operation: "read a workspace" }, caught));
      loadAgents();
    } catch (caught) {
      // The model the user picked did not save. Reloading the agents puts the real value back
      // on screen, so the change appears to undo itself — which is the only signal they get,
      // and says nothing about why.
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
      // Roll the toggle back so it reflects the daemon rather than the click. A sandbox
      // setting that silently refuses to change is worth a record.
      swallowed({ component: "workspace-page", operation: "change the sandbox enforcement" }, caught);
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

  // Derive the working directory from the workspace's first local location (the
  // workspace/agent-resolution still keys off a path). A deep-link to a workspace that
  // no longer exists bounces back to the Workspaces home rather than rendering an empty
  // workspace. A thrown request (e.g. mid connection-switch) is left alone.
  useEffect(() => {
    if (!workspaceId) return;
    let cancelled = false;
    getWorkspace(workspaceId).then((workspace) => {
      if (cancelled) return;
      if (!workspace) {
        router.replace("/");
        return;
      }
      const local = (workspace.locations ?? []).find((location) => location.kind === "local");
      setWorkingDirectory(local?.base_directory || homeWorkspace?.path || "");
    }).catch((caught) => swallowed({ component: "workspace-page", operation: "list the sessions" }, caught));
    return () => { cancelled = true; };
  }, [workspaceId, homeWorkspace, router]);

  return (
    // Floating-panel shell: the chat is the base surface — plain white (bg) that fills the
    // whole window; only the SIDE panels are elevated cards. The sessions sidebar here — and
    // the agents/terminal panels inside ChatPanel — carry their own bg.panel +
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
            // On a phone this panel *is* the screen, so its bottom edge is the device's — the
            // last conversation in the list would otherwise sit under the home indicator.
            pb={{ base: "var(--safe-bottom, 0px)", md: 0 }}
            // `100%` rather than `100dvh`: the parent has already reserved the top
            // inset, so a full-viewport child overflows the screen by exactly that much.
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
          isConnected={isConnected && activeSessionKnown}
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
          compactionReclaimAtFraction={compactionReclaimAtFraction}
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
