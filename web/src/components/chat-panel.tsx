"use client";

import {
  Box,
  Button,
  EmptyState,
  Flex,
  Heading,
  IconButton,
  Menu,
  Separator,
  Span,
  Text,
  VStack,
} from "@chakra-ui/react";
import { LuAppWindow, LuArrowDown, LuChevronLeft, LuChevronRight, LuClock, LuDownload, LuEllipsis, LuFile, LuFolderOpen, LuHistory, LuMaximize2, LuMinimize2, LuMessageSquare, LuMousePointerClick, LuNavigation, LuNetwork, LuPanelLeftClose, LuPanelLeftOpen, LuRotateCcw, LuRotateCw, LuSettings, LuTerminal, LuTrash2, LuTriangleAlert, LuX } from "react-icons/lu";
import { AnimatePresence, motion } from "motion/react";
import { useFormatter, useTranslations } from "next-intl";
import { toaster } from "@/components/ui/toaster";
import { PanelTiles, type TilePanel } from "./panel-tiles";
import { useColorMode } from "./ui/color-mode";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type PointerEvent } from "react";
import { useChat, type ChatMessage } from "@/lib/use-chat";
import { ChatMessageItem, ChatToolGroup } from "./chat-message";
import { VersionBadge } from "./attachment-chips";
import { InlineField } from "./ui/display";
import { PanelTab } from "./ui/panel-tab";
import { PanelCard, PanelHeader, PanelEmptyState, TOP_BAR_HEIGHT } from "@/components/ui/panel";
import { SegmentedToggle } from "@/components/ui/segmented-toggle";
import { extractToolArtifacts, externalArtifactUrl, ArtifactView } from "./tool-views";
import { NativeWebview } from "./native-webview";
import { nativeWebviewAvailable } from "@/lib/native-artifact";
import { ArtifactEventProvider, type ArtifactEvent } from "./artifact-bridge";
import { ChatInput } from "./chat-input";
import { QuestionOverlay } from "./question-overlay";
import { SettingsDialog, type SettingsSection } from "./settings-dialog";
import { BackgroundTasksPanel } from "./background-tasks-panel";
import { GitStatusBar } from "./git-status-bar";
import { LocationChip } from "./location-status";
import { useDirectoryStatus } from "@/lib/use-directory-status";
import { Tooltip } from "./ui/tooltip";
import { ToolbarAction } from "@/components/ui/toolbar";
import { DropdownMenu } from "@/components/ui/menu";
import { PermissionOverlay } from "./permission-overlay";
import { AgentSkills } from "./agent-skills";
import { getToolCallDisplay, type ToolDisplayTranslator } from "@/lib/tool-display";
import type { ToolPermission, ToolQuestion } from "@/lib/tool-event";

import { artifactBytesUrl, artifactPageUrl, deleteArtifactAnnotations, fetchArtifactAnnotations, fetchArtifacts, fetchArtifactDiff, fetchArtifactVersions, getProject, restoreArtifact, fetchSettings, saveArtifactAnnotations, saveSessionDraft, saveSettings, subscribeEvents, revealInFinder, type AgentCard, type AgentSummary, type ArtifactIndexEntry, type ArtifactScope, type ArtifactSurface, type ArtifactVersion, type Location, type PermissionMode, type WorkspaceStrategy } from "@/lib/api";
import { PdfDocumentView } from "./pdf-view";
import { imageIdentityForArtifact, type ArtifactAnnotationRecord, type ArtifactImageAnnotation, type ArtifactImageIdentity } from "@/lib/artifact-annotations";
import type { ConnectionTarget } from "@/lib/connection";
import { scrollFade, scrollFadeTopBottom } from "@/lib/scroll-fade";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { playAttentionSound, playTurnEndSound } from "@/lib/sounds";
import { closePermissionNotification, notifyPermissionRequest, setPermissionNotificationHandler } from "@/lib/notify";
import { ArtifactHistoryList } from "./artifact-history";

// A Chakra Box that is also a motion component, so the right panel region can
// animate its open/close (opacity + slide) exactly like the history sidebar on
// the left — without losing its flex-layout props.
const MotionBox = motion.create(Box);

type SidePanelKey = "artifact" | "background";

const MAXIMUM_OPEN_SIDE_PANELS = 2;


interface ChatPanelProps {
  agent: string;
  agents: AgentSummary[];
  agentCard?: AgentCard | null;
  onAgentChange: (agent: string) => void;
  // When set, the workspace opens with the Settings dialog already showing this section
  // (the Projects-home cog links here via `?settings=…`).
  initialSettingsSection?: string;
  initialSessionId: string | null;
  // The session's display title (LLM-generated once the conversation has one),
  // shown in the top bar. Absent until the session names itself.
  sessionTitle?: string;
  initialInputDraft?: string;
  // Deletes the session by id (aborts it, drops its tasks and record, then routes
  // the user back to a blank chat). Absent when there is no active session.
  onDeleteSession?: (sessionId: string) => void;
  initialPermissionMode?: PermissionMode;
  currentConnectionId?: string;
  onConnectionChange?: (target: ConnectionTarget) => void;
  onPermissionModeChange?: (mode: PermissionMode) => void;
  sessionRunning?: boolean;
  onSessionCreated: (sessionId: string) => void;
  onSlashCommand?: (command: string) => void;
  workingDirectory?: string;
  projectId?: string;
  homeDirectory?: string;
  sandboxEnabled?: boolean;
  onSandboxEnabledChange?: (enabled: boolean) => void;
  workspaceStrategy?: WorkspaceStrategy;
  onWorkspaceStrategyChange?: (strategy: WorkspaceStrategy) => void | Promise<void>;
  isConnected?: boolean;
  onStreamingChange?: (isStreaming: boolean) => void;
  historyOpen?: boolean;
  onToggleHistory?: () => void;
  models?: { id: string; name: string; provider: string; available: boolean }[];
  modelProviders?: { id: string; name: string; openai_compatible: boolean }[];
  recentModels?: { id: string; name: string; provider: string }[];
  agentModel?: string;
  onAgentModelChange: (modelIdentifier: string) => void | Promise<void>;
  compactionKeepRecentTurns: number;
}

type TimelineItem =
  | { kind: "message"; message: ChatMessage }
  // A tool_group with no messages is a reasoning ("thinking") phase. `thinkingTurns`
  // records whether reasoning exists so a standalone Thinking row can be retained.
  | { kind: "tool_group"; id: string; messages: ChatMessage[]; thinkingTurns: number };

// One versioned state of an artifact (the filmstrip walks these in sequence order).
// Version identity is the git commit sha; bytes are the git blob sha at that commit.
type ArtifactVersionEntry = {
  versionId: string; // the commit sha — the stable version identity
  commitSha: string;
  blobSha: string;
  sequence: number;
  changeType: "A" | "M" | "D";
  size: number;
  createdAt: string;
  annotationCount: number;
};

// A logical artifact and everything needed to render, version, and restore it. Tabs are
// keyed by `artifactId`; a file in History mode that was never surfaced as a tab gets a
// synthetic id from its (gitDirectory, relativePath). The git coordinates are what the
// version/diff/bytes/restore endpoints address.
type ArtifactGroup = {
  id: string; // artifactId, or `${gitDirectory}::${relativePath}` for a history-only file
  artifactId: string;
  kind: "image" | "html" | "iframe" | "file";
  title: string;
  source: string; // the original path/URL the artifact points at (shown to the user)
  gitDirectory: string;
  relativePath: string;
  absolutePath: string;
  locationUri: string;
  workTree: string;
  toolCallId: string;
  latestCommit: string;
  latestBlob: string;
};

// The artifact the open_artifact tool result surfaces in the transcript — enough to
// open (and immediately render) a tab before the backend surface/index catches up.
type TranscriptArtifact = {
  artifactId: string;
  kind: "image" | "html" | "iframe" | "file";
  title: string;
  source: string;
  location: string;
  absolutePath: string;
  toolCallId: string;
};

function versionEntryFromWire(version: ArtifactVersion): ArtifactVersionEntry {
  return {
    versionId: version.versionId,
    commitSha: version.commitSha,
    blobSha: version.blobSha,
    sequence: version.sequence,
    changeType: version.changeType,
    size: version.size,
    createdAt: version.createdAt,
    annotationCount: version.annotationCount,
  };
}

function groupFromSurface(surface: ArtifactSurface): ArtifactGroup {
  return {
    id: surface.artifactId,
    artifactId: surface.artifactId,
    kind: surface.kind,
    title: surface.title,
    source: surface.source,
    gitDirectory: surface.gitDirectory,
    relativePath: surface.relativePath,
    absolutePath: surface.absolutePath,
    locationUri: surface.locationUri,
    workTree: surface.workTree,
    toolCallId: surface.toolCallId,
    latestCommit: surface.latestCommit,
    latestBlob: surface.latestBlob,
  };
}

function groupFromIndexEntry(entry: ArtifactIndexEntry): ArtifactGroup {
  const id = entry.artifactId || `${entry.gitDirectory}::${entry.relativePath}`;
  return {
    id,
    artifactId: entry.artifactId || id,
    // The index only distinguishes image/html/file; anything non-image/html is a file.
    kind: entry.kind === "image" ? "image" : entry.kind === "html" ? "html" : "file",
    title: entry.title || entry.relativePath || "Artifact",
    source: entry.relativePath,
    gitDirectory: entry.gitDirectory,
    relativePath: entry.relativePath,
    absolutePath: entry.absolutePath,
    locationUri: entry.locationUri,
    workTree: entry.workTree,
    toolCallId: "",
    latestCommit: entry.latestCommit,
    latestBlob: entry.latestBlob,
  };
}

function folderDisplayName(workingDirectory?: string): string {
  const directory = (workingDirectory ?? "").trim();
  if (!directory) return "";
  return directory.split(/[\\/]/).filter(Boolean).at(-1) ?? directory;
}

// The open_artifact tool result carries a `{ type:"artifact", kind, artifact_id, ... }`
// record. Collect one TranscriptArtifact per open_artifact call in the transcript, so a
// tab can open (and render immediately) from the transcript before the backend surface
// arrives. Keyed by artifactId, newest occurrence winning.
function transcriptArtifactsFromMessages(messages: ChatMessage[]): Map<string, TranscriptArtifact> {
  const collected = new Map<string, TranscriptArtifact>();
  for (const message of messages) {
    if (message.role !== "tool_call" || message.content !== "open_artifact") continue;
    const toolCallId = String(message.meta?.toolCallId ?? "");
    const result = message.meta?.result;
    const resultContent = result == null ? null : typeof result === "string" ? result : JSON.stringify(result);
    if (resultContent == null) continue;
    for (const artifact of extractToolArtifacts("open_artifact", resultContent)) {
      const artifactId = String(artifact.artifact_id ?? "");
      if (!artifactId) continue;
      const kind = String(artifact.type ?? "").toLowerCase();
      if (kind !== "image" && kind !== "html" && kind !== "iframe" && kind !== "file") continue;
      collected.set(artifactId, {
        artifactId,
        kind,
        title: String(artifact.title ?? ""),
        source: String(artifact.source ?? ""),
        location: String(artifact.location ?? ""),
        absolutePath: String(artifact.absolute_path ?? ""),
        toolCallId,
      });
    }
  }
  return collected;
}

function groupFromTranscript(artifact: TranscriptArtifact): ArtifactGroup {
  return {
    id: artifact.artifactId,
    artifactId: artifact.artifactId,
    kind: artifact.kind,
    title: artifact.title,
    source: artifact.source,
    gitDirectory: "",
    relativePath: "",
    absolutePath: artifact.absolutePath,
    locationUri: artifact.location,
    workTree: "",
    toolCallId: artifact.toolCallId,
    latestCommit: "",
    latestBlob: "",
  };
}

