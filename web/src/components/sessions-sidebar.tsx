"use client";

// The chat-history sidebar as a self-contained unit: the project switcher, a new-session
// row, the sorted session list (each row a status dot + marquee title + options menu), and
// a footer showing which connection the sessions live on. It owns nothing about layout (the
// page wraps it in the resizable panel, and the collapsed state wraps the very same component
// in a hover popover), so the list looks and behaves identically wherever it is shown.

import { Box, Button, chakra, EmptyState, Flex, IconButton, Input, Kbd, Menu, Text, VStack } from "@chakra-ui/react";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";
import { LuArrowDownUp, LuChevronDown, LuEllipsis, LuFolderOpen, LuGlobe, LuHardDrive, LuMessageSquare, LuSearch, LuSquarePen, LuTerminal, LuTrash2 } from "react-icons/lu";
import { ProjectSwitcher } from "@/components/project-switcher";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { DropdownMenu, MenuOption } from "@/components/ui/menu";
import { PanelBody, PanelCard, PanelHeader } from "@/components/ui/panel";
import { revealInFinder, type FilesystemLease, type PermissionMode } from "@/lib/api";
import type { ConnectionKind } from "@/lib/connection-store";

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
const ROW_MIN_H = "30px";
// Just wide enough to hold the 14px row glyph / 8px status dot centered — kept tight so
// titles hug the left edge of the pill rather than floating in from a wide empty gutter.
const LEADING_SLOT = "14px";
// The corner radius the whole row family shares — the app's standard `md` default corner.
const ROW_RADIUS = "md";
const HOVER_BG = { base: "gray.100", _dark: "rgba(255, 255, 255, 0.08)" } as const;
const SELECTED_BG = { base: "gray.200", _dark: "rgba(255, 255, 255, 0.13)" } as const;

