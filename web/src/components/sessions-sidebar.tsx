"use client";

// The chat-history sidebar as a self-contained unit: the projects list, a new-session
// row, and each project's sorted sessions (status dot + marquee title + options menu),
// nested as a tree so the sessions a session creates sit under the one that created them.
// It owns nothing about layout (the
// page wraps it in the resizable panel, and the collapsed state wraps the very same component
// in a hover popover), so the list looks and behaves identically wherever it is shown.

import { Box, Button, Flex, IconButton, Input, Kbd, Menu, Span, Text, VStack } from "@chakra-ui/react";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useRef, useState } from "react";
import { LuArrowDownUp, LuChevronDown, LuChevronRight, LuEllipsis, LuFolderOpen, LuFolderPlus, LuMessageSquare, LuSearch, LuSettings, LuSquarePen, LuTrash2 } from "react-icons/lu";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { FrankMark } from "@/components/ui/frank-mark";
import { DropdownMenu, MenuOption } from "@/components/ui/menu";
import { PanelBody, PanelCard } from "@/components/ui/panel";
import { Tooltip } from "@/components/ui/tooltip";
import { deleteProject, listProjects, listSshHosts, revealInFinder, subscribeEvents, type PermissionMode, type Project, type SshHost } from "@/lib/api";
import type { ConnectionKind } from "@/lib/connection-store";
import { locationTargetAddress, locationTargetLabel } from "./location-status";
import { NewProjectDialog } from "./new-project-dialog";
import { DisclosureLabel, DisclosureRow } from "./ui/disclosure-row";
import { toaster } from "./ui/toaster";

// A session's process lifecycle, as the daemon's registry reports it — not the turn's.
// What a session is doing, as the daemon derives it. Distinct from whether it *exists*,
// which is `lifecycle` and is the durable half: a session with no process is asleep, not
// gone, and the next message to it forks a new worker in about 60ms.
export type SessionActivity = "working" | "waiting" | "idle" | "asleep" | "ended";

export interface SessionEntry {
  sessionId: string;
  // The session that created this one, empty for a session the user started. A session
  // composes by creating peers, so its children are ordinary sessions that would land
  // flat in this list unless they are nested under the row that created them.
  parentSessionId: string;
  projectId: string;
  connectionId: string;
  connectionName: string;
  connectionUrl: string;
  connectionKind: ConnectionKind;
  agent: string;
  title: string;
  createdAt: string;
  workingDirectory: string;
  activity: SessionActivity;
  // Whether this session has ended for good, as opposed to merely having no process.
  ended: boolean;
  // Set when an ended session ended badly.
  failed: boolean;
  awaitingInput: boolean;
  // Why an ended session ended, when the daemon knows — shown on the status dot.
  exitReason: string;
  permissionMode: PermissionMode;
}

export type SessionSort = "recent" | "active";

// The status a session's dot reflects. "working" means it is doing something — a soft
// pulsing gray dot, shown even while it's the active session ("not finished yet"). "done"
// means it finished since you last looked — a solid blue dot, suppressed for the active
// session (you're already looking at it). Plus the two alerts: a crashed session, and one
// parked on a decision only you can make.
//
// A sleeping session gets no dot at all, deliberately. It has no process, but it is not
// gone and nothing is waiting on you; surfacing it would be surfacing an implementation
// detail as if it were news.
type SessionIndicator = "working" | "problem" | "attention" | "done";

function sessionIndicator(
  entry: SessionEntry,
  isActive: boolean,
  unseenCompletions: Set<string>,
): SessionIndicator | null {
  if (entry.failed) return "problem";
  if (entry.awaitingInput || entry.activity === "waiting") return "attention";
  if (entry.activity === "working") return "working";
  if (!isActive && unseenCompletions.has(entry.sessionId)) return "done";
  return null;
}

const INDICATOR_COLOR: Record<SessionIndicator, string> = {
  working: "gray.solid",
  problem: "red.solid",
  attention: "yellow.solid",
  done: "blue.solid",
};

const ACTIVITY_LABEL_KEY: Record<SessionActivity, string> = {
  working: "statusWorking",
  waiting: "statusWaiting",
  idle: "statusIdle",
  asleep: "statusAsleep",
  ended: "statusEnded",
};

