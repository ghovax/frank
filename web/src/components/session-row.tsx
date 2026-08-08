"use client";

// One session as a row, rendered by both the sidebar and the delegated-work panel.

import { Box, Flex, IconButton, Menu, Span, Text } from "@chakra-ui/react";
import { useTranslations } from "next-intl";
import { memo, useEffect, useRef, useState, type ReactNode } from "react";
import { LuEllipsis, LuFolderOpen, LuMessagesSquare, LuTrash2 } from "react-icons/lu";
import { DropdownMenu, MenuOption } from "@/components/ui/menu";
import { Tooltip } from "@/components/ui/tooltip";
import {
  revealInFinder,
  type AgentSummary,
  type PermissionMode,
  type SessionGoal,
} from "@/lib/api";
import { PERMISSION_MODES } from "@shared/controls";
import { InlineField } from "./ui/display";
import { RelativeTime } from "./ui/relative-time";
import { TreeRow, type TreeRowDisclosure } from "./ui/tree-row";

// What a session is doing, as the daemon derives it, distinct from whether it still exists.
export type SessionActivity = "working" | "waiting" | "idle" | "asleep" | "ended";

export interface SessionEntry {
  sessionId: string;
  // The session that created this one, empty for a session the user started, and the edge the tree is built from.
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
  // What this session is working toward, when it has said. Null for a session with no goal.
  goal: SessionGoal | null;
}

// Extra left-shift so a fully-scrolled title comes to rest clear of the row's trailing actions.
const MARQUEE_TAIL_CLEARANCE = 44;

// The hover card, following the Git bar's shape because that is already this interface's vocabulary.
export function SessionHoverCard({
  entry,
  statusLabel,
  agents,
}: {
  entry: SessionEntry;
  statusLabel: string;
  agents: AgentSummary[];
}) {
  const translation = useTranslations("SessionsSidebar");
  const permissions = useTranslations("SessionControls");
  const title = entry.title || translation("untitledConversation");
  const created = new Date(entry.createdAt);
  // Both of these are identifiers on the wire and names on screen, resolved from their own catalogues.
  const agent = agents.find((candidate) => candidate.id === entry.agent);
  const agentName = agent?.title || agent?.name || entry.agent;
  const permissionKey = PERMISSION_MODES.choices.find(
    (choice) => choice.value === entry.permissionMode,
  )?.labelKey;
  return (
    <Box maxW="320px">
      <Flex align="center" gap={1} mb={1} color="fg">
        <LuMessagesSquare size={12} />
        <Text fontWeight="semibold" truncate>
          {title}
        </Text>
      </Flex>
      <Flex direction="column" ps={2} gap={1}>
        <InlineField label={translation("fieldAgent")}>
          <Text>{agentName}</Text>
        </InlineField>
        <InlineField label={translation("fieldStatus")}>
          <Text color={entry.failed ? "red.fg" : entry.awaitingInput ? "yellow.fg" : undefined}>
            {statusLabel}
          </Text>
        </InlineField>
        {Number.isNaN(created.getTime()) ? null : (
          <InlineField label={translation("fieldCreated")}>
            <RelativeTime date={created} />
          </InlineField>
        )}
        {permissionKey ? (
          <InlineField label={translation("fieldPermissions")}>
            <Text>{permissions(permissionKey as Parameters<typeof permissions>[0])}</Text>
          </InlineField>
        ) : null}
        {entry.exitReason ? (
          <InlineField label={translation("fieldExitReason")}>
            <Text color="fg.muted">{entry.exitReason}</Text>
          </InlineField>
        ) : null}
      </Flex>
    </Box>
  );
}

// The status a session's dot reflects, from working through problem and attention to done.
export type SessionIndicator = "working" | "problem" | "attention" | "done";