function timelineItems(messages: ChatMessage[]): TimelineItem[] {
  const items: TimelineItem[] = [];
  let index = 0;
  // The first reasoning phase seen since the last non-thinking, non-tool row. It
  // belongs to the tool batch it leads into: its id keys the group so the
  // tools-less "thinking" heading and the tool group it becomes are the SAME
  // element — the tools stream into the existing row instead of one row being
  // swapped for another (which would flash a remount). A prose or user row that
  // isn't a tool group discards it.
  let pendingThinkingId: string | null = null;
  // Count reasoning messages so a tools-less Thinking group can be distinguished from
  // an empty group.
  let pendingThinkingTurns = 0;
  while (index < messages.length) {
    const message = messages[index];
    if (message.role === "thinking") {
      pendingThinkingId ??= message.id;
      pendingThinkingTurns += 1;
      index += 1;
      continue;
    }
    if (message.role === "assistant" && !message.content.trim()) {
      index += 1;
      continue;
    }
    if (message.role !== "tool_call") {
      if (pendingThinkingId) {
        items.push({ kind: "tool_group", id: pendingThinkingId, messages: [], thinkingTurns: pendingThinkingTurns });
      }
      items.push({ kind: "message", message });
      pendingThinkingId = null;
      pendingThinkingTurns = 0;
      index += 1;
      continue;
    }

    const toolMessages: ChatMessage[] = [];
    // The leading reasoning that led into this batch keys the group (stable from
    // the pre-tool "thinking" heading onward). Reasoning phases are tallied from the
    // leading ones plus any interleaved between this group's calls.
    const groupKey = pendingThinkingId;
    let thinkingTurns = pendingThinkingTurns;
    pendingThinkingId = null;
    pendingThinkingTurns = 0;
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
        thinkingTurns += 1;
        index += 1;
      } else {
        break;
      }
    }
    items.push({
      kind: "tool_group",
      // Prefer the leading thinking id so the key is stable across the
      // thinking→tools transition; fall back to the first tool otherwise.
      id: groupKey ?? toolMessages[0].id,
      messages: toolMessages,
      thinkingTurns,
    });
  }
  // A reasoning phase at the tail surfaces as the live "Thinking" status line.
  // The item is emitted here unconditionally; ToolGroup renders it only while
  // the turn is live (keepOpen), so settled reasoning leaves no row behind.
  if (pendingThinkingId) {
    items.push({ kind: "tool_group", id: pendingThinkingId, messages: [], thinkingTurns: pendingThinkingTurns });
  }
  return items;
}