const CONNECTION_ICON: Record<ConnectionKind, typeof LuGlobe> = {
  local: LuHardDrive,
  remote: LuGlobe,
  ssh: LuTerminal,
};

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
    <span
      ref={outerRef}
      className="sidebar-title"
      data-overflow={overflow > 0 ? "true" : undefined}
      style={{
        ["--marquee-overflow" as string]: `${travel}px`,
        ["--marquee-duration" as string]: `${(travel * 0.02).toFixed(2)}s`,
      }}
    >
      <span ref={innerRef} className="sidebar-title-inner">{text}</span>
    </span>
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
  connectionName,
  connectionKind,
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
  connectionName?: string;
  connectionKind?: ConnectionKind;
  onSwitchProject: (projectId: string) => void;
  onOpenProjectSettings: (projectId: string) => void;
  onNewChat: () => void;
  onResume: (entry: SessionEntry) => void;
  onDeleteSession: (entry: SessionEntry) => void;
}) {
  const t = useTranslations("SessionsSidebar");
  // Delete is confirmed through a single shared dialog rather than a per-row one.
  const [pendingDelete, setPendingDelete] = useState<SessionEntry | null>(null);
  const [search, setSearch] = useState("");
  const searchQuery = search.trim().toLowerCase();
  const shownSessions = searchQuery
    ? sessions.filter((entry) => (entry.title || "").toLowerCase().includes(searchQuery))
    : sessions;

  const ConnectionIcon = CONNECTION_ICON[connectionKind ?? "local"];

  return (
    <PanelCard flex={1}>
      {/* The project switcher anchors the sidebar: the current project's name with a
          chevron, opening a dropdown to switch, create, or manage projects — in place,
          with no landing page to bounce through. It fills the shared panel top strip so
          the sidebar's header lines up with every other panel's header. */}
      <PanelHeader pl={2}>
        <Box flex={1} minW={0}>
          <ProjectSwitcher currentProjectId={currentProjectId} onSwitchProject={onSwitchProject} onOpenProjectSettings={onOpenProjectSettings} />
        </Box>
      </PanelHeader>

      {/* "New session" reads as the first row of the list, not a separate button — a
          circle-plus leading glyph on the shared row grid, with a ⌘N hint that surfaces on
          hover. Disabled when we're already in a fresh, un-started conversation. */}
      <Box px={2} flexShrink={0} pb={2}>
        <chakra.button
          type="button"
          display="flex"
          w="full"
          minH={ROW_MIN_H}
          alignItems="center"
          gap={1.5}
          px={2}
          borderRadius={ROW_RADIUS}
          // The primary action reads as one: a subtle blue fill and blue glyph/label so it
          // invites the click, distinct from the neutral session rows below it.
          bg="blue.subtle"
          color="blue.fg"
          textAlign="left"
          disabled={activeSessionId === null}
          _hover={{ bg: "blue.muted" }}
          _disabled={{ opacity: 0.45, pointerEvents: "none" }}
          transition="color 0.12s, background-color 0.12s"
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
        </chakra.button>
      </Box>

      {/* Filter the list by title — the same field treatment as the settings search. */}
      <Box px={2} flexShrink={0} pb={2}>
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
        {/* Section header: the "Recents" label with a sort control that stays out of the way
            until you reach for it — hidden until the header is hovered/focused or its menu
            is open, matching the reference's reveal-on-hover section actions. */}
        <Flex
          align="center"
          gap={1.5}
          mb={1}
          color="fg.muted"
          css={{
            "& [data-section-action]": { opacity: 0 },
            "&:hover [data-section-action]": { opacity: 1 },
            "&:focus-within [data-section-action]": { opacity: 1 },
            "&:has([data-state=open]) [data-section-action]": { opacity: 1 },
          }}
        >
          <Text textStyle="sectionLabel" flex={1}>{t("recents")}</Text>
          <Box data-section-action transition="opacity 0.12s">
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
        {!sessionsLoaded ? null : sessions.length === 0 ? (
          <EmptyState.Root size="sm">
            <EmptyState.Content pt={4}>
              <EmptyState.Indicator>
                <LuMessageSquare />
              </EmptyState.Indicator>
              <VStack gap={0}>
                <EmptyState.Title fontSize="sm">{t("noConversations")}</EmptyState.Title>
                <EmptyState.Description fontSize="xs">
                  {t("noConversationsHint")}
                </EmptyState.Description>
              </VStack>
            </EmptyState.Content>
          </EmptyState.Root>
        ) : shownSessions.length === 0 ? (
          <Text fontSize="xs" color="fg.muted" px={2} py={2}>{t("noMatches", { query: search })}</Text>
        ) : (
          <VStack gap={1} align="stretch">
            {shownSessions.map((entry) => {
              const isActive = entry.sessionId === activeSessionId;
              const indicator = sessionIndicator(entry, isActive, unseenCompletions);
              const title = entry.title || t("untitledConversation");

              return (
                  // The row: a real button carries the click/keyboard target; the ⋯ actions
                  // ride as an absolutely-positioned sibling (not nested in the button, which
                  // would be invalid) that fades in only on hover/focus. A long title reveals
                  // itself by scrolling on hover (MarqueeTitle) — no tooltip, which would fight
                  // the marquee for the same hover.
                  <Box
                    key={entry.sessionId}
                    className="sidebar-row"
                    position="relative"
                    borderRadius={ROW_RADIUS}
                    bg={isActive ? SELECTED_BG : undefined}
                    _hover={{ bg: isActive ? SELECTED_BG : HOVER_BG }}
                    transition="background-color 0.12s"
                    css={{
                      // Hidden actions are also click-through (pointerEvents none), so the
                      // absolutely-positioned ⋯ never swallows a click meant for the row.
                      "& [data-row-actions]": { opacity: 0, pointerEvents: "none" },
                      "&:hover [data-row-actions]": { opacity: 1, pointerEvents: "auto" },
                      "&:focus-within [data-row-actions]": { opacity: 1, pointerEvents: "auto" },
                    }}
                  >
                    <Flex
                      as="button"
                      w="full"
                      minH={ROW_MIN_H}
                      align="center"
                      gap={1.5}
                      pl={2}
                      pr={2}
                      textAlign="left"
                      color={isActive ? "fg" : "fg.muted"}
                      _hover={{ color: "fg" }}
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
                    </Flex>
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

      {/* Footer identity: which harness this project's sessions live on — a kind glyph
          (local disk / remote / SSH) and the connection's name, the local-first analog of
          the reference sidebar's account row. */}
      {connectionName ? (
        <Flex align="center" gap={2} px={3} py={2} flexShrink={0} borderTopWidth="1px" borderColor="border.muted" color="fg.muted">
          <Box flexShrink={0} display="flex" alignItems="center"><ConnectionIcon size={13} /></Box>
          <Text fontSize="xs" truncate flex={1}>{connectionName}</Text>
        </Flex>
      ) : null}

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
    </PanelCard>
  );
}