// A session and everything it created. The daemon hands the registry out flat (each row
// carrying its parent), so the nesting is derived here rather than being a shape the
// sidebar has to flatten again to search or sort.
interface SessionTreeNode {
  entry: SessionEntry;
  children: SessionTreeNode[];
}

function buildSessionTree(entries: SessionEntry[]): SessionTreeNode[] {
  const nodes = new Map(entries.map((entry) => [entry.sessionId, { entry, children: [] as SessionTreeNode[] }]));
  const roots: SessionTreeNode[] = [];
  for (const node of nodes.values()) {
    // A child whose parent is not in this list (filtered out by the search, or living in
    // another project) is promoted to a root rather than dropped — a session is never
    // unreachable because of where its parent happens to be.
    const parent = node.entry.parentSessionId ? nodes.get(node.entry.parentSessionId) : undefined;
    if (parent && parent !== node) parent.children.push(node);
    else roots.push(node);
  }
  return roots;
}

// Every session in a subtree, including its root — so a collapsed parent can still say
// what is hiding inside it.
function collectEntries(node: SessionTreeNode): SessionEntry[] {
  return [node.entry, ...node.children.flatMap(collectEntries)];
}

// Row geometry, shared by the New-session row, the session rows, and the footer so their
// leading glyphs, text, and left edge all line up on one grid — the reference sidebar's
// single-column rhythm. Kept as constants (not magic numbers repeated per element).
const ROW_MINIMUM_H = "30px";
// Just wide enough to hold the 14px row glyph / 8px status dot centered — kept tight so
// titles hug the left edge of the pill rather than floating in from a wide empty gutter.
const LEADING_SLOT = "14px";
// The corner radius the whole row family shares — the app's standard `md` default corner.
const ROW_RADIUS = "md";
const HOVER_BG = "bg.subtle";
const SELECTED_BG = "blue.subtle";
const SELECTED_HOVER_BG = "blue.muted";

// Extra left-shift, beyond the raw overflow, so a fully-scrolled title comes to rest with
// its end clear of the row's trailing ⋯ actions (which sit ~24px in from the right) rather
// than sliding underneath them. The matching CSS mask (globals.css) keeps that trailing
// gap transparent while a title travels.
const MARQUEE_TAIL_CLEARANCE = 30;

// A session title that scrolls its overflow on hover (see `.sidebar-title` in globals.css). It
// measures how far the text overruns its box, adds the tail clearance, and hands the CSS both
// the travel distance and a matching duration (a fixed 50px/s, the reference's cadence) so long
// and short titles scroll at the same speed. The mask/animation itself is pure CSS, driven by
// the row hover.
function MarqueeTitle({ text }: { text: string }) {
  const outerRef = useRef<HTMLSpanElement>(null);
  const innerRef = useRef<HTMLSpanElement>(null);
  const [overflow, setOverflow] = useState(0);

  useEffect(() => {
    const outer = outerRef.current;
    const inner = innerRef.current;
    if (!outer || !inner) return;
    const measure = () => setOverflow(Math.max(0, Math.round(inner.scrollWidth - outer.clientWidth)));
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(outer);
    observer.observe(inner);
    return () => observer.disconnect();
  }, [text]);

  const travel = overflow > 0 ? overflow + MARQUEE_TAIL_CLEARANCE : 0;
  return (
    <Span
      ref={outerRef}
      className="sidebar-title"
      data-overflow={overflow > 0 ? "true" : undefined}
      style={{
        ["--marquee-overflow" as string]: `${travel}px`,
        ["--marquee-duration" as string]: `${(travel * 0.02).toFixed(2)}s`,
      }}
    >
      <Span ref={innerRef} className="sidebar-title-inner">{text}</Span>
    </Span>
  );
}