export function ChatPanel({
  agent,
  agents,
  agentCard,
  onAgentChange,
  initialSettingsSection,
  initialSessionId,
  sessionTitle,
  initialInputDraft = "",
  onDeleteSession,
  initialPermissionMode = "default",
  currentConnectionId,
  onConnectionChange,
  onPermissionModeChange,
  sessionRunning = false,
  onSessionCreated,
  workingDirectory,
  projectId = "",
  homeDirectory,
  sandboxEnabled = true,
  onSandboxEnabledChange,
  workspaceStrategy = "none",
  onWorkspaceStrategyChange,
  isConnected = false,
  onStreamingChange,
  historyOpen = false,
  onToggleHistory,
  models = [],
  modelProviders = [],
  recentModels = [],
  agentModel = "",
  onAgentModelChange,
  compactionKeepRecentTurns,
}: ChatPanelProps) {
  const translation = useTranslations("ChatPanel");
  const tToolDisplay = useTranslations("ToolDisplay") as unknown as ToolDisplayTranslator;
  const format = useFormatter();
  const [permissionMode, setPermissionModeState] = useState<PermissionMode>(initialPermissionMode);
  const { messages, tokenUsage, queuedMessages, sessionId, isStreaming, isHistoryLoading, historyError, reloadHistory, send, sendArtifactEvent, abort, dequeueMessage, handlePermission, handleQuestion, declineQuestion, compact } =
    useChat(agent, initialSessionId, workingDirectory, workspaceStrategy, permissionMode, sessionRunning, projectId);

  // Single source of truth for the working directory's validity and Git status —
  // consumed by the workspace status bar (branch/dirty/ahead-behind) and passed to the
  // composer as `directoryValid` for its send-gate.
  const { status: directoryStatus, directoryValid } = useDirectoryStatus(workingDirectory);
  // Whether the chat body has resolved enough to render without flashing: connected,
  // and the working directory's validity determined (not mid-check). Until then we show
  // a neutral placeholder instead of the empty welcome and a disabled→enabled input, so
  // opening a project doesn't flicker.
  const trimmedWorkingDirectory = (workingDirectory ?? "").trim();
  const directoryPending = !!trimmedWorkingDirectory && (directoryStatus.checking || directoryStatus.path !== trimmedWorkingDirectory);
  const chatReady = isConnected && !directoryPending;

  // The project's locations, for the terminal location picker (and any location-aware
  // panels). Refreshed live when the project config changes.
  const [projectLocations, setProjectLocations] = useState<Location[]>([]);
  useEffect(() => {
    let cancelled = false;
    // Resolving through a promise (even for the empty-project case) keeps the state
    // update off the synchronous effect path, so an empty project clears locations
    // on the next microtask rather than mid-render.
    const load = () => {
      const request = projectId ? getProject(projectId) : Promise.resolve(null);
      request.then((project) => { if (!cancelled) setProjectLocations(project?.locations ?? []); }).catch(() => {});
    };
    load();
    const unsubscribe = subscribeEvents((event) => { if (event.type === "projects_changed") load(); });
    return () => { cancelled = true; unsubscribe(); };
  }, [projectId]);

  // On mount, fetch the stored permission mode from the server settings. This
  // overrides the "default" fallback when no session is active, so the user's
  // last choice persists across page reloads and new sessions.
  useEffect(() => {
    if (initialSessionId) return;
    let cancelled = false;
    fetchSettings().then((settings) => {
      if (cancelled || settings.permission_mode === permissionMode) return;
      setPermissionModeState(settings.permission_mode);
    }).catch(() => {});
    return () => { cancelled = true; };
  // Only run when there is no session — once the session is set, the session's own
  // permission_mode is authoritative.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialSessionId]);

  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const scrollContentRef = useRef<HTMLDivElement>(null);
  // "Following" the bottom. Released the moment the user scrolls up and resumed
  // only when they return to the bottom — so auto-scroll never grabs them.
  const isPinnedRef = useRef(true);
  const lastScrollTopRef = useRef(0);
  const onStreamingChangeRef = useRef(onStreamingChange);
  // Snapshot of the previous layout pass. Comparing the first key and the count
  // tells a *top* prepend (an older page loading in) from a *bottom* append (a new
  // turn), so the former can preserve the reader's exact viewport instead of
  // jumping. Seeded on the first non-loading pass.
  const scrollMetricsRef = useRef({ scrollHeight: 0, firstKey: "", count: 0 });
  const notifiedSessionIdRef = useRef<string | null>(null);
  // Whether the turn is currently paused on a pending decision (a permission or
  // question prompt on a tool call). Read via a ref inside handleSend so a new
  // message is queued rather than steered while a decision is outstanding.
  const hasInputRequiredRef = useRef(false);
  const [openSidePanels, setOpenSidePanels] = useState<SidePanelKey[]>([]);
  const artifactPanelOpen = openSidePanels.includes("artifact");
  const backgroundPanelOpen = openSidePanels.includes("background");
  // Default right-region width: comfortable for one panel without dwarfing the
  // transcript (pairs with the sidebar default of 240 in page.tsx). Drag grows it.
  const [artifactPanelWidth, setArtifactPanelWidth] = useState(480);
  // Tabs are keyed by artifactId. A tab opens from the transcript (an open_artifact
  // result) or from History mode (selecting a changed file); its versions and bytes come
  // from the backend endpoints, refreshed live on the `artifact_captured` broadcast.
  const [openArtifactIds, setOpenArtifactIds] = useState<string[]>([]);
  const [activeTabId, setActiveTabId] = useState<string | null>(null);
  // The version the user is inspecting per artifact (defaults to newest). Lets the
  // filmstrip hold an older frame without losing the newest.
  const [selectedVersionByArtifact, setSelectedVersionByArtifact] = useState<Record<string, string>>({});
  // The transcript artifactIds already auto-opened, so a brand-new open_artifact result
  // opens (and activates) its tab exactly once without re-grabbing it on every render.
  const [seenTranscriptIds, setSeenTranscriptIds] = useState<Set<string>>(() => new Set());
  const [artifactReloadKey, setArtifactReloadKey] = useState(0);
  const [artifactMaximized, setArtifactMaximized] = useState(false);
  // The backend surfaces (opened-tab artifacts) and the file-history index (every changed
  // file, for History mode), refetched on session load and on each artifact_captured
  // broadcast for this session. `artifactScope` drives the ?scope query for both.
  const [artifactSurfaces, setArtifactSurfaces] = useState<ArtifactSurface[]>([]);
  const [artifactIndex, setArtifactIndex] = useState<ArtifactIndexEntry[]>([]);
  const [artifactScope, setArtifactScope] = useState<ArtifactScope>("session");
  // Versions per artifact group id (from GET .../artifacts/versions), ordered by sequence.
  const [versionsByArtifact, setVersionsByArtifact] = useState<Record<string, ArtifactVersionEntry[]>>({});
  // Groups opened from History mode (files that were never surfaced as a tab) live here,
  // since they are not in the surfaces list — merged with the surface/transcript groups.
  const [historyGroupsById, setHistoryGroupsById] = useState<Record<string, ArtifactGroup>>({});
  // History mode replaces the tab body with the changed-file list; selecting a file opens
  // it in the same tab+filmstrip flow (with a diff view and a restore action).
  const [historyMode, setHistoryMode] = useState(false);
  const [artifactVersionsReloadKey, setArtifactVersionsReloadKey] = useState(0);
  const [annotationRecords, setAnnotationRecords] = useState<Record<string, ArtifactAnnotationRecord>>({});
  // Annotation image keys (artifactId::versionId) that have already been sent with a
  // message. They stay in `annotationRecords` — so they remain visible on that version in
  // the panel — but drop out of the composer's pending set, so a sent annotation is not
  // re-attached to the next message. Re-annotating a sent version clears its key again.
  const [sentAnnotationKeys, setSentAnnotationKeys] = useState<Set<string>>(() => new Set());
  const annotationChangeSequenceRef = useRef(0);
  const { colorMode } = useColorMode();
  // Whether the transcript is scrolled to (or near) the bottom. Drives the floating
  // "jump to latest" affordance so a reader who scrolled up to read history can
  // return to the live tail in one click instead of scrolling all the way down.
  const [isAtBottom, setIsAtBottom] = useState(true);
  // A strict at-bottom flag (small threshold) that drives the transcript's bottom fade:
  // the content fades above the composer only while there is more below the fold, and the
  // fade lifts the moment the reader reaches the bottom so the last line is never dimmed.
  const [transcriptPinned, setTranscriptPinned] = useState(true);
  // Top-bar surfaces: the settings dialog, the delete-session confirmation, and the
  // background-processes sheet all open from the persistent bar above the transcript.
  // Open Settings on mount when the workspace was entered with `?settings=<section>`.
  const validInitialSection: SettingsSection | null =
    initialSettingsSection === "general" || initialSettingsSection === "locations"
      || initialSettingsSection === "agents" || initialSettingsSection === "connection"
      ? initialSettingsSection : null;
  const [settingsOpen, setSettingsOpen] = useState(!!validInitialSection);
  const [settingsSection, setSettingsSection] = useState<SettingsSection>(validInitialSection ?? "general");
  const [appliedInitialSettingsSection, setAppliedInitialSettingsSection] = useState(validInitialSection);
  if (appliedInitialSettingsSection !== validInitialSection) {
    setAppliedInitialSettingsSection(validInitialSection);
    if (validInitialSection) {
      setSettingsSection(validInitialSection);
      setSettingsOpen(true);
    }
  }
  const [deleteConfirmOpen, setDeleteConfirmOpen] = useState(false);

  const setSidePanelOpen = useCallback((panel: SidePanelKey, open: boolean) => {
    setOpenSidePanels((currentPanels) => {
      const remainingPanels = currentPanels.filter((openPanel) => openPanel !== panel);
      if (!open) return remainingPanels;
      return [...remainingPanels, panel].slice(-MAXIMUM_OPEN_SIDE_PANELS);
    });
  }, []);

  useEffect(() => {
    if (openSidePanels.length === 0 || !historyOpen || !window.matchMedia("(max-width: 1199px)").matches) return;
    onToggleHistory?.();
  }, [openSidePanels.length, historyOpen, onToggleHistory]);

  const markSidePanelActive = useCallback((panel: SidePanelKey) => {
    setOpenSidePanels((currentPanels) => {
      if (!currentPanels.includes(panel) || currentPanels[currentPanels.length - 1] === panel) return currentPanels;
      return [...currentPanels.filter((openPanel) => openPanel !== panel), panel];
    });
  }, []);

  useEffect(() => {
    if (!sessionId) return;
    let cancelled = false;
    const changeSequence = annotationChangeSequenceRef.current;
    fetchArtifactAnnotations(sessionId)
      .then((records) => {
        if (cancelled || annotationChangeSequenceRef.current !== changeSequence) return;
        setAnnotationRecords(Object.fromEntries(records.map((record) => [record.image.key, record])));
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  // Refetch the artifact surfaces + file-history index on session load, on a scope
  // change, and on every artifact_captured broadcast for this session (capture is
  // asynchronous/background, so the panel can't be built from the transcript alone). An
  // artifact_annotations_changed broadcast re-pulls the annotations the same way.
  useEffect(() => {
    if (!sessionId) return;
    let cancelled = false;
    const load = () => {
      fetchArtifacts(sessionId, artifactScope)
        .then((result) => {
          if (cancelled) return;
          setArtifactSurfaces(result.surfaces);
          setArtifactIndex(result.artifacts);
        })
        .catch(() => {});
    };
    load();
    const unsubscribe = subscribeEvents((event) => {
      const eventSessionId = (event as { session_id?: string }).session_id;
      if (eventSessionId && eventSessionId !== sessionId) return;
      if (event.type === "artifact_captured") {
        load();
        // New bytes may have landed for an open tab — force its versions to refetch.
        setArtifactVersionsReloadKey((current) => current + 1);
      } else if (event.type === "artifact_annotations_changed") {
        // Merge-add only: our own saves fire this broadcast, and overwriting would clobber
        // the optimistic record the user is actively editing (the backend serves a
        // different, live source for the image) — which made the chip flash between
        // versions on every keystroke. Records we don't already hold (e.g. from another
        // window) are still picked up.
        fetchArtifactAnnotations(sessionId)
          .then((records) => {
            if (cancelled) return;
            setAnnotationRecords((current) => {
              const next = { ...current };
              for (const record of records) {
                if (!next[record.image.key]) next[record.image.key] = record;
              }
              return next;
            });
          })
          .catch(() => {});
      }
    });
    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, [sessionId, artifactScope]);

  // Pinned == the viewport is at (or within a hair of) the bottom. That single
  // fact drives everything: pinned means follow new content, unpinned means the
  // reader has scrolled up to read history and must never be pulled back down.
  const handleScroll = useCallback(() => {
    const container = scrollContainerRef.current;
    if (!container) return;
    const distanceFromBottom = container.scrollHeight - (container.scrollTop + container.clientHeight);
    const atBottom = distanceFromBottom <= 8;
    isPinnedRef.current = atBottom;
    setTranscriptPinned(atBottom);
    // A larger threshold for showing the jump button than for "pinned": the button
    // should not flash for a hair of scroll, but pinning must stay strict so the
    // live tail never fights a reader who nudged up a pixel.
    setIsAtBottom(distanceFromBottom <= 120);
    lastScrollTopRef.current = container.scrollTop;
  }, []);

  useEffect(() => {
    onStreamingChangeRef.current = onStreamingChange;
  }, [onStreamingChange]);

  const scrollToBottom = useCallback(() => {
    isPinnedRef.current = true;
    setIsAtBottom(true);
    const container = scrollContainerRef.current;
    if (!container) return;
    container.scrollTop = container.scrollHeight;
    lastScrollTopRef.current = container.scrollTop;
    scrollMetricsRef.current.scrollHeight = container.scrollHeight;
  }, []);

  const handleSend = useCallback((text: string, dataParts?: Record<string, unknown>[]) => {
    // Which agent runs is never assumed. A session cannot be created without one, and there
    // is no default to reach for, so an unchosen agent is asked for rather than guessed at.
    if (!agent && !initialSessionId) {
      toaster.create({
        type: "error",
        title: translation("chooseAnAgent"),
        description: translation("chooseAnAgentDescription"),
        closable: true,
      });
      return undefined;
    }
    scrollToBottom();
    // Queue (never steer) while a decision prompt is outstanding — see hasInputRequiredRef.
    const result = send(text, dataParts, hasInputRequiredRef.current);
    // Once the send has gone through, mark any annotations it carried as sent, so they
    // clear from the composer's pending strip (they stay in annotationRecords, so they
    // remain visible on that version in the panel). Done after send() so a synchronous
    // failure doesn't retire an annotation that never left.
    const sentKeys = (dataParts ?? [])
      .filter((part) => part.kind === "artifact_image_annotations")
      .map((part) => String((part.image as Record<string, unknown> | undefined)?.key ?? ""))
      .filter(Boolean);
    if (sentKeys.length > 0) {
      setSentAnnotationKeys((current) => {
        const next = new Set(current);
        for (const key of sentKeys) next.add(key);
        return next;
      });
    }
    scrollToBottom();
    return result;
  }, [agent, initialSessionId, scrollToBottom, send, translation]);

  const openSettings = useCallback((section: SettingsSection) => {
    setSettingsSection(section);
    setSettingsOpen(true);
  }, [setSettingsOpen, setSettingsSection]);

  // A rendered artifact posted an interaction back to the agent. It travels as a
  // structured turn (a typed DataPart), so just forward it intact.
  const handleArtifactEvent = useCallback((artifactEvent: ArtifactEvent) => {
    scrollToBottom();
    sendArtifactEvent(artifactEvent);
    scrollToBottom();
  }, [scrollToBottom, sendArtifactEvent]);

  useEffect(() => {
    if (!sessionId || notifiedSessionIdRef.current === sessionId) return;
    notifiedSessionIdRef.current = sessionId;
    onSessionCreated(sessionId);
  }, [sessionId, onSessionCreated]);

  // The single owner of scroll position. It runs before paint on every content
  // change and classifies it: a *top* prepend (an older page loading in) preserves
  // the reader's exact viewport by the height delta; everything else (a new turn,
  // streamed text) follows the bottom, but only while pinned. This one effect
  // replaces the old anchor + rAF tangle that fought itself and yanked the reader.
  useLayoutEffect(() => {
    const container = scrollContainerRef.current;
    if (!container || isHistoryLoading) return;
    const firstKey = messages.length > 0 ? messages[0].id : "";
    const count = messages.length;
    const previous = scrollMetricsRef.current;
    const prepended = count > previous.count && firstKey !== previous.firstKey && previous.count > 0;
    if (prepended) {
      // Older messages landed above the viewport — shift down by exactly how much
      // taller the content got, so what the reader is looking at stays fixed (and a
      // bottom-pinned view stays pinned). This is what makes background paging
      // invisible.
      const delta = container.scrollHeight - previous.scrollHeight;
      if (delta !== 0) container.scrollTop = container.scrollTop + delta;
    } else if (isPinnedRef.current) {
      container.scrollTop = container.scrollHeight;
    }
    scrollMetricsRef.current = { scrollHeight: container.scrollHeight, firstKey, count };
    lastScrollTopRef.current = container.scrollTop;
  }, [messages, queuedMessages, isHistoryLoading]);

  // Late-growing content (images, artifacts, streamed text) keeps the bottom in view
  // — but only while pinned, so a reader who scrolled up is never dragged down.
  // Re-runs when the transcript container actually mounts (it is absent during the
  // loading/empty states), otherwise the observer would never attach.
  const timelineMounted = !isHistoryLoading && !historyError && messages.length > 0;
  useEffect(() => {
    const content = scrollContentRef.current;
    const container = scrollContainerRef.current;
    if (!content || !container) return;
    const observer = new ResizeObserver(() => {
      if (!isPinnedRef.current) return;
      container.scrollTop = container.scrollHeight;
      lastScrollTopRef.current = container.scrollTop;
      scrollMetricsRef.current.scrollHeight = container.scrollHeight;
    });
    observer.observe(content);
    return () => observer.disconnect();
  }, [timelineMounted]);

  // Reset the artifact panel when the session changes, so a freshly loaded transcript
  // seeds its own tabs rather than inheriting the previous conversation's. Render-phase
  // prop-change adjustment (the same pattern as previousActiveSteps below) rather than an
  // effect, to stay lint-clean.
  const [previousInitialSession, setPreviousInitialSession] = useState({ id: initialSessionId, permissionMode: initialPermissionMode });
  if (initialSessionId !== previousInitialSession.id || initialPermissionMode !== previousInitialSession.permissionMode) {
    const sessionChanged = initialSessionId !== previousInitialSession.id;
    setPreviousInitialSession({ id: initialSessionId, permissionMode: initialPermissionMode });
    setPermissionModeState(initialPermissionMode);
    if (sessionChanged) {
      setSeenTranscriptIds(new Set());
      setSelectedVersionByArtifact({});
      setOpenArtifactIds([]);
      setActiveTabId(null);
      setArtifactSurfaces([]);
      setArtifactIndex([]);
      setVersionsByArtifact({});
      setHistoryGroupsById({});
      setHistoryMode(false);
      setAnnotationRecords({});
      setSentAnnotationKeys(new Set());
    }
  }

  useLayoutEffect(() => {
    scrollMetricsRef.current = { scrollHeight: 0, firstKey: "", count: 0 };
    isPinnedRef.current = true;
  }, [initialSessionId]);

  // New content is followed by the layout effect above (only while pinned); this
  // surfaces the streaming flag to the parent and plays the turn-end chime on the
  // live→settled transition (never on mount, so loading a finished session is
  // silent). The initial jump-to-bottom is also handled there: pinned starts
  // true, so the first post-load pass lands at the bottom instantly.
  const wasStreamingRef = useRef(false);
  useEffect(() => {
    onStreamingChangeRef.current?.(isStreaming);
    if (wasStreamingRef.current && !isStreaming) playTurnEndSound();
    wasStreamingRef.current = isStreaming;
  }, [isStreaming]);

  // The mode is fixed when the session is created, so this only ever configures the
  // *next* session: it moves the local choice and persists it as the stored default.
  // A live session's mode is immutable, and the controls that call this are read-only
  // once one exists.
  function handlePermissionModeChange(nextMode: PermissionMode) {
    if (sessionId) return;
    setPermissionModeState(nextMode);
    onPermissionModeChange?.(nextMode);
    // Persist to server settings so it survives across sessions.
    saveSettings({ permission_mode: nextMode }).catch(() => {});
  }

  const handleInputDraftChange = useCallback((nextDraft: string) => {
    if (!sessionId) return;
    saveSessionDraft(sessionId, nextDraft).catch(() => {});
  }, [sessionId]);

  const currentFolderName = folderDisplayName(workingDirectory) || translation("thisFolder");
  const renderedTimeline = useMemo(() => timelineItems(messages), [messages]);
  // Entrance animation is reserved for rows a *live turn* just appended at the
  // bottom — never the initial load or a background history prepend, which arrive
  // in bulk (and, for prepends, above the fold). The rule is purely positional and
  // so immune to state-flag timing: a row animates only if its key has never been
  // seen AND every row after it is also unseen (i.e. it is part of the trailing run
  // of brand-new rows at the end of the list). The first population seeds the set
  // with everything, so nothing animates on load. `enteredKeysRef` persists across
  // renders; the panel remounts per session (page.tsx keys it), so it resets then.
  const timelineKeys = renderedTimeline.map((item) => (item.kind === "tool_group" ? item.id : item.message.id));
  const timelineSessionKey = initialSessionId ?? "__new__";
  const [timelineAnimationState, setTimelineAnimationState] = useState<{
    sessionKey: string;
    keys: string[];
    seen: string[];
    animated: string[];
  }>(() => ({ sessionKey: timelineSessionKey, keys: [], seen: [], animated: [] }));
  let animatedKeys = new Set(timelineAnimationState.animated);
  const timelineKeysChanged =
    timelineAnimationState.sessionKey !== timelineSessionKey ||
    timelineAnimationState.keys.length !== timelineKeys.length ||
    timelineAnimationState.keys.some((key, index) => key !== timelineKeys[index]);
  if (timelineKeysChanged) {
    const previousSeen = timelineAnimationState.sessionKey === timelineSessionKey
      ? new Set(timelineAnimationState.seen)
      : new Set<string>();
    const nextAnimated = new Set<string>();
    if (previousSeen.size > 0) {
      for (let index = timelineKeys.length - 1; index >= 0; index -= 1) {
        if (previousSeen.has(timelineKeys[index])) break;
        nextAnimated.add(timelineKeys[index]);
      }
    }
    const nextSeen = Array.from(new Set([...previousSeen, ...timelineKeys]));
    setTimelineAnimationState({
      sessionKey: timelineSessionKey,
      keys: timelineKeys,
      seen: nextSeen,
      animated: Array.from(nextAnimated),
    });
    animatedKeys = nextAnimated;
  }
  // Running shell commands drive the badge on the top-bar background-processes
  // button, so a long-running bash call is visible without opening the sheet.
  const runningShellCount = messages.filter((message) =>
    message.role === "tool_call" && message.content === "bash" &&
    (message.meta?.status === "running" || message.meta?.status === "input_required")
  ).length;
  // A compaction pass is live while its timeline marker is still "running" — drives
  // the Compact control's in-progress state (spinner + disabled).
  const isCompacting = messages.some(
    (message) => message.role === "compaction" && message.meta?.status === "running"
  );
  // "Reveal" opens the session's working directory in the OS file manager.
  const revealPath = (workingDirectory || "").trim();

  // The open_artifact results in the transcript, keyed by artifactId — the signal for
  // which tabs to open (and what to render immediately, before the backend surface lands).
  const transcriptArtifacts = useMemo(() => transcriptArtifactsFromMessages(messages), [messages]);

  // Every artifact the panel knows about, keyed by group id. Surfaces (from the backend)
  // are authoritative for the git coordinates that address versions/bytes/diff/restore;
  // the transcript fills a tab in immediately; History-opened files add their own groups.
  const groupsById = useMemo(() => {
    const groups = new Map<string, ArtifactGroup>();
    for (const [artifactId, transcriptArtifact] of transcriptArtifacts) {
      groups.set(artifactId, groupFromTranscript(transcriptArtifact));
    }
    for (const surface of artifactSurfaces) {
      groups.set(surface.artifactId, groupFromSurface(surface));
    }
    for (const [id, group] of Object.entries(historyGroupsById)) {
      if (!groups.has(id)) groups.set(id, group);
    }
    return groups;
  }, [transcriptArtifacts, artifactSurfaces, historyGroupsById]);
  const groupsByIdRef = useRef(groupsById);
  useEffect(() => {
    groupsByIdRef.current = groupsById;
  }, [groupsById]);

  const openGroups = useMemo(
    () => openArtifactIds
      .map((id) => groupsById.get(id))
      .filter((group): group is ArtifactGroup => group !== undefined),
    [openArtifactIds, groupsById],
  );

  // Load each open tab's versions from the backend (capture is asynchronous, so versions
  // never come from the transcript). Re-runs when the open set or git coordinates change,
  // the scope switches, or an artifact_captured broadcast bumps the reload key.
  const versionsRequestKey = openArtifactIds
    .map((id) => {
      const group = groupsById.get(id);
      return group ? `${group.id}|${group.gitDirectory}|${group.relativePath}` : id;
    })
    .join(",");
  useEffect(() => {
    if (!sessionId) return;
    let cancelled = false;
    const groupsToLoad = openArtifactIds
      .map((id) => groupsByIdRef.current.get(id))
      .filter((group): group is ArtifactGroup => Boolean(group?.relativePath));
    Promise.all(
      groupsToLoad.map(async (group) => {
        const versions = await fetchArtifactVersions(sessionId, group.gitDirectory, group.relativePath, artifactScope);
        const entries = versions.map(versionEntryFromWire).sort((left, right) => left.sequence - right.sequence);
        return [group.id, entries] as const;
      }),
    )
      .then((loaded) => {
        if (cancelled) return;
        setVersionsByArtifact((current) => {
          const next = { ...current };
          for (const [id, entries] of loaded) next[id] = entries;
          return next;
        });
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  // groupsById is read through a ref to keep the dependency list on stable primitives.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, versionsRequestKey, artifactScope, artifactVersionsReloadKey]);

  const activeGroup = activeTabId ? groupsById.get(activeTabId) ?? null : null;
  const activeVersions = (activeGroup && versionsByArtifact[activeGroup.id]) ?? [];
  const latestVersionId = activeVersions.length > 0 ? activeVersions[activeVersions.length - 1].versionId : "";
  const selectedVersionId =
    (activeGroup && selectedVersionByArtifact[activeGroup.id]) || latestVersionId;
  const activeVersionEntry =
    activeVersions.find((version) => version.versionId === selectedVersionId) ??
    (activeVersions.length > 0 ? activeVersions[activeVersions.length - 1] : null);
  // The 1-based number of the version currently shown, badged next to the artifact title.
  const selectedVersionNumber = activeVersions.findIndex((version) => version.versionId === selectedVersionId) + 1;
  // Only the newest version accepts new pins; older frames are read-only history.
  const isViewingLatestVersion = !activeVersionEntry || selectedVersionId === latestVersionId;

  // Many versions crowd the timeline, so we only ever render a sliding window of at most
  // five nodes centered on the selected one; stepping or scrubbing past an edge slides the
  // window so the rest stay reachable via the prev/next controls. selectedVersionNumber is
  // 1-based (0 when the selection isn't found, which we treat as the newest).
  const VERSION_WINDOW_SIZE = 5;
  const selectedVersionIndex = selectedVersionNumber > 0 ? selectedVersionNumber - 1 : activeVersions.length - 1;
  const versionWindowStart = activeVersions.length <= VERSION_WINDOW_SIZE
    ? 0
    : Math.min(
        Math.max(0, selectedVersionIndex - Math.floor(VERSION_WINDOW_SIZE / 2)),
        activeVersions.length - VERSION_WINDOW_SIZE,
      );
  const visibleVersions = activeVersions.slice(versionWindowStart, versionWindowStart + VERSION_WINDOW_SIZE);

  // The concrete artifact record ArtifactView renders for the active version: an image
  // streams its version's blob bytes; an html file is served live (runtime-injected) at
  // its absolute path; an external page is proxied (or handed to the native webview).
  const activeArtifact = useMemo<Record<string, unknown> | null>(() => {
    if (!activeGroup || !sessionId) return null;
    const blobSha = activeVersionEntry?.blobSha || activeGroup.latestBlob;
    const commitSha = activeVersionEntry?.commitSha || activeGroup.latestCommit;
    if (activeGroup.kind === "image") {
      // Prefer the selected version's blob bytes (so the filmstrip can step through
      // history); fall back to serving the file live through /artifact-page — the same
      // reliable path HTML uses. The blob URL is empty until the first version is
      // captured (capture is async), so without the `file` fallback a freshly-opened
      // image would have no renderable source and show "nothing to preview".
      const versionedSrc = blobSha && activeGroup.gitDirectory !== undefined
        ? artifactBytesUrl({ location: activeGroup.locationUri, gitDirectory: activeGroup.gitDirectory, sha: blobSha, session: sessionId })
        : "";
      return {
        type: "image",
        title: activeGroup.title,
        src: versionedSrc,
        file: activeGroup.absolutePath,
        location: activeGroup.locationUri,
        session: sessionId,
        artifact_id: activeGroup.artifactId,
        version_id: selectedVersionId || commitSha,
        version_seq: selectedVersionNumber,
        name: activeGroup.relativePath.split("/").pop() || activeGroup.title,
      };
    }
    if (activeGroup.kind === "html") {
      // The file may live on a remote location; carry its location + session so the
      // backend serves it (and its assets) through that location's executor, live.
      return { type: "iframe", title: activeGroup.title, file: activeGroup.absolutePath, version: commitSha, location: activeGroup.locationUri, session: sessionId };
    }
    if (activeGroup.kind === "iframe") {
      return { type: "iframe", title: activeGroup.title, src: activeGroup.source };
    }
    return null;
  }, [activeGroup, activeVersionEntry, selectedVersionId, selectedVersionNumber, sessionId]);

  // Version-timeline navigation. Selecting a version anywhere from the track (click or
  // drag) and stepping with the arrow keys while the panel is hovered all funnel
  // through these; kept as refs so the one global key listener always sees the latest.
  const timelineDotsRef = useRef<HTMLDivElement | null>(null);
  const timelineDraggingRef = useRef(false);
  const artifactHoveredRef = useRef(false);
  const selectVersion = (versionId: string) => {
    if (activeGroup && versionId && versionId !== selectedVersionId) {
      setSelectedVersionByArtifact((current) => ({ ...current, [activeGroup.id]: versionId }));
    }
  };
  const stepVersion = (direction: number) => {
    if (activeVersions.length < 2) return;
    const currentIndex = activeVersions.findIndex((version) => version.versionId === selectedVersionId);
    const target = activeVersions[Math.min(activeVersions.length - 1, Math.max(0, currentIndex + direction))];
    if (target) selectVersion(target.versionId);
  };
  // Map a pointer x-position on the track to the nearest version node (click-anywhere + drag).
  const selectVersionAtClientX = (clientX: number) => {
    const dots = timelineDotsRef.current;
    if (!dots || activeVersions.length < 2) return;
    const rect = dots.getBoundingClientRect();
    if (rect.width <= 0) return;
    const ratio = Math.min(1, Math.max(0, (clientX - rect.left) / rect.width));
    // The track only renders the visible window, so map the pointer across those nodes.
    const target = visibleVersions[Math.round(ratio * (visibleVersions.length - 1))];
    if (target) selectVersion(target.versionId);
  };
  const stepVersionRef = useRef(stepVersion);
  useEffect(() => { stepVersionRef.current = stepVersion; });
  // Arrow keys step versions while the mouse is over the artifacts panel — but never while
  // the user is typing (an input/textarea has focus), so the composer keeps its cursor.
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (!artifactHoveredRef.current) return;
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      const active = document.activeElement as HTMLElement | null;
      if (active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA" || active.isContentEditable)) return;
      event.preventDefault();
      stepVersionRef.current(event.key === "ArrowRight" ? 1 : -1);
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);
  const activeImageIdentity = useMemo(
    () => (activeArtifact ? imageIdentityForArtifact(activeArtifact) : null),
    [activeArtifact],
  );
  const activeAnnotationRecord = activeImageIdentity ? annotationRecords[activeImageIdentity.key] : undefined;
  const activeAnnotations = activeAnnotationRecord?.annotations ?? [];
  // A version accepts new pins while it's the latest OR while it still carries unsent
  // feedback you're working on — so a newer version landing mid-annotation never turns
  // your in-progress notes read-only under you.
  const activeVersionHasPendingAnnotations = Boolean(
    activeImageIdentity &&
    (annotationRecords[activeImageIdentity.key]?.annotations.length ?? 0) > 0 &&
    !sentAnnotationKeys.has(activeImageIdentity.key),
  );
  // Every open image artifact's unsent annotations ride along as context on the next
  // message — not just the active tab — so feedback drawn on several images is all sent
  // together. Feedback stays pinned to the exact version it was drawn on: if a newer
  // version is captured in the background before you send, your notes still ride along
  // referencing the version you marked (rather than being dropped for not being latest).
  const pendingAnnotationRecords = useMemo(() => {
    const records: ArtifactAnnotationRecord[] = [];
    for (const group of openGroups) {
      if (group.kind !== "image") continue;
      const versions = versionsByArtifact[group.id] ?? [];
      // Exactly one pending chip per artifact: the version it's parked on — the one you
      // drew on (drawing pins it), or the latest if you haven't touched it. Older versions
      // whose feedback you already sent are excluded via sentAnnotationKeys.
      const versionId = selectedVersionByArtifact[group.id] || versions[versions.length - 1]?.versionId || group.latestCommit;
      if (!versionId) continue;
      const key = `${group.artifactId}::${versionId}`;
      const record = annotationRecords[key];
      if (!record || record.annotations.length === 0 || sentAnnotationKeys.has(key)) continue;
      // Stamp the version's 1-based position (the same number the panel badges) from the
      // loaded versions: a record loaded from the backend carries versionSeq 0 (the server
      // doesn't compute it), so the chip would otherwise show no badge.
      const versionNumber = versions.findIndex((version) => version.versionId === versionId) + 1;
      records.push(versionNumber > 0 && versionNumber !== record.image.versionSeq
        ? { ...record, image: { ...record.image, versionSeq: versionNumber } }
        : record);
    }
    return records;
  }, [openGroups, versionsByArtifact, selectedVersionByArtifact, annotationRecords, sentAnnotationKeys]);

  // Sent-message annotation chips carry whatever version_seq was baked into the transcript
  // at send time — which is 0 for records loaded from the backend (the server doesn't
  // compute the 1-based position). Stamp it here from the loaded versions so the transcript
  // chips badge the drawn-on version exactly like the composer chips do.
  const enrichAnnotationVersions = useCallback((message: ChatMessage): ChatMessage => {
    const records = message.meta?.artifactAnnotationRecords as ArtifactAnnotationRecord[] | undefined;
    if (!records || records.length === 0) return message;
    let changed = false;
    const enriched = records.map((record) => {
      if (record.image.versionSeq > 0) return record;
      const versions = versionsByArtifact[record.image.artifactId];
      if (!versions) return record;
      const versionNumber = versions.findIndex((version) => version.versionId === record.image.versionId) + 1;
      if (versionNumber <= 0) return record;
      changed = true;
      return { ...record, image: { ...record.image, versionSeq: versionNumber } };
    });
    if (!changed) return message;
    return { ...message, meta: { ...message.meta, artifactAnnotationRecords: enriched } };
  }, [versionsByArtifact]);

  // Auto-open a brand-new open_artifact result exactly once (render-phase state update,
  // the same pattern as previousActiveSteps below), activating it and opening the panel.
  const newlyOpened: string[] = [];
  for (const artifactId of transcriptArtifacts.keys()) {
    if (!seenTranscriptIds.has(artifactId)) newlyOpened.push(artifactId);
  }
  if (newlyOpened.length > 0) {
    setSeenTranscriptIds((current) => new Set([...current, ...newlyOpened]));
    setOpenArtifactIds((current) => [...current, ...newlyOpened.filter((id) => !current.includes(id))]);
    setActiveTabId(newlyOpened[newlyOpened.length - 1]);
    setSidePanelOpen("artifact", true);
  }

  // Open an artifact as a tab by its group id. If already open, just switch to it.
  const handleOpenTab = useCallback((artifactId: string) => {
    if (!artifactId) return;
    setOpenArtifactIds((current) => (current.includes(artifactId) ? current : [...current, artifactId]));
    setActiveTabId(artifactId);
    setHistoryMode(false);
  }, [setActiveTabId, setOpenArtifactIds]);

  // Open a changed file from the History list in the same tab+filmstrip flow. The file may
  // never have been surfaced as a tab, so its group is registered here.
  const handleOpenHistoryFile = useCallback((entry: ArtifactIndexEntry, versionId: string) => {
    const group = groupFromIndexEntry(entry);
    setHistoryGroupsById((current) => (current[group.id] ? current : { ...current, [group.id]: group }));
    setSelectedVersionByArtifact((current) => ({ ...current, [group.id]: versionId }));
    setOpenArtifactIds((current) => (current.includes(group.id) ? current : [...current, group.id]));
    setActiveTabId(group.id);
    setHistoryMode(false);
    setSidePanelOpen("artifact", true);
  }, [setSidePanelOpen]);

  // Close a specific artifact tab. If it was the active tab, switch to the nearest.
  const handleCloseTab = useCallback((artifactId: string) => {
    setOpenArtifactIds((current) => {
      const index = current.indexOf(artifactId);
      if (index === -1) return current;
      const next = current.filter((id) => id !== artifactId);
      if (activeTabId === artifactId) {
        setActiveTabId(next.length > 0 ? next[Math.min(index, next.length - 1)] : null);
      }
      return next;
    });
  }, [activeTabId, setActiveTabId, setOpenArtifactIds]);

  // Restore the artifact's file to a specific commit (the selected version), then refetch
  // the surfaces/index so the change surfaces as a new version.
  const handleRestore = useCallback((group: ArtifactGroup, commitSha: string) => {
    if (!sessionId || !commitSha || !group.relativePath) return;
    void restoreArtifact(sessionId, {
      locationUri: group.locationUri,
      gitDirectory: group.gitDirectory,
      workTree: group.workTree,
      relativePath: group.relativePath,
      commitSha,
    }).then((ok) => {
      if (ok) setArtifactVersionsReloadKey((current) => current + 1);
    });
  }, [sessionId]);

  const handleAnnotationChange = useCallback((image: ArtifactImageIdentity, annotations: ArtifactImageAnnotation[]) => {
    annotationChangeSequenceRef.current += 1;
    const updatedAt = new Date().toISOString();
    // Editing an already-sent version's annotations makes them pending again — the new
    // feedback should ride along on the next message.
    setSentAnnotationKeys((current) => {
      if (!current.has(image.key)) return current;
      const next = new Set(current);
      next.delete(image.key);
      return next;
    });
    setAnnotationRecords((current) => {
      const next = { ...current };
      if (annotations.length === 0) {
        delete next[image.key];
        if (sessionId) void deleteArtifactAnnotations(sessionId, image.artifactId, image.versionId);
      } else {
        const record = { image, annotations, updatedAt };
        next[image.key] = record;
        if (sessionId) void saveArtifactAnnotations(sessionId, record);
      }
      return next;
    });
    if (annotations.length > 0) {
      // Pin the panel to the version being annotated, so a newer version captured in the
      // background doesn't yank the view off the work in progress.
      setSelectedVersionByArtifact((current) =>
        current[image.artifactId] === image.versionId ? current : { ...current, [image.artifactId]: image.versionId },
      );
    }
  }, [sessionId, setAnnotationRecords, setSelectedVersionByArtifact]);

  // Artifact activation from a tool card flows through the message tree, while the panel
  // owns the actual tab state and rendered artifact.
  const activeArtifactTabId = activeTabId;
  const handleActivateArtifact = handleOpenTab;

  // "Try again" on a turn-error box re-runs the turn that failed by resending the
  // most recent user message. The failed turn produced no lasting state, so a
  // plain resend is the correct retry (a rate limit or provider blip clears on its
  // own; a rejected request goes back through the same path).
  const handleRetry = useCallback(() => {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const candidate = messages[index];
      if (candidate.role === "user" && candidate.content.trim()) {
        send(candidate.content);
        return;
      }
    }
  }, [messages, send]);

  // A tool call awaiting the user's approval or answer pauses the turn. While it is
  // outstanding, the composer may only queue (see handleSend) and Stop auto-denies it.
  const hasInputRequired = isStreaming && messages.some(
    (message) => message.role === "tool_call" && message.meta?.status === "input_required"
  );
  useEffect(() => {
    hasInputRequiredRef.current = hasInputRequired;
  }, [hasInputRequired]);
  // The first pending input-required prompt (an ask_user question or a permission
  // approval), surfaced as an overlay above the input. Both live outside the tool
  // card so a pending decision always grabs attention at the bottom of the chat,
  // and resolving one reveals the next. A question takes precedence on the same
  // card, though in practice a card carries only one.
  let pendingPrompt: (
    | { kind: "question"; question: ToolQuestion }
    | { kind: "permission"; permission: ToolPermission; title: string; command?: string; arguments?: Record<string, unknown> }
    | null
  ) = null;
  if (isStreaming) {
    for (const message of messages) {
      if (message.role !== "tool_call" || message.meta?.status !== "input_required") continue;
      const question = message.meta?.question as ToolQuestion | undefined;
      if (question) {
        pendingPrompt = { kind: "question", question };
        break;
      }
      const permission = message.meta?.permission as ToolPermission | undefined;
      if (permission) {
        const name = message.content;
        const args = message.meta?.arguments as Record<string, unknown> | undefined;
        const command = name === "bash" && args?.command ? String(args.command) : "";
        pendingPrompt = {
          kind: "permission",
          permission,
          title: getToolCallDisplay(name, args, tToolDisplay).label,
          command: command || undefined,
          arguments: args,
        };
        break;
      }
    }
  }
  // Audio + system-notification side of a pending decision. The attention cue
  // plays for the first prompt in a turn, while later prompts stay silent; a
  // permission prompt additionally raises a system notification carrying the
  // overlay's own primary action ("Allow once") as its action button — shown
  // only while the window is unfocused, and retracted the moment the request
  // resolves or is superseded, so nothing stale lingers in the notification
  // center. Strings reuse the overlay's, so the two surfaces can never drift.
  const tPermission = useTranslations("PermissionOverlay");
  const pendingPermissionId = pendingPrompt?.kind === "permission" ? pendingPrompt.permission.requestId : "";
  const pendingQuestionId = pendingPrompt?.kind === "question" ? pendingPrompt.question.requestId : "";
  const pendingPermissionBody = pendingPrompt?.kind === "permission" ? pendingPrompt.command || pendingPrompt.title : "";
  const notifiedPermissionRef = useRef("");
  const attentionSoundPlayedRef = useRef(false);
  const pendingPromptId = pendingPermissionId || pendingQuestionId;
  useEffect(() => {
    if (!isStreaming) {
      attentionSoundPlayedRef.current = false;
      return;
    }
    if (!pendingPromptId || attentionSoundPlayedRef.current) return;
    attentionSoundPlayedRef.current = true;
    playAttentionSound();
  }, [isStreaming, pendingPromptId]);
  useEffect(() => {
    const previous = notifiedPermissionRef.current;
    if (previous && previous !== pendingPermissionId) void closePermissionNotification(previous);
    notifiedPermissionRef.current = pendingPermissionId;
    if (!pendingPermissionId) return;
    void notifyPermissionRequest({
      requestId: pendingPermissionId,
      title: tPermission("approvalNeeded"),
      body: pendingPermissionBody,
      actionLabel: tPermission("allowOnce"),
    });
  }, [pendingPermissionId, pendingPermissionBody, tPermission]);
  // The notification's action button resolves the request exactly like the
  // overlay's primary button would.
  useEffect(() => {
    setPermissionNotificationHandler((requestId) => handlePermission(requestId, "allow_once"));
    return () => setPermissionNotificationHandler(null);
  }, [handlePermission]);

  const handleArtifactResizeStart = useCallback((event: PointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    setArtifactMaximized(false);
    const startX = event.clientX;
    const startWidth = artifactPanelWidth;

    function handlePointerMove(moveEvent: globalThis.PointerEvent) {
      // Clamp to the same bounds the region's CSS enforces (minW 360 / maxW 80vw,
      // capped at 900) so the drag can never fight the styled limits.
      const nextWidth = Math.min(Math.min(900, Math.round(window.innerWidth * 0.8)), Math.max(360, startWidth + startX - moveEvent.clientX));
      setArtifactPanelWidth(nextWidth);
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
  }, [artifactPanelWidth]);

  return (
    <ArtifactEventProvider onEvent={handleArtifactEvent}>
    <Flex h="100%" minW={0} position="relative">
      <Flex direction="column" flex={1} minW={0} h="100%">
        {/* Persistent top bar: session identity on the left, session tools on the
            right. Always visible so the controls have a stable home; the title
            fills in once the session names itself, matching the sidebar default
            until then. */}
        <Flex align="center" gap={2} px={2} h={TOP_BAR_HEIGHT} flexShrink={0} minW={0}>
          {onToggleHistory ? (
            <Tooltip content={historyOpen ? translation("hideConversations") : translation("showConversations")} openDelay={300}>
              <IconButton
                aria-label={historyOpen ? translation("hideConversationsSidebar") : translation("showConversationsSidebar")}
                variant="ghost"
                colorPalette="gray"
                flexShrink={0}
                onClick={onToggleHistory}
              >
                {historyOpen ? <LuPanelLeftClose size={14} /> : <LuPanelLeftOpen size={14} />}
              </IconButton>
            </Tooltip>
          ) : (
            <Box color="fg.muted" flexShrink={0}><LuMessageSquare size={14} /></Box>
          )}
          <Text textStyle="panelTitle" fontWeight="medium" truncate minW={0} flex={1}>
            {sessionId ? (sessionTitle || translation("untitledConversation")) : translation("newConversation")}
          </Text>
          <GitStatusBar status={directoryStatus} />
          <Flex align="center" gap={1} flexShrink={0}>
            <ToolbarAction
              label={translation("terminalAndBackground")}
              icon={<LuTerminal size={14} />}
              active={backgroundPanelOpen}
              colorPalette="green"
              indicator={runningShellCount > 0}
              onClick={() => setSidePanelOpen("background", !backgroundPanelOpen)}
            />
            <ToolbarAction
              label={translation("artifacts")}
              icon={<LuAppWindow size={14} />}
              active={artifactPanelOpen}
              colorPalette="blue"
              indicator={openGroups.length > 0 || artifactIndex.length > 0}
              onClick={() => setSidePanelOpen("artifact", !artifactPanelOpen)}
            />
            <ToolbarAction
              label={translation("settings")}
              icon={<LuSettings size={14} />}
              onClick={() => openSettings("general")}
            />
            <DropdownMenu
              trigger={
                <IconButton aria-label={translation("sessionOptions")} variant="ghost">
                  <LuEllipsis size={14} />
                </IconButton>
              }
              minW="200px"
            >
              <Menu.Item
                value="reveal"
                fontSize="xs"
                disabled={!revealPath}
                onClick={() => { if (revealPath) void revealInFinder(revealPath); }}
              >
                <LuFolderOpen size={13} />
                <Box flex={1}>{translation("openThisFolder")}</Box>
              </Menu.Item>
              <Menu.Item
                value="delete"
                fontSize="xs"
                color="red.fg"
                _hover={{ bg: "red.subtle" }}
                disabled={!sessionId || !onDeleteSession}
                onClick={() => setDeleteConfirmOpen(true)}
              >
                <LuTrash2 size={13} />
                <Box flex={1}>{translation("deleteSession")}</Box>
              </Menu.Item>
            </DropdownMenu>
          </Flex>
        </Flex>
        <Box position="relative" flex={1} minH={0} display="flex" flexDirection="column">
        <Box ref={scrollContainerRef} flex={1} minH={0} display="flex" flexDirection="column" overflowY="auto" px={4} py={3} onScroll={handleScroll} css={transcriptPinned ? scrollFade : scrollFadeTopBottom} style={{ overflowAnchor: "none", scrollbarGutter: "stable both-edges" }}>
          {!chatReady || isHistoryLoading ? (
            <Flex h="100%" />
          ) : historyError ? (
            <Flex direction="column" align="center" justify="center" minH="100%" gap={6} px={2}>
              <EmptyState.Root>
                <EmptyState.Content>
                  <EmptyState.Indicator>
                    <LuTriangleAlert />
                  </EmptyState.Indicator>
                  <VStack gap={1}>
                    <EmptyState.Title>{translation("loadConversationErrorTitle")}</EmptyState.Title>
                    <EmptyState.Description>
                      {translation("loadConversationErrorDescription")}
                    </EmptyState.Description>
                  </VStack>
                  <Button variant="solid" colorPalette="blue" onClick={reloadHistory}>
                    {translation("retry")}
                  </Button>
                </EmptyState.Content>
              </EmptyState.Root>
            </Flex>
          ) : (
            // Empty welcome ↔ message timeline is a true cross-fade, so sending the first message
            // never flashes. `mode="popLayout"` pops the exiting welcome out of flow (position it
            // absolute) so the timeline — carrying the just-sent message — takes its place at once
            // and fades in while the welcome fades out over it; there is no blank frame between the
            // two (which `mode="wait"` left). `initial={false}` keeps an existing session's load
            // un-animated: only the empty→timeline transition fades.
            <AnimatePresence mode="popLayout" initial={false}>
              {messages.length === 0 ? (
                <motion.div key="empty" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.15, ease: "easeOut" }} style={{ width: "100%" }}>
                  <Flex direction="column" align="center" gap={8} px={4} pt={{ base: 10, md: 20 }} pb={{ base: 8, md: 12 }}>
                    {/* The blank-conversation state inside a project: no brand lockup (that lives
                        on the Projects home) — the build prompt, the project's locations (dotted
                        by connection status), then the folder's skills. */}
                    <Flex direction="column" align="center" gap={4}>
                      <Heading as="h2" fontSize="3xl" fontWeight="semibold" textAlign="center">
                        {translation("buildPrompt", { folder: currentFolderName })}
                      </Heading>
                      {projectLocations.length > 0 && (
                        <Flex direction="column" align="center" gap={2}>
                          <Flex align="center" justify="center" gap={1.5} color="fg.muted">
                            <LuNetwork size={14} />
                            <Text textStyle="panelTitle">{translation("locationsAvailable")}</Text>
                          </Flex>
                          <Flex align="center" gap={2.5} wrap="wrap" justify="center">
                            {projectLocations.map((location) => (
                              <LocationChip key={location.id} location={location} />
                            ))}
                          </Flex>
                        </Flex>
                      )}
                    </Flex>

                    <AgentSkills card={agentCard ?? null} workingDirectory={workingDirectory} homeDirectory={homeDirectory} />
                  </Flex>
                </motion.div>
              ) : (
                <motion.div key="timeline" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.15, ease: "easeOut" }} style={{ width: "100%" }}>
                  {/* gap 2.5 (10px): tight enough that a tool-activity line and the prose
                      around it read as one document, while user bubbles — carrying their own
                      fill — still mark the turn boundaries. */}
                  <VStack ref={scrollContentRef} gap={2.5} align="stretch" w="full" maxW="80rem" mx="auto">
                    <AnimatePresence initial={false}>
                      {renderedTimeline.map((item, itemIndex) => {
                        const isLastItem = itemIndex === renderedTimeline.length - 1;
                        const key = item.kind === "tool_group" ? item.id : item.message.id;
                        // A tools-less group is a reasoning phase; it stays in the transcript as a
                        // persistent "Thinking" row (its own record of the planning that happened),
                        // so it is only skipped if it somehow carries neither tools nor thinking.
                        if (item.kind === "tool_group" && item.messages.length === 0 && item.thinkingTurns === 0) {
                          return null;
                        }
                        const inner = item.kind === "tool_group" ? (
                          <ChatToolGroup
                            messages={item.messages}
                            activeArtifactId={activeArtifactTabId}
                            onActivateArtifact={handleActivateArtifact}
                            keepOpen={isStreaming && isLastItem}
                          />
                        ) : (
                          <ChatMessageItem
                            message={enrichAnnotationVersions(item.message)}
                            activeArtifactId={activeArtifactTabId}
                            onActivateArtifact={handleActivateArtifact}
                            onRetry={item.message.role === "error" ? handleRetry : undefined}
                            streaming={isStreaming && isLastItem}
                          />
                        );
                        // The assistant message streams its content in (the markdown animates
                        // token by token), so its wrapper stays a plain, stable row — an entrance
                        // animation on top of the streaming text would fight it. User messages and
                        // tool-call groups, though, are complete the moment they appear, so they get
                        // a single gentle fade. `animatedKeys` limits it to rows a live turn
                        // just appended (never load or history), and `initial` only fires on mount,
                        // so a tool group fades once — not again as its calls fill in.
                        const isAssistantMessage = item.kind === "message" && item.message.role === "assistant";
                        if (isAssistantMessage) {
                          return (
                            <Box key={key} display="flex" flexDirection="column">
                              {inner}
                            </Box>
                          );
                        }
                        return (
                          <motion.div
                            key={key}
                            initial={animatedKeys.has(key) ? { opacity: 0 } : false}
                            animate={{ opacity: 1 }}
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
                      <Flex key={message.id} align="flex-start" alignSelf="flex-end" maxW="80%" gap={1.5}>
                        <IconButton
                          aria-label={translation("deleteQueuedMessage")}
                          variant="ghost"
                          colorPalette="red"
                          mt={0.5}
                          flexShrink={0}
                          onClick={() => dequeueMessage(index)}
                        >
                          <LuTrash2 size={13} />
                        </IconButton>
                        <Box
                          px={2}
                          py={1.5}
                          borderRadius="md"
                          border="1px dashed"
                          borderColor="border"
                          bg="bg.subtle"
                          opacity={0.7}
                          flex={1}
                          minW={0}
                        >
                          <Flex align="center" gap={1.5}>
                            <Span display="inline-flex" alignItems="center">
                              {message.steering ? <LuNavigation size={11} /> : <LuClock size={11} />}
                            </Span>
                            <Text textStyle="fieldLabel" color="fg.subtle">
                              {message.steering ? translation("steeringNextOpening") : translation("queued")}
                            </Text>
                          </Flex>
                          <Text fontSize="sm" color="fg.muted">{message.text}</Text>
                        </Box>
                      </Flex>
                    ))}
                  </VStack>
                </motion.div>
              )}
            </AnimatePresence>
          )}
        </Box>
        {!isAtBottom && !isHistoryLoading && !historyError && messages.length > 0 && (
          <Button
            variant="outline"
            position="absolute"
            bottom={3}
            left="50%"
            transform="translateX(-50%)"
            zIndex={2}
            bg="bg.subtle"
            color="fg"
            fontWeight="medium"
            px={2}
            onClick={scrollToBottom}
          >
            <LuArrowDown />
            {translation("jumpToLatest")}
          </Button>
        )}
        </Box>

        {/* The approval/question overlay sits in the same 80rem centered column as the messages
            and composer, so it reads as a card in the chat column instead of a bar spanning the
            whole panel. No overflow clipping here — that would slice the card's drop shadow. */}
        {pendingPrompt && (
          <Box px={4}>
            <Box w="full" maxW="80rem" mx="auto">
              {pendingPrompt.kind === "question" && (
                <QuestionOverlay question={pendingPrompt.question} onQuestion={handleQuestion} onDismiss={declineQuestion} />
              )}
              {pendingPrompt.kind === "permission" && (
                <PermissionOverlay
                  permission={pendingPrompt.permission}
                  title={pendingPrompt.title}
                  command={pendingPrompt.command}
                  arguments={pendingPrompt.arguments}
                  onPermission={handlePermission}
                />
              )}
            </Box>
          </Box>
        )}
        {/* The composer wrapper mirrors the transcript scroll container's horizontal geometry
            — same px, and the scrollbar gutter reserved via overflow:hidden + scrollbar-gutter
            stable both-edges — so the input's 80rem column co-centers with the messages above it
            at every width. `both-edges` reserves the gutter symmetrically (left AND right) so the
            centered column stays on the panel's true centre and the left/right insets match,
            rather than a single-edge gutter nudging everything off-centre. */}
        {chatReady && (
        <Box px={4} overflowY="hidden" style={{ scrollbarGutter: "stable both-edges" }}>
        <Box w="full" maxW="80rem" mx="auto">
        <ChatInput
          onSend={handleSend}
          onAbort={abort}
          isStreaming={isStreaming}
          disabled={!isConnected || !!pendingPrompt}
          sessionId={sessionId}
          initialDraft={initialInputDraft}
          onDraftChange={handleInputDraftChange}
          workingDirectory={workingDirectory}
          directoryValid={directoryValid}
          agents={agents}
          selectedAgent={agent}
          onAgentChange={onAgentChange}
          models={models}
          modelProviders={modelProviders}
          recentModels={recentModels}
          agentModel={agentModel}
          onAgentModelChange={onAgentModelChange}
          permissionMode={permissionMode}
          onPermissionModeChange={handlePermissionModeChange}
          tokenUsage={tokenUsage}
          onCompact={compact}
          isCompacting={isCompacting}
          compactionKeepRecentTurns={compactionKeepRecentTurns}
          compactionUserCount={messages.filter((message) => message.role === "user").length}
          artifactAnnotationRecords={pendingAnnotationRecords}
        />
        </Box>
        </Box>
        )}
      </Flex>

      {/* The right region: every open panel tiles into a resizable 2D grid. It sits flush to
          the chat (no gutter column) so the chat content keeps symmetric left/right padding;
          the resize handle overlaps the boundary as an absolute strip rather than consuming a
          column of space — mirroring the left sidebar's handle. */}
      <AnimatePresence initial={false}>
      {(artifactPanelOpen || backgroundPanelOpen) && (
        <MotionBox
          key="panel-region"
          data-layout="side-panel-region"
          flexShrink={0}
          h="100%"
          w={{ base: "100%", md: `min(${artifactPanelWidth}px, 55%)` }}
          minW={{ base: "100%", md: "min(360px, 55%)" }}
          maxW={{ base: "100%", md: "80vw" }}
          pr={2}
          pb={2}
          position={{ base: "absolute", md: "relative" }}
          inset={{ base: 0, md: "auto" }}
          zIndex={{ base: 3, md: "auto" }}
          // Same slide + fade (and timing) as the history sidebar on the left, mirrored:
          // the two edges of the window open and close as one family. Only transform and
          // opacity animate — the width is applied instantly, so the resize drag never
          // fights a tween and the transcript reflows exactly once per toggle.
          initial={{ opacity: 0, x: 24 }}
          animate={{ opacity: 1, x: 0 }}
          exit={{ opacity: 0, x: 24 }}
          transition={{ duration: 0.2, ease: [0.4, 0, 0.2, 1] }}
        >
          <Box
            display={{ base: "none", md: "block" }}
            position="absolute"
            top={0}
            bottom={0}
            left={-1}
            w={2}
            cursor="col-resize"
            zIndex={1}
            onPointerDown={handleArtifactResizeStart}
          />
            <PanelTiles
              gap={8}
              panels={[
                artifactPanelOpen && {
                  key: "artifact",
                  onActivate: () => markSidePanelActive("artifact"),
                  content: (
                    <PanelCard
                      position="relative"
                      onMouseEnter={() => { artifactHoveredRef.current = true; }}
                      onMouseLeave={() => { artifactHoveredRef.current = false; }}
                    >
            {/* Persistent top bar with the Artifacts ⇄ History toggle. */}
            <PanelHeader
              icon={<LuAppWindow size={14} />}
              title={translation("artifacts")}
              onClose={() => setSidePanelOpen("artifact", false)}
              closeLabel={translation("collapseArtifactsSidebar")}
            >
              <SegmentedToggle
                value={historyMode ? "history" : "artifacts"}
                onChange={(next) => setHistoryMode(next === "history")}
                options={[
                  { value: "artifacts", label: translation("artifacts"), icon: <LuAppWindow size={14} /> },
                  { value: "history", label: translation("history"), icon: <LuHistory size={14} /> },
                ]}
              />
            </PanelHeader>
            {historyMode ? (
              <ArtifactHistoryList
                sessionId={sessionId}
                index={artifactIndex}
                scope={artifactScope}
                onScopeChange={setArtifactScope}
                onOpen={handleOpenHistoryFile}
              />
            ) : openGroups.length === 0 ? (
              <PanelEmptyState
                icon={<LuAppWindow />}
                title={translation("noArtifactsTitle")}
                description={translation("noArtifactsDescription")}
              />
            ) : activeGroup ? (
              <>
                {/* Artifact tabs — the shared PanelTab, one per open artifact. The strip
                    scrolls horizontally when tabs overflow, but the scrollbar is hidden so
                    it doesn't flicker across the row while scrolling. */}
                <Flex
                  px={4}
                  pt={2}
                  overflowX="auto"
                  flexShrink={0}
                  css={{ scrollbarWidth: "none", "&::-webkit-scrollbar": { display: "none" } }}
                >
                  <Flex gap={1.5}>
                    {openGroups.map((tab) => {
                      const tabSourcePath = tab.relativePath || tab.source || "";
                      const isUrl = /^https?:\/\//i.test(tabSourcePath);
                      return (
                        <PanelTab
                          key={tab.id}
                          icon={tab.kind === "file" ? <LuFile size={13} /> : <LuAppWindow size={13} />}
                          label={tab.title}
                          active={tab.id === activeTabId}
                          onSelect={() => setActiveTabId(tab.id)}
                          onClose={() => handleCloseTab(tab.id)}
                          tooltip={
                            <Box fontSize="xs" lineHeight="1.6" maxW="340px">
                              <Text fontWeight="semibold" mb={tabSourcePath ? 1 : 0} color="fg">{tab.title}</Text>
                              {tabSourcePath ? (
                                <InlineField label={isUrl ? translation("url") : translation("path")}>
                                  <Text wordBreak="break-all" fontFamily="var(--app-font-mono)">{tabSourcePath}</Text>
                                </InlineField>
                              ) : null}
                            </Box>
                          }
                          closeLabel={translation("closeTab", { title: tab.title })}
                        />
                      );
                    })}
                  </Flex>
                </Flex>
                {/* Active tab header with controls — title, reload, maximize, restore,
                    download, close. The 32px icon-button controls are the tallest element in
                    the row, so it keeps a constant height whether or not the (shorter) image
                    annotation pill is present — the bar never jumps switching image ⇄ web page. */}
                <Flex px={4} py={2} align="center" gap={1} flexShrink={0}>
                  <Flex align="center" gap={1.5} flex={1} minW={0}>
                    <Text textStyle="panelTitle" truncate>
                      {activeGroup.title}
                    </Text>
                    {activeVersions.length > 1 ? <VersionBadge number={selectedVersionNumber} active size="sm" /> : null}
                  </Flex>
                  {activeImageIdentity ? (
                    <Flex
                      align="center"
                      gap={1.5}
                      px={2}
                      py={1}
                      borderRadius="full"
                      color="fg.muted"
                      fontSize="sm"
                      fontWeight="medium"
                      flexShrink={0}
                      pointerEvents="none"
                    >
                      <LuMousePointerClick size={13} />
                      {activeAnnotations.length > 0
                        ? translation("imageAnnotationCount", { count: activeAnnotations.length })
                        : isViewingLatestVersion
                          ? translation("clickToAnnotate")
                          : translation("noAnnotations")}
                    </Flex>
                  ) : null}
                  <IconButton
                    aria-label={translation("reloadArtifact")}
                    title={translation("reload")}
                    variant="ghost"
                    boxSize="8"
                    onClick={() => setArtifactReloadKey((current) => current + 1)}
                  >
                    <LuRotateCw size={14} />
                  </IconButton>
                  <IconButton
                    aria-label={artifactMaximized ? translation("minimizeArtifact") : translation("maximizeArtifact")}
                    title={artifactMaximized ? translation("minimize") : translation("maximize")}
                    variant="ghost"
                    boxSize="8"
                    onClick={() => setArtifactMaximized((current) => !current)}
                  >
                    {artifactMaximized ? <LuMinimize2 size={14} /> : <LuMaximize2 size={14} />}
                  </IconButton>
                  {activeVersionEntry && activeGroup.relativePath ? (
                    <IconButton
                      aria-label={translation("restoreThisVersion")}
                      title={isViewingLatestVersion ? translation("restoreLatestVersion") : translation("restoreVersionToWorkingTree")}
                      variant="ghost"
                      boxSize="8"
                      onClick={() => handleRestore(activeGroup, activeVersionEntry.commitSha)}
                    >
                      <LuRotateCcw size={14} />
                    </IconButton>
                  ) : null}
                  <IconButton
                    aria-label={translation("downloadThisVersion")}
                    title={translation("download")}
                    variant="ghost"
                    boxSize="8"
                    disabled={!(activeVersionEntry?.blobSha || activeGroup.latestBlob) || !sessionId}
                    onClick={() => {
                      const blobSha = activeVersionEntry?.blobSha || activeGroup.latestBlob;
                      if (!blobSha || !sessionId) return;
                      const link = document.createElement("a");
                      link.href = artifactBytesUrl({
                        location: activeGroup.locationUri,
                        gitDirectory: activeGroup.gitDirectory,
                        sha: blobSha,
                        session: sessionId,
                        download: activeGroup.title || activeGroup.relativePath || "artifact",
                      });
                      link.download = activeGroup.title || activeGroup.relativePath || "artifact";
                      link.click();
                    }}
                  >
                    <LuDownload size={14} />
                  </IconButton>
                  <IconButton
                    aria-label={translation("closeAllArtifacts")}
                    title={translation("close")}
                    variant="ghost"
                    boxSize="8"
                    colorPalette="red"
                    onClick={() => {
                      setOpenArtifactIds([]);
                      setActiveTabId(null);
                    }}
                  >
                    <LuX size={14} />
                  </IconButton>
                </Flex>
                <Separator borderColor={activeImageIdentity ? "transparent" : "border"} />
                {/* Version timeline — a node per version, oldest → newest, with prev/next
                    stepping. Only shown when there's more than one version. Newest is
                    editable; older nodes are read-only history. */}
                {activeVersions.length > 1 ? (
                  <Flex px={4} pt={2} pb={1.5} align="center" gap={1} flexShrink={0}>
                    <IconButton
                      aria-label={translation("previousVersion")}
                      variant="ghost"
                      boxSize="8"
                      disabled={selectedVersionNumber <= 1}
                      onClick={() => stepVersion(-1)}
                    >
                      <LuChevronLeft size={14} />
                    </IconButton>
                    {/* The whole track is the hit area — click anywhere to jump to the
                        nearest node, or press and drag to scrub across versions. */}
                    <Box
                      flex={1}
                      position="relative"
                      minH={8}
                      display="flex"
                      alignItems="center"
                      px={2}
                      cursor="pointer"
                      userSelect="none"
                      onPointerDown={(event) => {
                        timelineDraggingRef.current = true;
                        event.currentTarget.setPointerCapture(event.pointerId);
                        selectVersionAtClientX(event.clientX);
                      }}
                      onPointerMove={(event) => { if (timelineDraggingRef.current) selectVersionAtClientX(event.clientX); }}
                      onPointerUp={(event) => {
                        timelineDraggingRef.current = false;
                        if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
                      }}
                    >
                      <Box position="absolute" left={2} right={2} top="50%" transform="translateY(-50%)" h={0.5} bg="border" borderRadius="full" />
                      <Flex ref={timelineDotsRef} flex={1} align="center" justify="space-between" position="relative">
                        {visibleVersions.map((version, localIndex) => {
                          const versionIndex = versionWindowStart + localIndex;
                          const isSelected = version.versionId === selectedVersionId;
                          const isLatest = version.versionId === latestVersionId;
                          const liveAnnotationCount = annotationRecords[`${activeGroup.artifactId}::${version.versionId}`]?.annotations.length;
                          const annotationCount = liveAnnotationCount ?? version.annotationCount;
                          const tooltip = (
                            <Box whiteSpace="nowrap">
                              <Text fontWeight="semibold" mb={1} color="fg">
                                {translation("versionNumber", { number: versionIndex + 1 })}
                              </Text>
                              <Flex direction="column" gap={1}>
                                <InlineField label={translation("created")}>
                                  <Text>
                                    {version.createdAt && !Number.isNaN(new Date(version.createdAt).getTime())
                                      ? format.dateTime(new Date(version.createdAt), { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })
                                      : translation("unknown")}
                                  </Text>
                                </InlineField>
                                {annotationCount > 0 && (
                                  <InlineField label={translation("annotations")}><Text>{annotationCount}</Text></InlineField>
                                )}
                                {!isLatest && <InlineField label={translation("editing")}><Text>{translation("readOnly")}</Text></InlineField>}
                              </Flex>
                            </Box>
                          );
                          return (
                            <Tooltip
                              key={version.versionId}
                              content={tooltip}
                              rich
                              openDelay={200}
                            >
                              {/* A numbered node — shows the version number directly, hoverable
                                  for its tooltip; selection is handled by the track (the
                                  pointerdown bubbles up), so it never swallows a scrub click. A
                                  circle for single digits that widens into a pill for 2+ digits
                                  (minW keeps it round, px lets it grow) so multi-digit version
                                  numbers never look crowded. */}
                              <Box
                                flexShrink={0}
                                display="inline-flex"
                                alignItems="center"
                                justifyContent="center"
                                h={isSelected ? 7 : 6}
                                minW={isSelected ? 7 : 6}
                                px={1.5}
                                borderRadius="full"
                                bg={isSelected ? "blue.solid" : "bg"}
                                color={isSelected ? "white" : "fg.muted"}
                                border="2px solid"
                                borderColor={isSelected ? "blue.solid" : "border.emphasized"}
                                fontSize={isSelected ? "xs" : "2xs"}
                                fontWeight="semibold"
                                lineHeight="1"
                                transition="min-width 0.12s, height 0.12s, border-color 0.12s"
                                _hover={{ borderColor: "blue.solid" }}
                                style={{ fontVariantNumeric: "tabular-nums", textBox: "trim-both cap alphabetic" }}
                              >
                                {versionIndex + 1}
                              </Box>
                            </Tooltip>
                          );
                        })}
                      </Flex>
                    </Box>
                    <IconButton
                      aria-label={translation("nextVersion")}
                      variant="ghost"
                      boxSize="8"
                      disabled={selectedVersionNumber >= activeVersions.length}
                      onClick={() => stepVersion(1)}
                    >
                      <LuChevronRight size={14} />
                    </IconButton>
                  </Flex>
                ) : null}
                {(() => {
                  const bodyKey = `${activeGroup.id}:${selectedVersionId}:${artifactReloadKey}`;
                  // A PDF is previewed as the actual document — scrollable, lazy-rendered
                  // pages — not a text diff. Served live through the file's location
                  // (local or remote via its executor); the selected version busts the
                  // PDF byte cache so a re-capture refreshes it.
                  if (activeGroup.kind === "file" && /\.pdf$/i.test(activeGroup.absolutePath || activeGroup.relativePath || "")) {
                    const base = artifactPageUrl(activeGroup.absolutePath, { location: activeGroup.locationUri, session: sessionId ?? undefined });
                    const url = base ? `${base}${base.includes("?") ? "&" : "?"}v=${encodeURIComponent(selectedVersionId || "")}` : "";
                    return (
                      <Box key={bodyKey} flex={1} minH={0}>
                        {url ? <PdfDocumentView url={url} /> : null}
                      </Box>
                    );
                  }
                  // A code/text file shows a readable diff (selected version vs its
                  // predecessor), streamed from the backend diff endpoint.
                  if (activeGroup.kind === "file") {
                    return (
                      <Box key={bodyKey} flex={1} minH={0} display="flex" flexDirection="column">
                        <ArtifactFileDiff
                          sessionId={sessionId}
                          group={activeGroup}
                          versions={activeVersions}
                          selectedVersionId={selectedVersionId}
                          scope={artifactScope}
                          darkMode={colorMode === "dark"}
                        />
                      </Box>
                    );
                  }
                  // Desktop: an external website renders in the embedded native webview
                  // (real browser engine, top-level navigation — full fidelity). Local
                  // files, inline HTML, and the web build keep the proxied iframe.
                  const nativeUrl = nativeWebviewAvailable() && activeArtifact ? externalArtifactUrl(activeArtifact) : "";
                  if (nativeUrl) {
                    return (
                      <Box key={bodyKey} flex={1} minH={0}>
                        <NativeWebview url={nativeUrl} />
                      </Box>
                    );
                  }
                  if (!activeArtifact) {
                    return (
                      <Flex key={bodyKey} flex={1} minH={0} align="center" justify="center" color="fg.subtle" fontSize="sm">
                        {translation("loadingArtifact")}
                      </Flex>
                    );
                  }
                  return (
                    <Box key={bodyKey} flex={1} minH={0} display="flex" flexDirection="column">
                      <ArtifactView
                        artifact={activeArtifact}
                        showHeader={false}
                        fillContainer
                        annotationsControl={activeImageIdentity ? {
                          image: activeImageIdentity,
                          annotations: activeAnnotations,
                          onChange: (annotations) => handleAnnotationChange(activeImageIdentity, annotations),
                          readOnly: !isViewingLatestVersion && !activeVersionHasPendingAnnotations,
                        } : undefined}
                      />
                    </Box>
                  );
                })()}
              </>
            ) : null}
                    </PanelCard>
                  ),
                },
                backgroundPanelOpen && {
                  key: "background",
                  onActivate: () => markSidePanelActive("background"),
                  content: (
                    <BackgroundTasksPanel
                      open={backgroundPanelOpen}
                      onClose={() => setSidePanelOpen("background", false)}
                      messages={messages}
                      sessionId={sessionId}
                      workingDirectory={workingDirectory || homeDirectory || ""}
                      locations={projectLocations}
                    />
                  ),
                },
              ].filter(Boolean) as TilePanel[]}
            />
        </MotionBox>
      )}
      </AnimatePresence>

      <SettingsDialog
        open={settingsOpen}
        onOpenChange={setSettingsOpen}
        section={settingsSection}
        onSectionChange={setSettingsSection}
        currentConnectionId={currentConnectionId}
        onConnectionChange={onConnectionChange}
        projectId={projectId}
        workingDirectory={workingDirectory}
        models={models}
        modelProviders={modelProviders}
        recentModels={recentModels}
        agents={agents}
        selectedAgent={agent}
        onAgentChange={onAgentChange}
        livePermissionMode={permissionMode}
        onPermissionModeChange={handlePermissionModeChange}
        liveSandboxEnabled={sandboxEnabled}
        onSandboxEnabledChange={onSandboxEnabledChange}
        liveWorkspaceStrategy={workspaceStrategy}
        onWorkspaceStrategyChange={onWorkspaceStrategyChange}
      />

      <ConfirmDialog
        open={deleteConfirmOpen}
        onOpenChange={setDeleteConfirmOpen}
        title={translation("deleteSessionConfirmTitle")}
        confirmLabel={translation("delete")}
        confirmIcon={<LuTrash2 size={14} />}
        danger
        onConfirm={() => { if (sessionId) onDeleteSession?.(sessionId); }}
      >
        {translation("deleteSessionConfirmBody")}
      </ConfirmDialog>
    </Flex>
    </ArtifactEventProvider>
  );
}

// The diff view for a file artifact: the unified diff between the selected version and its
// predecessor, fetched from the backend and rendered with simple per-line colouring.
function ArtifactFileDiff({
  sessionId,
  group,
  versions,
  selectedVersionId,
  scope,
  darkMode,
}: {
  sessionId: string | null;
  group: ArtifactGroup;
  versions: ArtifactVersionEntry[];
  selectedVersionId: string;
  scope: ArtifactScope;
  darkMode: boolean;
}) {
  const translation = useTranslations("ChatPanel");
  // The fetched diff is keyed by its exact target so a stale result from a previous file
  // or version is never shown, and state is only written from the async callback (never
  // synchronously inside the effect).
  const [diffResult, setDiffResult] = useState<{ key: string; text: string }>({ key: "", text: "" });
  const selectedIndex = versions.findIndex((version) => version.versionId === selectedVersionId);
  const selectedVersion = selectedIndex >= 0 ? versions[selectedIndex] : versions[versions.length - 1];
  const predecessor = selectedIndex > 0 ? versions[selectedIndex - 1] : undefined;
  const fromCommit = predecessor?.commitSha ?? "";
  const toCommit = selectedVersion?.commitSha ?? "";

  const hasDiffTarget = Boolean(sessionId && toCommit && group.relativePath);
  const targetKey = `${group.gitDirectory}|${group.relativePath}|${fromCommit}|${toCommit}|${scope}`;
  useEffect(() => {
    if (!hasDiffTarget || !sessionId) return;
    let cancelled = false;
    fetchArtifactDiff(sessionId, {
      gitDirectory: group.gitDirectory,
      relativePath: group.relativePath,
      fromCommit,
      toCommit,
      location: group.locationUri,
    })
      .then((diff) => {
        if (!cancelled) setDiffResult({ key: targetKey, text: diff });
      })
      .catch(() => {
        if (!cancelled) setDiffResult({ key: targetKey, text: "" });
      });
    return () => {
      cancelled = true;
    };
  }, [hasDiffTarget, sessionId, group.gitDirectory, group.relativePath, group.locationUri, fromCommit, toCommit, targetKey]);

  const resultIsCurrent = diffResult.key === targetKey;
  const shownDiff = hasDiffTarget && resultIsCurrent ? diffResult.text : "";
  if (hasDiffTarget && !resultIsCurrent) {
    return (
      <Flex flex={1} minH={0} align="center" justify="center" color="fg.subtle" fontSize="sm">
        {translation("loadingDiff")}
      </Flex>
    );
  }
  if (!shownDiff) {
    return (
      <Flex flex={1} minH={0} align="center" justify="center" color="fg.subtle" fontSize="sm">
        {translation("noChangesToShow")}
      </Flex>
    );
  }
  const lines = shownDiff.split("\n");
  return (
    <Box flex={1} minH={0} overflow="auto" bg={darkMode ? "gray.900" : "bg.subtle"} fontFamily="var(--app-font-mono)" fontSize="xs" p={2}>
      {lines.map((line, lineIndex) => {
        const isAddition = line.startsWith("+") && !line.startsWith("+++");
        const isRemoval = line.startsWith("-") && !line.startsWith("---");
        const isHunk = line.startsWith("@@");
        const background = isAddition ? "green.subtle" : isRemoval ? "red.subtle" : isHunk ? "blue.subtle" : "transparent";
        const color = isAddition ? "green.fg" : isRemoval ? "red.fg" : isHunk ? "blue.fg" : "fg.muted";
        return (
          <Box key={lineIndex} bg={background} color={color} px={1} whiteSpace="pre" minH={4}>
            {line || " "}
          </Box>
        );
      })}
    </Box>
  );
}
