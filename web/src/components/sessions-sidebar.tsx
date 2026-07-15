"use client";

// The chat-history sidebar as a self-contained unit: the projects list, a new-session
// row, and each project's sorted sessions (status dot + marquee title + options menu).
// It owns nothing about layout (the
// page wraps it in the resizable panel, and the collapsed state wraps the very same component
// in a hover popover), so the list looks and behaves identically wherever it is shown.

import { Box, Button, Flex, IconButton, Image, Input, Kbd, Menu, Span, Text, VStack } from "@chakra-ui/react";
import { useTranslations } from "next-intl";
import { useCallback, useEffect, useRef, useState } from "react";
import { LuArrowDownUp, LuChevronDown, LuEllipsis, LuFolderOpen, LuFolderPlus, LuSearch, LuSettings, LuSquarePen, LuTrash2 } from "react-icons/lu";
import daisyIcon from "@/app/icon.png";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { DropdownMenu, MenuOption } from "@/components/ui/menu";
import { PanelBody, PanelCard } from "@/components/ui/panel";
import { Tooltip } from "@/components/ui/tooltip";
import { deleteProject, listProjects, listSshHosts, revealInFinder, subscribeEvents, type FilesystemLease, type PermissionMode, type Project, type SshHost } from "@/lib/api";
import type { ConnectionKind } from "@/lib/connection-store";
import { locationTargetAddress, locationTargetLabel } from "./location-status";
import { NewProjectDialog } from "./new-project-dialog";
import { toaster } from "./ui/toaster";

export interface SessionEntry {
  sessionId: string;
  projectId: string;
  connectionId: string;
  connectionName: string;
  connectionUrl: string;
  connectionKind: ConnectionKind;
  agent: string;
  title: string;
  createdAt: string;
  workingDirectory: string;
  runtimeWorkingDirectory: string;
  workspaceStrategy: "none" | "branch" | "worktree";
  workspaceBranch: string;
  workspaceError: string;
  running: boolean;
  awaitingInput: boolean;
  filesystemLeases: FilesystemLease[];
  inputDraft: string;
  permissionMode: PermissionMode;
}

export type SessionSort = "recent" | "active";

type SidebarListEntry =
  | { kind: "project"; project: Project }
  | { kind: "session"; session: SessionEntry };

// The status a session's dot reflects. "working" means the session is still doing
// something — a soft pulsing gray dot, shown even while it's the active session ("not
// finished yet"). "done" means it finished since you last looked — a solid blue dot,
// suppressed for the active session (you're already looking at it). Plus the two alerts.
type SessionIndicator = "working" | "problem" | "attention" | "done";

function sessionIndicator(
  entry: SessionEntry,
  isActive: boolean,
  unseenCompletions: Set<string>,
): SessionIndicator | null {
  if (entry.workspaceError) return "problem";
  if (entry.awaitingInput) return "attention";
  if (entry.running) return "working";
  if (!isActive && unseenCompletions.has(entry.sessionId)) return "done";
  return null;
}