// One session row, plus — nested beneath it, behind a chevron — the sessions it
// created. Children start collapsed: a task that fans out puts one row in the list, not
// one per child, which is the whole reason the hierarchy is rendered at all. The row
// itself always resumes the session; only the chevron toggles the subtree, so the two
// gestures never compete for the same click.
function SessionTreeRow({
  node,
  activeSessionId,
  unseenCompletions,
  expandedSessions,
  onToggleExpanded,
  onResume,
  onRequestDelete,
}: {
  node: SessionTreeNode;
  activeSessionId: string | null;
  unseenCompletions: Set<string>;
  expandedSessions: Set<string>;
  onToggleExpanded: (sessionId: string) => void;
  onResume: (entry: SessionEntry) => void;
  onRequestDelete: (entry: SessionEntry) => void;
}) {
  const translation = useTranslations("SessionsSidebar");
  const entry = node.entry;
  const isActive = entry.sessionId === activeSessionId;
  const indicator = sessionIndicator(entry, isActive, unseenCompletions);
  const title = entry.title || translation("untitledConversation");
  const hasChildren = node.children.length > 0;
  const expanded = expandedSessions.has(entry.sessionId);
  // What a collapsed parent is hiding. Without this the tree would swallow exactly the
  // signals the sidebar exists to raise — a child parked on a permission prompt, or one
  // that crashed — behind a chevron the user has no reason to open.
  const hidden = hasChildren && !expanded ? node.children.flatMap(collectEntries) : [];
  const hiddenAttention = hidden.some((child) => child.awaitingInput);
  const hiddenProblem = hidden.some((child) => child.failed);
  const statusLabel = translation(
    (entry.failed ? "statusFailed" : ACTIVITY_LABEL_KEY[entry.activity] ?? "statusIdle") as Parameters<typeof translation>[0]
  );
  const statusTooltip = (
    <Box>
      <Text color="fg">{entry.awaitingInput ? translation("awaitingInput") : statusLabel}</Text>
      {entry.exitReason ? <Text color="fg.muted">{entry.exitReason}</Text> : null}
    </Box>
  );

  return (
    <Box minW={0}>
      <Box
        className="sidebar-row"
        borderRadius={ROW_RADIUS}
        bg={isActive ? SELECTED_BG : undefined}
        _hover={{ bg: isActive ? SELECTED_HOVER_BG : HOVER_BG }}
        transition="background-color 0.12s"
        css={{
          "& [data-row-actions]": { opacity: 0, pointerEvents: "none" },
          "&:hover [data-row-actions]": { opacity: 1, pointerEvents: "auto" },
          "&:focus-within [data-row-actions]": { opacity: 1, pointerEvents: "auto" },
        }}
      >
        <Flex align="center" gap={0.5} minW={0}>
          {hasChildren ? (
            <Button
              type="button"
              aria-label={expanded ? translation("hideChildSessions") : translation("showChildSessions")}
              variant="plain"
              h={5}
              minW={0}
              px={1}
              gap={0.5}
              flexShrink={0}
              color={hiddenProblem ? "red.fg" : hiddenAttention ? "yellow.fg" : "fg.subtle"}
              _hover={{ bg: "transparent", color: "fg" }}
              _focusVisible={{ outline: "none", boxShadow: "none", color: "fg" }}
              onClick={() => onToggleExpanded(entry.sessionId)}
            >
              {expanded ? <LuChevronDown size={12} /> : <LuChevronRight size={12} />}
              {expanded ? null : <Text fontSize="2xs" lineHeight="1">{hidden.length}</Text>}
            </Button>
          ) : null}
          <Box flex={1} minW={0}>
            <DisclosureRow
              fill
              tone={isActive ? "active" : "muted"}
              onActivate={() => onResume(entry)}
              icon={
                <Tooltip content={statusTooltip} rich openDelay={350} positioning={{ placement: "right" }}>
                  <Box position="relative" color={isActive ? "blue.fg" : "fg.muted"}>
                    <LuMessageSquare />
                    {indicator ? (
                      <Box
                        position="absolute"
                        right="-2px"
                        bottom="-2px"
                        boxSize="1.5"
                        borderRadius="full"
                        bg={INDICATOR_COLOR[indicator]}
                        outline="1px solid"
                        outlineColor="bg.panel"
                        className={indicator === "working" ? "status-dot-pulse" : undefined}
                      />
                    ) : null}
                  </Box>
                </Tooltip>
              }
              title={
                <Tooltip content={title} openDelay={350} positioning={{ placement: "right" }}>
                  <Box minW={0} color={isActive ? "blue.fg" : undefined}>
                    <MarqueeTitle text={title} />
                  </Box>
                </Tooltip>
              }
              actions={
                <Box data-row-actions opacity={0} pointerEvents="none" transition="opacity 0.12s">
                  <DropdownMenu
                    trigger={
                      <IconButton
                        aria-label={translation("sessionOptions")}
                        variant="plain"
                        boxSize={5}
                        color="fg.subtle"
                        _hover={{ bg: "transparent", color: "fg" }}
                        _active={{ bg: "transparent" }}
                        _focusVisible={{ outline: "none", boxShadow: "none", color: "fg" }}
                        css={{ "&[data-state=open]": { background: "transparent", color: "var(--chakra-colors-fg)" } }}
                      >
                        <LuEllipsis size={13} />
                      </IconButton>
                    }
                    minW="180px"
                    positioning={{ placement: "bottom-end" }}
                  >
                    <Menu.Item
                      value="reveal"
                      fontSize="xs"
                      disabled={!entry.workingDirectory}
                      onClick={() => { if (entry.workingDirectory) void revealInFinder(entry.workingDirectory); }}
                    >
                      <LuFolderOpen size={14} />
                      <Box flex={1}>{translation("openFolder")}</Box>
                    </Menu.Item>
                    <MenuOption value="delete" danger icon={<LuTrash2 size={14} />} onClick={() => onRequestDelete(entry)}>
                      {translation("deleteSession")}
                    </MenuOption>
                  </DropdownMenu>
                </Box>
              }
            />
          </Box>
        </Flex>
      </Box>

      {/* The children hang off the same hairline rule every other disclosure body uses,
          so a created subtree reads as part of its parent rather than as a new list. */}
      {hasChildren && expanded ? (
        <Box ml={1.5} pl={3.5} py={1} borderLeft="2px solid" borderColor="border.muted">
          <VStack gap={1} align="stretch">
            {node.children.map((child) => (
              <SessionTreeRow
                key={child.entry.sessionId}
                node={child}
                activeSessionId={activeSessionId}
                unseenCompletions={unseenCompletions}
                expandedSessions={expandedSessions}
                onToggleExpanded={onToggleExpanded}
                onResume={onResume}
                onRequestDelete={onRequestDelete}
              />
            ))}
          </VStack>
        </Box>
      ) : null}
    </Box>
  );
}

