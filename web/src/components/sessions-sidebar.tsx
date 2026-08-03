"use client";

// The chat-history sidebar as a self-contained unit: the workspaces list, a new-session
// row, and each workspace's sorted sessions (status dot + marquee title + options menu),
// one flat list. It owns nothing about layout (the
// page wraps it in the resizable panel, and the collapsed state wraps the very same component
// in a hover popover), so the list looks and behaves identically wherever it is shown.
//
// It lists the conversations you started, and only those. A session composes by creating
// peers, so its children are ordinary sessions — but nesting them here showed one row where
// eight conversations existed, and listing them flat filled the sidebar with rows nobody
// began. Both are answers to a question this list is not asking: it answers "which
// conversation do I want", and what those conversations went and delegated is the
// delegated-work panel's to answer.

import { Alert, Box, Button, Flex, IconButton, Input, Kbd, Menu, Text, VStack } from "@chakra-ui/react";
import { swallowed } from "@/lib/swallowed";
import { useTranslations } from "next-intl";
import { useLocale } from "@/lib/i18n/locale-provider";
import { useCallback, useEffect, useState } from "react";
import { LuArrowDownUp, LuChevronDown, LuClock, LuEllipsis, LuFolderOpen, LuFolderPlus, LuSearch, LuSettings, LuSquarePen, LuTrash2 } from "react-icons/lu";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { FrankMark } from "@/components/ui/frank-mark";
import { DropdownMenu, MenuOption } from "@/components/ui/menu";
import { PanelBody, PanelCard } from "@/components/ui/panel";
import { Tooltip } from "@/components/ui/tooltip";
import { deleteWorkspace, listWorkspaces, listSshHosts, subscribeEvents, type AgentSummary, type Workspace, type SshHost } from "@/lib/api";
import { locationTargetAddress, workspaceLabel } from "./location-status";
import { NewScheduleDialog } from "./new-schedule-dialog";
import { NewWorkspaceDialog } from "./new-workspace-dialog";
import { SessionRow, type SessionEntry } from "./session-row";
import { InlineField } from "./ui/display";
import { TreeRow } from "./ui/tree-row";
import { toaster } from "./ui/toaster";
import { errorMessage } from "@/lib/errors";

// A session row, its status vocabulary and its hover card all live in `session-row.tsx`,
// shared with the session-tree panel so a conversation reads the same in both.

// The workspace hover card follows the Git bar's shape — a titled heading with the glyph that
// stands for the thing, then label/value rows — because that is already the vocabulary this
// interface uses for "here is what I know about this", and a second one would only make the
// two harder to read.

function WorkspaceHoverCard({
  label, workspace, sessionCount,
}: { label: string; workspace: Workspace; sessionCount: number }) {
  const translation = useTranslations("SessionsSidebar");
  const locations = workspace.locations ?? [];
  return (
    <Box maxW="320px">
      <Flex align="center" gap={1} mb={1} color="fg">
        <LuFolderOpen size={12} />
        <Text fontWeight="semibold" truncate>{label}</Text>
      </Flex>
      <Flex direction="column" ps={2} gap={1}>
        {/* Every location, not just the first. A workspace that reaches two machines is
            precisely the one whose card is worth opening, and showing only the head of the
            list made those indistinguishable from a plain local folder. */}
        {locations.map((location, index) => (
          <InlineField key={index} label={index === 0 ? translation("fieldLocation") : ""}>
            <Text fontFamily="mono" wordBreak="break-all">{locationTargetAddress(location)}</Text>
          </InlineField>
        ))}
        <InlineField label={translation("fieldConversations")}><Text>{sessionCount}</Text></InlineField>
        <InlineField label={translation("fieldWorkspace")}>
          <Text fontFamily="mono" wordBreak="break-all" color="fg.muted">{workspace.id}</Text>
        </InlineField>
      </Flex>
    </Box>
  );
}

export type SessionSort = "recent" | "active";

// Row geometry, shared by the New-session row, the session rows, and the footer so their
// leading glyphs, text, and left edge all line up on one grid — the reference sidebar's
// single-column rhythm. Kept as constants (not magic numbers repeated per element).
const ROW_MINIMUM_H = "30px";
// Just wide enough to hold the 14px row glyph / 8px status dot centered — kept tight so
// titles hug the left edge of the pill rather than floating in from a wide empty gutter.
const LEADING_SLOT = "14px";

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
      // Only the conversations *you* started. A session a session created is real work and is
      // listed — in the delegated-work panel, under the conversation that asked for it. Here it
      // would be a row nobody began, indistinguishable from one that was.
      sessions: shownSessions.filter(
        (session) => session.workspaceId === workspace.id && !session.parentSessionId,
      ),
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
              const label = workspaceName(workspace);
              const workspaceOpenKey = searchQuery ? `${workspace.id}:${searchQuery}` : workspace.id;
              const workspaceOpen = workspaceOpenOverrides[workspaceOpenKey]
                ?? (searchQuery ? workspaceSessions.length > 0 : workspace.id === currentWorkspaceId);
              const tooltipContent = (
                <WorkspaceHoverCard label={label} workspace={workspace} sessionCount={workspaceSessions.length} />
              );
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
                <TreeRow
                  key={workspace.id}
                  disclosure={workspaceOpen ? "open" : "closed"}
                  disclosureLabel={workspaceOpen ? translation("hideWorkspace") : translation("showWorkspace")}
                  onDisclosureChange={(nextOpen) => {
                    setWorkspaceOpenOverrides((current) => ({ ...current, [workspaceOpenKey]: nextOpen }));
                    if (nextOpen) onSwitchWorkspace(workspace.id);
                  }}
                  onActivate={() => onSwitchWorkspace(workspace.id)}
                  glyph={<LuFolderOpen size={13} />}
                  label={
                    <Tooltip content={tooltipContent} rich openDelay={350} positioning={{ placement: "right" }}>
                      <Box minW={0} w="full"><Text textStyle="xs" truncate>{label}</Text></Box>
                    </Tooltip>
                  }
                  actions={workspaceActions}
                >
                  <VStack gap={1} align="stretch">
                    {workspaceSessions.map((entry) => (
                      <SessionRow
                        key={entry.sessionId}
                        entry={entry}
                        agents={agents}
                        isActive={entry.sessionId === activeSessionId}
                        unseenCompletions={unseenCompletions}
                        onResume={onResume}
                        onRequestDelete={setPendingDelete}
                      />
                    ))}
                  </VStack>
                </TreeRow>
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
