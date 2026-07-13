"use client";

// The chat-history sidebar as a self-contained unit: the project's session list with
// its sort control, new-conversation / back actions, and a per-session notification
// dot. It owns nothing about layout (the page wraps it in the resizable panel, and the
// collapsed state wraps the very same component in a hover popover), so the list looks
// and behaves identically wherever it is shown.

import { Box, Button, EmptyState, Flex, IconButton, Menu, Text, VStack } from "@chakra-ui/react";
import { useFormatter, useTranslations } from "next-intl";
import { useState } from "react";
import { LuArrowDownUp, LuChevronDown, LuEllipsis, LuFolderOpen, LuMessageSquare, LuPlus, LuTrash2 } from "react-icons/lu";
import { ProjectSwitcher } from "@/components/project-switcher";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { DropdownMenu, MenuOption } from "@/components/ui/menu";
import { Tooltip } from "@/components/ui/tooltip";
import { PanelBody, PanelCard, PanelHeader } from "@/components/ui/panel";
import { InlineField } from "@/components/tool-views/primitives";
import { revealInFinder, type AgentSummary, type FilesystemLease, type PermissionMode } from "@/lib/api";
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

export function SessionsSidebar({
  sessions,
  sessionsLoaded,
  activeSessionId,
  agents,
  sessionSort,
  onSessionSortChange,
  unseenCompletions,
  currentProjectId,
  onSwitchProject,
  onNewChat,
  onResume,
  onDeleteSession,
}: {
  sessions: SessionEntry[];
  sessionsLoaded: boolean;
  activeSessionId: string | null;
  agents: AgentSummary[];
  sessionSort: SessionSort;
  onSessionSortChange: (sort: SessionSort) => void;
  unseenCompletions: Set<string>;
  currentProjectId: string;
  onSwitchProject: (projectId: string) => void;
  onNewChat: () => void;
  onResume: (entry: SessionEntry) => void;
  onDeleteSession: (entry: SessionEntry) => void;
}) {
  const t = useTranslations("SessionsSidebar");
  const format = useFormatter();
  // Delete is confirmed through a single shared dialog rather than a per-row one.
  const [pendingDelete, setPendingDelete] = useState<SessionEntry | null>(null);

  const formatSessionTimestamp = (value: string) => {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return format.dateTime(date, {
      month: "long",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  };

  const formatPermissionMode = (mode: string): string => {
    switch (mode) {
      case "auto":
        return t("permissionAuto");
      case "read_only":
        return t("permissionReadOnly");
      case "bypass":
        return t("permissionBypass");
      default:
        return t("permissionAsk");
    }
  };

  return (
    <PanelCard flex={1}>
      {/* The project switcher anchors the sidebar: the current project's name with a
          chevron, opening a dropdown to switch, create, or manage projects — in place,
          with no landing page to bounce through. It fills the shared panel top strip so
          the sidebar's header lines up with every other panel's header. */}
      <PanelHeader pl={2}>
        <Box flex={1} minW={0}>
          <ProjectSwitcher currentProjectId={currentProjectId} onSwitchProject={onSwitchProject} />
        </Box>
      </PanelHeader>

      {/* Prominent labeled action, mirroring the reference sidebar's "New session" row.
          Disabled when we're already in a fresh, un-started conversation (no active session):
          there's nothing to start anew from a blank chat. Its `px={2}` matches the header
          override and the body below, so the switcher, this button, and the session rows all
          share one left edge. */}
      <Box px={2} flexShrink={0}>
        <Button
          w="full"
          variant="subtle"
          colorPalette="blue"
          justifyContent="flex-start"
          gap={2}
          onClick={onNewChat}
          disabled={activeSessionId === null}
        >
          <LuPlus size={14} />
          {t("newConversation")}
        </Button>
      </Box>

      <PanelBody>
        <Flex align="center" gap={1.5} mb={2} color="fg.muted">
          <Text textStyle="sectionLabel" flex={1}>{t("recents")}</Text>
          <DropdownMenu
            trigger={
              <Button
                aria-label={t("sortSessions")}
                variant="ghost"
                color="fg.muted"
                textStyle="fieldLabel"
                gap={1}
                px={1.5}
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
        ) : (
          <VStack gap={1} align="stretch">
            {sessions.map((entry) => {
              const sessionMeta = formatSessionTimestamp(entry.createdAt);
              const activeLease = entry.filesystemLeases[0];
              const isActive = entry.sessionId === activeSessionId;
              const indicator = sessionIndicator(entry, isActive, unseenCompletions);
              const workspaceTitle = entry.workspaceStrategy === "worktree" || entry.workspaceStrategy === "branch"
                ? entry.workspaceBranch
                : entry.workspaceError || t("noIsolation");
              const statusLabel = entry.awaitingInput
                ? t("statusAwaiting")
                : activeLease
                  ? t("statusEditing")
                  : entry.running
                    ? t("statusRunning")
                    : t("statusIdle");
              const agentSummary = agents.find((agent) => agent.id === entry.agent);
              const agentName = agentSummary?.title || agentSummary?.name || entry.agent;
              const sessionDetail = (
                <Box whiteSpace="nowrap">
                  <Text fontWeight="semibold" mb={1} color="fg" maxW={80} truncate>
                    {entry.title || t("untitledConversation")}
                  </Text>
                  <Flex direction="column" ps={2} gap={1}>
                    <InlineField label={t("fieldCreated")}><Text>{sessionMeta}</Text></InlineField>
                    <InlineField label={t("fieldStatus")}><Text>{statusLabel}</Text></InlineField>
                    {entry.agent && <InlineField label={t("fieldAgent")}><Text>{agentName}</Text></InlineField>}
                    {entry.workingDirectory && (
                      <InlineField label={t("fieldDirectory")}><Text truncate maxW="300px" fontFamily="var(--app-font-mono)">{entry.workingDirectory}</Text></InlineField>
                    )}
                    <InlineField label={t("fieldIsolation")}><Text truncate maxW="300px">{workspaceTitle}</Text></InlineField>
                    <InlineField label={t("fieldPermissions")}><Text>{formatPermissionMode(entry.permissionMode)}</Text></InlineField>
                  </Flex>
                </Box>
              );

              return (
                <Tooltip
                  key={entry.sessionId}
                  content={sessionDetail}
                  rich
                  openDelay={300}
                  closeDelay={60}
                  positioning={{ placement: "right", gutter: 8 }}
                >
                  <Box
                    px={2}
                    py={1.5}
                    borderRadius="md"
                    cursor="pointer"
                    // The open session is marked only by a soft, non-aggressive blue tint.
                    bg={isActive ? "blue.subtle" : undefined}
                    // Hover tint: a gentle gray in light mode, and a translucent white
                    // overlay in dark mode — the near-black `bg.*` grays are too close to the
                    // panel to read as a hover, so an alpha lift gives clear-but-soft contrast.
                    // (Chakra v3 has no `whiteAlpha` scale, so use a raw rgba value.)
                    _hover={{ bg: isActive ? "blue.muted" : { base: "gray.100", _dark: "rgba(255, 255, 255, 0.09)" } }}
                    // The per-row options button is hidden until the row is hovered/focused.
                    css={{
                      "& [data-row-actions]": { opacity: 0 },
                      "&:hover [data-row-actions]": { opacity: 1 },
                      "&:focus-within [data-row-actions]": { opacity: 1 },
                    }}
                    onClick={() => onResume(entry)}
                  >
                    <Flex align="center" gap={2}>
                      {/* Status dot: gray + pulsing while the session is still working, solid
                          blue once it's finished since you last looked (red/amber for an error
                          or an awaiting-approval prompt). A fixed-width slot keeps the titles
                          aligned whether or not there is a dot. */}
                      <Box boxSize="2" flexShrink={0} display="flex" alignItems="center" justifyContent="center">
                        {indicator ? (
                          <Box
                            boxSize="2"
                            borderRadius="full"
                            bg={INDICATOR_COLOR[indicator]}
                            className={indicator === "working" ? "status-dot-pulse" : undefined}
                          />
                        ) : null}
                      </Box>
                      <Text textStyle="fieldLabel" truncate flex={1}>
                        {entry.title || t("untitledConversation")}
                      </Text>
                      <Box data-row-actions display="flex" alignItems="center" flexShrink={0}>
                        <DropdownMenu
                          trigger={
                            <IconButton
                              aria-label={t("sessionOptions")}
                              // Plain (no background box) in every state — including hover and
                              // while the menu is open — so the ⋯ reads as a bare glyph, not a chip.
                              variant="plain"
                              boxSize={4.5}
                              color="fg.subtle"
                              _hover={{ bg: "transparent", color: "fg" }}
                              _active={{ bg: "transparent" }}
                              // Stay a bare glyph on focus too: no ring/chip when the menu
                              // closes and restores focus here (only tint the glyph).
                              _focusVisible={{ outline: "none", boxShadow: "none", color: "fg" }}
                              css={{ "&[data-state=open]": { background: "transparent", color: "var(--chakra-colors-fg)" } }}
                              onClick={(event) => event.stopPropagation()}
                            >
                              <LuEllipsis size={12} />
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
                    </Flex>
                  </Box>
                </Tooltip>
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
    </PanelCard>
  );
}