export function SessionsSidebar({
  sessions,
  sessionsLoaded,
  activeSessionId,
  sessionSort,
  onSessionSortChange,
  unseenCompletions,
  currentProjectId,
  connectionId,
  onSwitchProject,
  onOpenProjectSettings,
  onNewChat,
  onResume,
  onDeleteSession,
}: {
  sessions: SessionEntry[];
  sessionsLoaded: boolean;
  activeSessionId: string | null;
  sessionSort: SessionSort;
  onSessionSortChange: (sort: SessionSort) => void;
  unseenCompletions: Set<string>;
  currentProjectId: string;
  connectionId?: string;
  onSwitchProject: (projectId: string) => void;
  onOpenProjectSettings: (projectId: string) => void;
  onNewChat: () => void;
  onResume: (entry: SessionEntry) => void;
  onDeleteSession: (entry: SessionEntry) => void;
}) {
  const translation = useTranslations("SessionsSidebar");
  const [pendingDelete, setPendingDelete] = useState<SessionEntry | null>(null);
  const [pendingProjectDelete, setPendingProjectDelete] = useState<Project | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [sshHosts, setSshHosts] = useState<SshHost[]>([]);
  const [loadedSshHostsConnectionId, setLoadedSshHostsConnectionId] = useState<string | null>(null);
  const [newProjectOpen, setNewProjectOpen] = useState(false);
  const [projectOpenOverrides, setProjectOpenOverrides] = useState<Record<string, boolean>>({});
  // Which parents have their child sessions showing. Collapsed is the default and the
  // state is additive (an id is present only once opened), so a session that fans out
  // mid-view never expands the list under the reader.
  const [expandedSessions, setExpandedSessions] = useState<Set<string>>(() => new Set());
  const toggleSessionExpanded = useCallback((sessionId: string) => {
    setExpandedSessions((current) => {
      const next = new Set(current);
      if (!next.delete(sessionId)) next.add(sessionId);
      return next;
    });
  }, []);
  const [search, setSearch] = useState("");
  const searchQuery = search.trim().toLowerCase();
  const connectionSessions = connectionId
    ? sessions.filter((entry) => entry.connectionId === connectionId)
    : sessions;
  const shownSessions = searchQuery
    ? connectionSessions.filter((entry) => (entry.title || "").toLowerCase().includes(searchQuery))
    : connectionSessions;

  const refreshProjects = useCallback(() => {
    listProjects().then(setProjects).catch(() => {});
  }, []);

  useEffect(() => {
    let cancelled = false;
    const sshHostsConnectionId = connectionId ?? "";
    const refreshSshHosts = () => {
      listSshHosts()
        .then((nextHosts) => {
          if (cancelled) return;
          setSshHosts(nextHosts);
          setLoadedSshHostsConnectionId(sshHostsConnectionId);
        })
        .catch(() => {
          if (!cancelled) setLoadedSshHostsConnectionId(sshHostsConnectionId);
        });
    };
    refreshProjects();
    refreshSshHosts();
    const unsubscribe = subscribeEvents((event) => {
      if (event.type === "projects_changed") refreshProjects();
      if (event.type === "hosts_changed") refreshSshHosts();
    });
    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, [refreshProjects, connectionId]);

  async function confirmProjectDelete() {
    if (!pendingProjectDelete) return;
    const deletedProjectId = pendingProjectDelete.id;
    try {
      await deleteProject(deletedProjectId);
      const remainingProjects = projects.filter((project) => project.id !== deletedProjectId);
      setProjects(remainingProjects);
      if (deletedProjectId === currentProjectId && remainingProjects[0]) {
        onSwitchProject(remainingProjects[0].id);
      }
    } catch (error) {
      toaster.create({
        type: "error",
        title: translation("deleteProjectError"),
        description: error instanceof Error ? error.message : "",
        closable: true,
      });
    }
  }

  function projectLabel(project: Project): string {
    const primaryLocation = project.locations?.[0];
    return primaryLocation ? locationTargetLabel(primaryLocation) : translation("untitledProject");
  }

  const visibleProjects = projects
    .map((project) => ({
      project,
      sessions: shownSessions.filter((session) => session.projectId === project.id),
    }))
    .filter(({ sessions: projectSessions }) => !searchQuery || projectSessions.length > 0);

  return (
    <PanelCard flex={1}>
      <Flex align="center" gap={2} px={3} pt={3} pb={2} flexShrink={0}>
        <FrankMark size="26px" style={{ flexShrink: 0 }} />
        <Text fontFamily="var(--font-display)" fontSize="2xl" lineHeight="1" fontWeight="bold" letterSpacing="tight">Frank</Text>
      </Flex>

      {/* "New session" reads as the first row of the list, not a separate button — a
          circle-plus leading glyph on the shared row grid, with a ⌘N hint that surfaces on
          hover. Disabled when we're already in a fresh, un-started conversation. */}
      <Box px={2} pt={1} flexShrink={0} pb={1}>
        <Button
          type="button"
          variant="subtle"
          colorPalette="blue"
          w="full"
          minH={ROW_MINIMUM_H}
          gap={1.5}
          px={2}
          justifyContent="flex-start"
          textAlign="left"
          disabled={activeSessionId === null}
          css={{ "& [data-kbd-hint]": { opacity: 0 }, "&:hover [data-kbd-hint]": { opacity: 1 } }}
          onClick={onNewChat}
        >
          <Flex w={LEADING_SLOT} flexShrink={0} align="center" justify="center">
            <LuSquarePen size={14} />
          </Flex>
          <Text flex={1} minW={0} truncate fontSize="xs" fontWeight="semibold">{translation("newConversation")}</Text>
          {/* Chakra's semantic keyboard-key component, in its `plain` variant so it reads as a
              subtle shortcut hint rather than a raised keycap chip. */}
          <Kbd data-kbd-hint variant="plain" fontFamily="var(--app-font-sans)" fontSize="xs" color="blue.fg" transition="opacity 0.12s" flexShrink={0}>⌘N</Kbd>
        </Button>
      </Box>

      <Box px={2} flexShrink={0} pb={1}>
        <Button
          type="button"
          variant="outline"
          w="full"
          minH={ROW_MINIMUM_H}
          gap={1.5}
          px={2}
          justifyContent="flex-start"
          textAlign="left"
          onClick={() => setNewProjectOpen(true)}
        >
          <Flex w={LEADING_SLOT} flexShrink={0} align="center" justify="center">
            <LuFolderPlus size={14} />
          </Flex>
          <Text flex={1} minW={0} truncate fontSize="xs" fontWeight="semibold">{translation("newProject")}</Text>
        </Button>
      </Box>

      {/* Filter the list by title — the same field treatment as the settings search. */}
      <Box px={2} flexShrink={0} pb={1}>
        <Flex align="center" gap={2} h={8} px={2} borderRadius="md" bg="bg.subtle" borderWidth="1px" borderColor="border.muted" _focusWithin={{ borderColor: "border.emphasized" }}>
          <Box color="fg.muted" flexShrink={0} display="flex" alignItems="center"><LuSearch size={14} /></Box>
          <Input
            border="none"
            size="xs"
            h="full"
            px={0}
            placeholder={translation("searchPlaceholder")}
            aria-label={translation("searchPlaceholder")}
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            _focusVisible={{ boxShadow: "none", outline: "none" }}
          />
        </Flex>
      </Box>

      <PanelBody pt={1}>
        <Flex align="center" gap={1.5} mb={1} color="fg.muted">
          <Text textStyle="sectionLabel" flex={1}>{translation("projects")}</Text>
          <Box>
            <DropdownMenu
              trigger={
                <Button
                  aria-label={translation("sortSessions")}
                  variant="ghost"
                  color="fg.muted"
                  textStyle="fieldLabel"
                  gap={1}
                  size="2xs"
                  // The menu returns focus to this trigger on close, which fires a stray
                  // focus-visible ring even after a pointer selection. Swap the ring for a
                  // subtle background so keyboard focus still reads without the harsh outline.
                  _focusVisible={{ outline: "none", boxShadow: "none", bg: "bg.subtle" }}
                >
                  <LuArrowDownUp size={12} />
                  {sessionSort === "active" ? translation("activeFirst") : translation("newest")}
                  <LuChevronDown size={12} />
                </Button>
              }
              minW="170px"
              positioning={{ placement: "bottom-end" }}
            >
              <Menu.ItemGroup>
                <Menu.ItemGroupLabel>{translation("sortBy")}</Menu.ItemGroupLabel>
                <MenuOption value="recent" selected={sessionSort === "recent"} onClick={() => onSessionSortChange("recent")}>
                  {translation("newestFirst")}
                </MenuOption>
                <MenuOption value="active" selected={sessionSort === "active"} onClick={() => onSessionSortChange("active")}>
                  {translation("activeFirst")}
                </MenuOption>
              </Menu.ItemGroup>
            </DropdownMenu>
          </Box>
        </Flex>
        {!sessionsLoaded || projects.length === 0 ? null : visibleProjects.length === 0 ? (
          <Text fontSize="xs" color="fg.muted" px={2} py={2}>{translation("noMatches", { query: search })}</Text>
        ) : (
          <VStack gap={1} align="stretch">
            {visibleProjects.map(({ project, sessions: projectSessions }) => {
              const primaryLocation = project.locations?.[0];
              const address = primaryLocation ? locationTargetAddress(primaryLocation) : "";
              const label = projectLabel(project);
              const projectOpenKey = searchQuery ? `${project.id}:${searchQuery}` : project.id;
              const projectOpen = projectOpenOverrides[projectOpenKey]
                ?? (searchQuery ? projectSessions.length > 0 : project.id === currentProjectId);
              const tooltipContent = address ? (
                <Box>
                  <Text fontWeight="semibold" color="fg" mb={1}>{label}</Text>
                  <Text color="fg.muted" fontFamily="mono" wordBreak="break-all">{address}</Text>
                </Box>
              ) : label;
              const projectActions = (
                <Box>
                  <DropdownMenu
                    trigger={
                      <IconButton
                        aria-label={translation("projectOptions")}
                        variant="plain"
                        boxSize={5}
                        color="fg.subtle"
                        _hover={{ bg: "transparent", color: "fg" }}
                        _active={{ bg: "transparent" }}
                        _focusVisible={{ outline: "none", boxShadow: "none", color: "fg" }}
                        css={{ "&[data-state=open]": { background: "transparent", color: "var(--chakra-colors-fg)" } }}
                      >
                        <LuEllipsis size={13} />
                      </IconButton>
                    }
                    minW="180px"
                    positioning={{ placement: "bottom-end" }}
                  >
                    <MenuOption value="settings" icon={<LuSettings size={14} />} onClick={() => onOpenProjectSettings(project.id)}>
                      {translation("projectSettings")}
                    </MenuOption>
                    <Menu.Item
                      value="delete-project"
                      color="red.fg"
                      disabled={projects.length <= 1}
                      onClick={() => setPendingProjectDelete(project)}
                    >
                      <LuTrash2 size={14} />
                      <Box flex={1}>{translation("deleteProject")}</Box>
                    </Menu.Item>
                  </DropdownMenu>
                </Box>
              );

              return (
                <Box
                  key={project.id}
                  className="sidebar-row"
                  borderRadius={ROW_RADIUS}
                >
                  <DisclosureRow
                    fill
                    open={projectOpen}
                    onOpenChange={(nextOpen) => {
                      setProjectOpenOverrides((current) => ({ ...current, [projectOpenKey]: nextOpen }));
                      if (nextOpen) onSwitchProject(project.id);
                    }}
                    onActivate={() => onSwitchProject(project.id)}
                    icon={<Box color="fg.muted"><LuFolderOpen /></Box>}
                    title={
                      <Tooltip content={tooltipContent} rich={Boolean(address)} openDelay={350} positioning={{ placement: "right" }}>
                        <Box minW={0}><DisclosureLabel>{label}</DisclosureLabel></Box>
                      </Tooltip>
                    }
                    actions={projectActions}
                  >
                    {projectSessions.length > 0 ? (
                      <VStack gap={1} align="stretch">
                        {buildSessionTree(projectSessions).map((node) => (
                          <SessionTreeRow
                            key={node.entry.sessionId}
                            node={node}
                            activeSessionId={activeSessionId}
                            unseenCompletions={unseenCompletions}
                            expandedSessions={expandedSessions}
                            onToggleExpanded={toggleSessionExpanded}
                            onResume={onResume}
                            onRequestDelete={setPendingDelete}
                          />
                        ))}
                      </VStack>
                    ) : undefined}
                  </DisclosureRow>
                </Box>
              );
            })}
          </VStack>
        )}
      </PanelBody>

      <ConfirmDialog
        open={pendingDelete !== null}
        onOpenChange={(open) => { if (!open) setPendingDelete(null); }}
        title={translation("deleteTitle")}
        confirmLabel={translation("deleteConfirm")}
        danger
        onConfirm={() => { if (pendingDelete) onDeleteSession(pendingDelete); }}
      >
        {translation("deleteBody", { title: pendingDelete?.title || translation("untitledConversation") })}
      </ConfirmDialog>

      {newProjectOpen ? (
        <NewProjectDialog
          open
          hosts={sshHosts}
          hostsLoaded={loadedSshHostsConnectionId === (connectionId ?? "")}
          onOpenChange={setNewProjectOpen}
          onCreated={(project) => {
            setProjects((current) => [project, ...current.filter((entry) => entry.id !== project.id)]);
            onSwitchProject(project.id);
          }}
        />
      ) : null}

      <ConfirmDialog
        open={pendingProjectDelete !== null}
        onOpenChange={(open) => { if (!open) setPendingProjectDelete(null); }}
        title={translation("deleteProjectTitle")}
        confirmLabel={translation("deleteProjectConfirm")}
        danger
        onConfirm={() => void confirmProjectDelete()}
      >
        {translation("deleteProjectBody", { project: pendingProjectDelete ? projectLabel(pendingProjectDelete) : "" })}
      </ConfirmDialog>
    </PanelCard>
  );
}