export function sessionIndicator(
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

export const INDICATOR_COLOR: Record<SessionIndicator, string> = {
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

// A session title that scrolls its overflow on hover, measuring the overrun and handing the CSS both numbers.
export function MarqueeTitle({ text }: { text: string }) {
  const outerRef = useRef<HTMLSpanElement>(null);
  const innerRef = useRef<HTMLSpanElement>(null);
  const [overflow, setOverflow] = useState(0);

  useEffect(() => {
    const outer = outerRef.current;
    const inner = innerRef.current;
    if (!outer || !inner) return;
    const measure = () =>
      setOverflow(Math.max(0, Math.round(inner.scrollWidth - outer.clientWidth)));
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
      // The sidebar sits one step below the default text size, since it is a list to scan rather than prose.
      textStyle="xs"
      data-overflow={overflow > 0 ? "true" : undefined}
      style={{
        ["--marquee-overflow" as string]: `${travel}px`,
        ["--marquee-duration" as string]: `${(travel * 0.02).toFixed(2)}s`,
      }}
    >
      <Span ref={innerRef} className="sidebar-title-inner">
        {text}
      </Span>
    </Span>
  );
}

// The row itself: a status dot, the title, and the menu, on the shared tree grid. Memoized because a row is
// dear to build — two tooltips, a menu and a measured title — and folding a workspace re-renders every other.
export const SessionRow = memo(function SessionRow({
  entry,
  agents,
  isActive,
  unseenCompletions,
  disclosure,
  onDisclosureChange,
  badges,
  onResume,
  onRequestDelete,
  children,
}: {
  entry: SessionEntry;
  agents: AgentSummary[];
  isActive: boolean;
  unseenCompletions: Set<string>;
  // Present only in a list where something can expand, so the sidebar spends no width on the column.
  disclosure?: TreeRowDisclosure;
  onDisclosureChange?: (open: boolean) => void;
  badges?: ReactNode;
  onResume: (entry: SessionEntry) => void;
  onRequestDelete: (entry: SessionEntry) => void;
  children?: ReactNode;
}) {
  const translation = useTranslations("SessionsSidebar");
  const indicator = sessionIndicator(entry, isActive, unseenCompletions);
  const title = entry.title || translation("untitledConversation");
  const statusLabel = translation(
    (entry.failed ? "statusFailed" : ACTIVITY_LABEL_KEY[entry.activity]) as Parameters<
      typeof translation
    >[0],
  );
  const statusTooltip = (
    <Box>
      <Text color="fg">{entry.awaitingInput ? translation("awaitingInput") : statusLabel}</Text>
      {entry.exitReason ? <Text color="fg.muted">{entry.exitReason}</Text> : null}
    </Box>
  );

  return (
    <TreeRow
      disclosure={disclosure}
      onDisclosureChange={onDisclosureChange}
      disclosureLabel={
        disclosure === "open" ? translation("hideChildSessions") : translation("showChildSessions")
      }
      selected={isActive}
      onActivate={() => onResume(entry)}
      label={
        <Tooltip
          content={
            <SessionHoverCard
              entry={entry}
              agents={agents}
              statusLabel={entry.awaitingInput ? translation("awaitingInput") : statusLabel}
            />
          }
          rich
          openDelay={350}
          positioning={{ placement: "right" }}
        >
          <Box minW={0} w="full">
            <MarqueeTitle text={title} />
          </Box>
        </Tooltip>
      }
      // The status rides at the trailing edge, where it does not hold a column open on every quiet row.
      badges={
        badges || indicator ? (
          <>
            {badges}
            {indicator ? (
              <Tooltip
                content={statusTooltip}
                rich
                openDelay={350}
                positioning={{ placement: "left" }}
              >
                <Box
                  boxSize="1.5"
                  borderRadius="full"
                  bg={INDICATOR_COLOR[indicator]}
                  className={indicator === "working" ? "status-dot-pulse" : undefined}
                />
              </Tooltip>
            ) : null}
          </>
        ) : undefined
      }
      actions={
        // No actions marker here, since the row owns the reveal and a second marker would hide the menu inside it.
        <Box>
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
                css={{
                  "&[data-state=open]": {
                    background: "transparent",
                    color: "var(--chakra-colors-fg)",
                  },
                }}
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
              onClick={() => {
                if (entry.workingDirectory) void revealInFinder(entry.workingDirectory);
              }}
            >
              <LuFolderOpen size={14} />
              <Box flex={1}>{translation("openFolder")}</Box>
            </Menu.Item>
            <MenuOption
              value="delete"
              danger
              icon={<LuTrash2 size={14} />}
              onClick={() => onRequestDelete(entry)}
            >
              {translation("deleteSession")}
            </MenuOption>
          </DropdownMenu>
        </Box>
      }
    >
      {children}
    </TreeRow>
  );
});
