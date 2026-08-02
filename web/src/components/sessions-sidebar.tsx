"use client";

// The chat-history sidebar as a self-contained unit: the workspaces list, a new-session
// row, and each workspace's sorted sessions (status dot + marquee title + options menu),
// nested as a tree so the sessions a session creates sit under the one that created them.
// It owns nothing about layout (the
// page wraps it in the resizable panel, and the collapsed state wraps the very same component
// in a hover popover), so the list looks and behaves identically wherever it is shown.

import { Alert, Box, Button, Flex, IconButton, Input, Kbd, Menu, Span, Text, VStack } from "@chakra-ui/react";
import { swallowed } from "@/lib/swallowed";
import { useTranslations } from "next-intl";
import { useLocale } from "@/lib/i18n/locale-provider";
import { useCallback, useEffect, useRef, useState } from "react";
import { LuArrowDownUp, LuChevronDown, LuChevronRight, LuClock, LuEllipsis, LuFolderOpen, LuFolderPlus, LuSearch, LuSettings, LuSquarePen, LuTrash2 } from "react-icons/lu";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { FrankMark } from "@/components/ui/frank-mark";
import { DropdownMenu, MenuOption } from "@/components/ui/menu";
import { PanelBody, PanelCard } from "@/components/ui/panel";
import { Tooltip } from "@/components/ui/tooltip";
import { deleteWorkspace, listWorkspaces, listSshHosts, revealInFinder, subscribeEvents, type AgentSummary, type PermissionMode, type Workspace, type SshHost } from "@/lib/api";
import { locationTargetAddress, workspaceLabel } from "./location-status";
import { NewScheduleDialog } from "./new-schedule-dialog";
import { NewWorkspaceDialog } from "./new-workspace-dialog";
import { DisclosureLabel, DisclosureRow } from "./ui/disclosure-row";
import { toaster } from "./ui/toaster";
import { errorMessage } from "@/lib/errors";

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
  workspaceId: string;
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
// A sleeping or idle session shows no dot at all — there is nothing to report, and a mark
// against every row would say nothing while making the few that matter harder to find. The
// dot sits at the row's trailing edge, beside the ⋯, and takes no space when absent. It used
// to hold a fixed slot on the leading edge so that quiet rows lined up with busy ones; that
// alignment was bought with a permanent indent on every title in the list, for a mark most
// rows never show. Nothing shifts as sessions change state, because the ⋯ it sits beside
// keeps its own width whether or not it is visible.
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
  waiting: "awaitingInput",
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
    // another workspace) is promoted to a root rather than dropped — a session is never
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