const INDICATOR_COLOR: Record<SessionIndicator, string> = {
  working: "gray.solid",
  problem: "red.solid",
  attention: "yellow.solid",
  done: "blue.solid",
};

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
  const t = useTranslations("SessionsSidebar");
  const [pendingDelete, setPendingDelete] = useState<SessionEntry | null>(null);
  const [pendingProjectDelete, setPendingProjectDelete] = useState<Project | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [sshHosts, setSshHosts] = useState<SshHost[]>([]);
  const [newProjectOpen, setNewProjectOpen] = useState(false);
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
    refreshProjects();
    listSshHosts().then(setSshHosts).catch(() => {});
    return subscribeEvents((event) => {
      if (event.type === "projects_changed") refreshProjects();
      if (event.type === "hosts_changed") listSshHosts().then(setSshHosts).catch(() => {});
    });
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
        title: t("deleteProjectError"),
        description: error instanceof Error ? error.message : "",
        closable: true,
      });
    }
  }

  function projectLabel(project: Project): string {
    const primaryLocation = project.locations?.[0];
    return primaryLocation ? locationTargetLabel(primaryLocation) : t("untitledProject");
  }

  function renderProjectRow(project: Project) {
    const primaryLocation = project.locations?.[0];
    const address = primaryLocation ? locationTargetAddress(primaryLocation) : "";
    const label = projectLabel(project);
    const tooltipContent = address ? (
      <Box>
        <Text fontWeight="semibold" color="fg" mb={1}>{label}</Text>
        <Text color="fg.muted" fontFamily="mono" wordBreak="break-all">{address}</Text>
      </Box>
    ) : label;

    return (
      <Box
        key={project.id}
        className="sidebar-row"
        position="relative"
        borderRadius={ROW_RADIUS}
        _hover={{ bg: HOVER_BG }}
        transition="background-color 0.12s"
        css={{
          "& [data-row-actions]": { opacity: 0, pointerEvents: "none" },
          "&:hover [data-row-actions]": { opacity: 1, pointerEvents: "auto" },
          "&:focus-within [data-row-actions]": { opacity: 1, pointerEvents: "auto" },
        }}
      >
        <Tooltip
          content={tooltipContent}
          rich={Boolean(address)}
          openDelay={350}
          positioning={{ placement: "right" }}
        >
          <Button
            variant="ghost"
            w="full"
            minH={ROW_MINIMUM_H}
            gap={1.5}
            px={2}
            justifyContent="flex-start"
            textAlign="left"
            color="fg.muted"
            _hover={{ color: "fg" }}
            transition="color 0.12s"
            onClick={() => onSwitchProject(project.id)}
          >
            <Flex w={LEADING_SLOT} flexShrink={0} align="center" justify="center">
              <LuFolderOpen size={14} />
            </Flex>
            <Text flex={1} minW={0} truncate fontSize="xs" fontWeight="semibold">
              {label}
            </Text>
          </Button>
        </Tooltip>
        <Box
          data-row-actions
          position="absolute"
          right={1}
          top="50%"
          transform="translateY(-50%)"
          display="flex"
          alignItems="center"
          transition="opacity 0.12s"
        >
          <DropdownMenu
            trigger={
              <IconButton
                aria-label={t("projectOptions")}
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
              {t("projectSettings")}
            </MenuOption>
            <Menu.Item
              value="delete-project"
              color="red.fg"
              disabled={projects.length <= 1}
              onClick={() => setPendingProjectDelete(project)}
            >
              <LuTrash2 size={14} />
              <Box flex={1}>{t("deleteProject")}</Box>
            </Menu.Item>
          </DropdownMenu>
        </Box>
      </Box>
    );
  }

  const sidebarEntries = projects.flatMap<SidebarListEntry>((project) => {
    const projectSessions = shownSessions.filter((session) => session.projectId === project.id);
    if (searchQuery && projectSessions.length === 0) return [];
    return [
      { kind: "project", project },
      ...projectSessions.map((session): SidebarListEntry => ({ kind: "session", session })),
    ];
  });

  return (
    <PanelCard flex={1}>
      <Flex align="center" gap={2} px={3} pt={3} pb={2} flexShrink={0}>
        <Image src={daisyIcon.src} alt="" boxSize="26px" borderRadius="md" flexShrink={0} />
        <Text fontFamily="var(--font-display)" fontSize="2xl" lineHeight="1" fontWeight="bold" letterSpacing="tight">Daisy</Text>
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
          <Text flex={1} minW={0} truncate fontSize="xs" fontWeight="semibold">{t("newConversation")}</Text>
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
          <Text flex={1} minW={0} truncate fontSize="xs" fontWeight="semibold">{t("newProject")}</Text>
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
            placeholder={t("searchPlaceholder")}
            aria-label={t("searchPlaceholder")}
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            _focusVisible={{ boxShadow: "none", outline: "none" }}
          />
        </Flex>
      </Box>

      <PanelBody pt={1}>
        <Flex align="center" gap={1.5} mb={1} color="fg.muted">
          <Text textStyle="sectionLabel" flex={1}>{t("projects")}</Text>
          <Box>
            <DropdownMenu
              trigger={
                <Button
                  aria-label={t("sortSessions")}
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
                  {sessionSort === "active" ? t("activeFirst") : t("newest")}
                  <LuChevronDown size={12} />
                </Button>
              }
              minW="170px"
              positioning={{ placement: "bottom-end" }}
            >
              <Menu.ItemGroup>
                <Menu.ItemGroupLabel>{t("sortBy")}</Menu.ItemGroupLabel>
                <MenuOption value="recent" selected={sessionSort === "recent"} onClick={() => onSessionSortChange("recent")}>
                  {t("newestFirst")}
                </MenuOption>
                <MenuOption value="active" selected={sessionSort === "active"} onClick={() => onSessionSortChange("active")}>
                  {t("activeFirst")}
                </MenuOption>
              </Menu.ItemGroup>
            </DropdownMenu>
          </Box>
        </Flex>
        {!sessionsLoaded || projects.length === 0 ? null : sidebarEntries.length === 0 ? (
          <Text fontSize="xs" color="fg.muted" px={2} py={2}>{t("noMatches", { query: search })}</Text>
        ) : (
          <VStack gap={1} align="stretch">
            {sidebarEntries.map((sidebarEntry) => {
              if (sidebarEntry.kind === "project") return renderProjectRow(sidebarEntry.project);
              const entry = sidebarEntry.session;
              const isActive = entry.sessionId === activeSessionId;
              const indicator = sessionIndicator(entry, isActive, unseenCompletions);
              const title = entry.title || t("untitledConversation");

              return (
                  // The row: a real button carries the click/keyboard target; the ⋯ actions
                  // ride as an absolutely-positioned sibling (not nested in the button, which
                  // would be invalid) that fades in only on hover/focus. A long title reveals
                  // itself by scrolling on hover (MarqueeTitle), while a delayed tooltip keeps
                  // the complete title accessible without interrupting a quick pointer pass.
                  <Box
                    key={entry.sessionId}
                    className="sidebar-row"
                    ml={3}
                    position="relative"
                    borderRadius={ROW_RADIUS}
                    bg={isActive ? SELECTED_BG : undefined}
                    _hover={{ bg: isActive ? SELECTED_HOVER_BG : HOVER_BG }}
                    transition="background-color 0.12s"
                    css={{
                      // Hidden actions are also click-through (pointerEvents none), so the
                      // absolutely-positioned ⋯ never swallows a click meant for the row.
                      "& [data-row-actions]": { opacity: 0, pointerEvents: "none" },
                      "&:hover [data-row-actions]": { opacity: 1, pointerEvents: "auto" },
                      "&:focus-within [data-row-actions]": { opacity: 1, pointerEvents: "auto" },
                    }}
                  >
                    <Tooltip
                      content={title}
                      openDelay={350}
                      positioning={{ placement: "right" }}
                    >
                      <Button
                        variant="plain"
                        w="full"
                        minH={ROW_MINIMUM_H}
                        gap={1.5}
                        pl={2}
                        pr={2}
                        justifyContent="flex-start"
                        textAlign="left"
                        color={isActive ? "blue.fg" : "fg.muted"}
                        _hover={{ color: isActive ? "blue.fg" : "fg" }}
                        transition="color 0.12s"
                        onClick={() => onResume(entry)}
                      >
                        {/* Fixed-width leading slot keeps titles aligned whether or not a status
                            dot is present. Gray + pulsing while working; solid blue once finished
                            since you last looked; red/amber for an error or an awaiting prompt. */}
                        <Flex w={LEADING_SLOT} flexShrink={0} align="center" justify="center">
                          {indicator ? (
                            <Box
                              boxSize="2"
                              borderRadius="full"
                              bg={INDICATOR_COLOR[indicator]}
                              className={indicator === "working" ? "status-dot-pulse" : undefined}
                            />
                          ) : null}
                        </Flex>
                        <Box flex={1} minW={0} fontSize="xs">
                          <MarqueeTitle text={title} />
                        </Box>
                      </Button>
                    </Tooltip>
                    <Box
                      data-row-actions
                      position="absolute"
                      right={1}
                      top="50%"
                      transform="translateY(-50%)"
                      display="flex"
                      alignItems="center"
                      transition="opacity 0.12s"
                    >
                      <DropdownMenu
                        trigger={
                          <IconButton
                            aria-label={t("sessionOptions")}
                            // Plain (no background box) in every state — including hover and
                            // while the menu is open — so the ⋯ reads as a bare glyph, not a chip.
                            variant="plain"
                            boxSize={5}
                            color="fg.subtle"
                            _hover={{ bg: "transparent", color: "fg" }}
                            _active={{ bg: "transparent" }}
                            _focusVisible={{ outline: "none", boxShadow: "none", color: "fg" }}
                            css={{ "&[data-state=open]": { background: "transparent", color: "var(--chakra-colors-fg)" } }}
                            onClick={(event) => event.stopPropagation()}
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
                          <Box flex={1}>{t("openFolder")}</Box>
                        </Menu.Item>
                        <MenuOption value="delete" danger icon={<LuTrash2 size={14} />} onClick={() => setPendingDelete(entry)}>
                          {t("deleteSession")}
                        </MenuOption>
                      </DropdownMenu>
                    </Box>
                  </Box>
              );
            })}
          </VStack>
        )}
      </PanelBody>

      <ConfirmDialog
        open={pendingDelete !== null}
        onOpenChange={(open) => { if (!open) setPendingDelete(null); }}
        title={t("deleteTitle")}
        confirmLabel={t("deleteConfirm")}
        danger
        onConfirm={() => { if (pendingDelete) onDeleteSession(pendingDelete); }}
      >
        {t("deleteBody", { title: pendingDelete?.title || t("untitledConversation") })}
      </ConfirmDialog>

      {newProjectOpen ? (
        <NewProjectDialog
          open
          hosts={sshHosts}
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
        title={t("deleteProjectTitle")}
        confirmLabel={t("deleteProjectConfirm")}
        danger
        onConfirm={() => void confirmProjectDelete()}
      >
        {t("deleteProjectBody", { project: pendingProjectDelete ? projectLabel(pendingProjectDelete) : "" })}
      </ConfirmDialog>
    </PanelCard>
  );
}