// Extra left-shift, beyond the raw overflow, so a fully-scrolled title comes to rest with
// its end clear of the row's trailing ⋯ actions rather than sliding underneath them. The
// button is 32px wide, so this is that plus a margin, and it matches the clear zone of the
// hover mask in globals.css — the two describe the same edge and drifted apart once already.
const MARQUEE_TAIL_CLEARANCE = 44;

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
      // The sidebar sits one step below the app's default text size: it is a list to scan,
      // not prose to read, and a smaller face fits more of a title before the marquee has to
      // do any work. The workspace row above matches this deliberately, so a name and the
      // conversations under it read as one list rather than two.
      textStyle="xs"
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
    (entry.failed ? "statusFailed" : ACTIVITY_LABEL_KEY[entry.activity]) as Parameters<typeof translation>[0]
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
        bg={isActive ? "blue.subtle" : undefined}
        _hover={{ bg: isActive ? "blue.muted" : "bg.subtle" }}
        transition="background-color 0.12s"
        // The row draws the selected and hover background, and `DisclosureRow` inside it is a
        // fixed-height strip with no padding of its own — so without this the highlight ended
        // exactly at the glyph and the last letter of the title, which read as a label crammed
        // into a box rather than a row that happens to be selected.
        px={2}
        py={1}
        css={{
          // Hidden with `display`, not with opacity. An invisible button still occupies its
          // width, and because the actions are laid over the row's right edge that width pushed
          // the status dot inward — so a row with nothing to report still looked like it was
          // holding space open on the right. Taking it out of layout entirely lets the dot sit
          // flush against the edge, and the ⋯ pushes it aside only while the row is hovered.
          //
          // All of which describes a pointer, and is why the reveal is gated on there being one.
          // Hiding a control behind hover on a touch device hides it for good — there is no
          // gesture that produces hover, so the ⋯ menu was unreachable on a phone — and worse,
          // WebKit gives the first tap on a row whose `:hover` changes what is rendered to the
          // hover state, so opening a conversation took two taps and the first did nothing.
          "@media (hover: hover)": {
            "& [data-row-actions]": { display: "none" },
            "&:hover [data-row-actions]": { display: "flex" },
            "&:focus-within [data-row-actions]": { display: "flex" },
            // Same nesting problem as the title mask in globals.css: a workspace row contains its
            // session rows, so its `:hover` revealed every nested row's actions at once.
            "&:hover .sidebar-row:not(:hover):not(:focus-within) [data-row-actions]": {
              display: "none",
            },
          },
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
              // The status leads the row, and only when there is one. `DisclosureRow` renders no
              // glyph slot at all for a row that passes no icon, so a quiet session spends
              // nothing on the left and its title starts at the row's edge — which is what the
              // fixed slot got wrong before, holding an indent open on every row for a mark most
              // of them never show. Leading rather than trailing because it belongs to the
              // conversation, not to the controls: pinned to the right it read as another button
              // and sat hard against the edge.
              icon={indicator ? (
                <Tooltip content={statusTooltip} rich openDelay={350} positioning={{ placement: "right" }}>
                  <Box
                    boxSize="1.5"
                    borderRadius="full"
                    bg={INDICATOR_COLOR[indicator]}
                    className={indicator === "working" ? "status-dot-pulse" : undefined}
                  />
                </Tooltip>
              ) : undefined}
              title={
                <Tooltip content={title} openDelay={350} positioning={{ placement: "right" }}>
                  <Box minW={0} color={isActive ? "blue.fg" : undefined}>
                    <MarqueeTitle text={title} />
                  </Box>
                </Tooltip>
              }
              actionsOverlay
              actions={
                <Box data-row-actions>
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
  currentWorkspaceId,
  onSwitchWorkspace,
  onOpenWorkspaceSettings,
  onNewChat,
  onResume,
  onDeleteSession,
  agents,
}: {
  sessions: SessionEntry[];
  sessionsLoaded: boolean;
  activeSessionId: string | null;
  sessionSort: SessionSort;
  onSessionSortChange: (sort: SessionSort) => void;
  unseenCompletions: Set<string>;
  currentWorkspaceId: string;
  onSwitchWorkspace: (workspaceId: string) => void;
  onOpenWorkspaceSettings: (workspaceId: string) => void;
  onNewChat: () => void;
  onResume: (entry: SessionEntry) => void;
  onDeleteSession: (entry: SessionEntry) => void;
  // The profiles a schedule can be given. Passed down rather than fetched here: the sidebar
  // has no other reason to know what an agent is, and the parent already holds the list.
  agents: AgentSummary[];
}) {
  const translation = useTranslations("SessionsSidebar");
  const { locale } = useLocale();
  const [pendingDelete, setPendingDelete] = useState<SessionEntry | null>(null);
  const [pendingWorkspaceDelete, setPendingWorkspaceDelete] = useState<Workspace | null>(null);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [sshHosts, setSshHosts] = useState<SshHost[]>([]);
  const [sshHostsLoaded, setSshHostsLoaded] = useState(false);
  const [newWorkspaceOpen, setNewWorkspaceOpen] = useState(false);
  const [newScheduleOpen, setNewScheduleOpen] = useState(false);
  const [workspaceOpenOverrides, setWorkspaceOpenOverrides] = useState<Record<string, boolean>>({});
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
  const shownSessions = searchQuery
    ? sessions.filter((entry) => (entry.title || "").toLowerCase().includes(searchQuery))
    : sessions;

  const refreshWorkspaces = useCallback(() => {
    listWorkspaces().then(setWorkspaces).catch((caught) => swallowed({ component: "sessions-sidebar", operation: "list the SSH hosts" }, caught));
  }, []);

  useEffect(() => {
    let cancelled = false;
    const refreshSshHosts = () => {
      listSshHosts()
        .then((nextHosts) => {
          if (cancelled) return;
          setSshHosts(nextHosts);
          setSshHostsLoaded(true);
        })
        .catch(() => {
          if (!cancelled) setSshHostsLoaded(true);
        });
    };
    refreshWorkspaces();
    refreshSshHosts();
    const unsubscribe = subscribeEvents((event) => {
      if (event.type === "workspaces_changed") refreshWorkspaces();
      if (event.type === "hosts_changed") refreshSshHosts();
    });
    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, [refreshWorkspaces]);

  async function confirmWorkspaceDelete() {
    if (!pendingWorkspaceDelete) return;
    const deletedWorkspaceId = pendingWorkspaceDelete.id;
    try {
      await deleteWorkspace(deletedWorkspaceId);
      const remainingWorkspaces = workspaces.filter((workspace) => workspace.id !== deletedWorkspaceId);
      setWorkspaces(remainingWorkspaces);
      if (deletedWorkspaceId === currentWorkspaceId && remainingWorkspaces[0]) {
        onSwitchWorkspace(remainingWorkspaces[0].id);
      }
    } catch (error) {
      toaster.create({
        type: "error",
        title: translation("deleteWorkspaceError"),
        description: errorMessage(error),
        closable: true,
      });
    }
  }

  function workspaceName(workspace: Workspace): string {
    return workspaceLabel(workspace.locations, locale, translation("untitledWorkspace"));
  }

  const visibleWorkspaces = workspaces
    .map((workspace) => ({
      workspace,
      sessions: shownSessions.filter((session) => session.workspaceId === workspace.id),
    }))
    .filter(({ sessions: workspaceSessions }) => !searchQuery || workspaceSessions.length > 0);

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
          onClick={() => setNewWorkspaceOpen(true)}
        >
          <Flex w={LEADING_SLOT} flexShrink={0} align="center" justify="center">
            <LuFolderPlus size={14} />
          </Flex>
          <Text flex={1} minW={0} truncate fontSize="xs" fontWeight="semibold">{translation("newWorkspace")}</Text>
        </Button>
      </Box>

      {/* A schedule is something you make, like a workspace or a conversation, so it sits with
          them rather than three screens into Settings — where it was reachable only by somebody
          who already knew it was there. It belongs to a workspace, so it is offered only once
          one is selected. */}
      {currentWorkspaceId ? (
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
            onClick={() => setNewScheduleOpen(true)}
          >
            <Flex w={LEADING_SLOT} flexShrink={0} align="center" justify="center">
              <LuClock size={14} />
            </Flex>
            <Text flex={1} minW={0} truncate fontSize="xs" fontWeight="semibold">{translation("newSchedule")}</Text>
          </Button>
        </Box>
      ) : null}

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
          <Text textStyle="sectionLabel" flex={1}>{translation("workspaces")}</Text>
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
        {!sessionsLoaded || workspaces.length === 0 ? null : visibleWorkspaces.length === 0 ? (
          // The same alert the settings panel uses, rather than a line of muted text. A search
          // that found nothing is a state to report, not a caption: as plain text it read as a
          // row of the list that happened to be greyed out, which is the one thing it must not
          // look like in a list of rows.
          <Alert.Root status="info" size="sm" borderRadius="md" my={2} alignItems="center">
            <Alert.Indicator />
            <Alert.Content minW={0}>
              <Alert.Description fontSize="xs">{translation("noMatches", { query: search })}</Alert.Description>
            </Alert.Content>
          </Alert.Root>
        ) : (
          <VStack gap={1} align="stretch">
            {visibleWorkspaces.map(({ workspace, sessions: workspaceSessions }) => {
              const primaryLocation = workspace.locations?.[0];
              const address = primaryLocation ? locationTargetAddress(primaryLocation) : "";
              const label = workspaceName(workspace);
              const workspaceOpenKey = searchQuery ? `${workspace.id}:${searchQuery}` : workspace.id;
              const workspaceOpen = workspaceOpenOverrides[workspaceOpenKey]
                ?? (searchQuery ? workspaceSessions.length > 0 : workspace.id === currentWorkspaceId);
              const tooltipContent = address ? (
                <Box>
                  <Text fontWeight="semibold" color="fg" mb={1}>{label}</Text>
                  <Text color="fg.muted" fontFamily="mono" wordBreak="break-all">{address}</Text>
                </Box>
              ) : label;
              const workspaceActions = (
                <Box>
                  <DropdownMenu
                    trigger={
                      <IconButton
                        aria-label={translation("workspaceOptions")}
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
                    <MenuOption value="settings" icon={<LuSettings size={14} />} onClick={() => onOpenWorkspaceSettings(workspace.id)}>
                      {translation("workspaceSettings")}
                    </MenuOption>
                    <Menu.Item
                      value="delete-workspace"
                      color="red.fg"
                      disabled={workspaces.length <= 1}
                      onClick={() => setPendingWorkspaceDelete(workspace)}
                    >
                      <LuTrash2 size={14} />
                      <Box flex={1}>{translation("deleteWorkspace")}</Box>
                    </Menu.Item>
                  </DropdownMenu>
                </Box>
              );

              return (
                <Box
                  key={workspace.id}
                  className="sidebar-row"
                  borderRadius={ROW_RADIUS}
                  pr={2}
                  py={1}
                >
                  <DisclosureRow
                    fill
                    open={workspaceOpen}
                    onOpenChange={(nextOpen) => {
                      setWorkspaceOpenOverrides((current) => ({ ...current, [workspaceOpenKey]: nextOpen }));
                      if (nextOpen) onSwitchWorkspace(workspace.id);
                    }}
                    onActivate={() => onSwitchWorkspace(workspace.id)}
                    icon={<Box color="fg.muted"><LuFolderOpen /></Box>}
                    title={
                      <Tooltip content={tooltipContent} rich={Boolean(address)} openDelay={350} positioning={{ placement: "right" }}>
                        <Box minW={0}><DisclosureLabel>{label}</DisclosureLabel></Box>
                      </Tooltip>
                    }
                    actions={workspaceActions}
                  >
                    {workspaceSessions.length > 0 ? (
                      // The workspace row pads itself by 2 so its own highlight clears the label,
                      // but that padding also inset this list — which then adds its own 2 on
                      // each row. The result was a conversation's ⋯ sitting 8px inside the
                      // workspace's ⋯, reading as two columns that nearly line up. Give the
                      // right-hand padding back to the nested rows so the trailing controls
                      // share one edge; the left inset is the disclosure rail and stays.
                      <VStack gap={1} align="stretch" mr={-2}>
                        {buildSessionTree(workspaceSessions).map((node) => (
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

      {newWorkspaceOpen ? (
        <NewWorkspaceDialog
          open
          hosts={sshHosts}
          hostsLoaded={sshHostsLoaded}
          onOpenChange={setNewWorkspaceOpen}
          onCreated={(workspace) => {
            setWorkspaces((current) => [workspace, ...current.filter((entry) => entry.id !== workspace.id)]);
            onSwitchWorkspace(workspace.id);
          }}
        />
      ) : null}

      <NewScheduleDialog
        workspaceId={currentWorkspaceId}
        agents={agents}
        open={newScheduleOpen}
        onOpenChange={setNewScheduleOpen}
      />

      <ConfirmDialog
        open={pendingWorkspaceDelete !== null}
        onOpenChange={(open) => { if (!open) setPendingWorkspaceDelete(null); }}
        title={translation("deleteWorkspaceTitle")}
        confirmLabel={translation("deleteConfirm")}
        danger
        onConfirm={() => void confirmWorkspaceDelete()}
      >
        {translation("deleteWorkspaceBody", { workspace: pendingWorkspaceDelete ? workspaceName(pendingWorkspaceDelete) : "" })}
      </ConfirmDialog>
    </PanelCard>
  );
}
