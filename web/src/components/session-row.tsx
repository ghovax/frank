"use client";

// One session, as a row. Two surfaces render it: the sidebar, which lists the conversations
// you started, and the delegated-work panel, which nests what those conversations created. It
// lives here rather than in either of them because a session must look and behave the same
// wherever it is shown — the same status dot, the same marquee title, the same hover card and
// the same ⋯ menu.
//
// The geometry is `TreeRow`'s and not this file's: the chevron column, the glyph column and
// the label all line up with the workspace rows above them because they are literally the same
// row. What a surface chooses is whether its list reserves a disclosure column, which it says
// once for the whole list rather than once per row.
//
// The strings stay in the `SessionsSidebar` namespace. They are the vocabulary of a session
// row, and a second copy under a second name would mean two translations of "Awaiting input"
// that could drift apart while describing the same dot.

import { Box, Flex, IconButton, Menu, Span, Text } from "@chakra-ui/react";
import { useFormatter, useTranslations } from "next-intl";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { LuEllipsis, LuFolderOpen, LuMessagesSquare, LuTrash2 } from "react-icons/lu";
import { DropdownMenu, MenuOption } from "@/components/ui/menu";
import { Tooltip } from "@/components/ui/tooltip";
import { revealInFinder, type AgentSummary, type PermissionMode } from "@/lib/api";
import { PERMISSION_MODES } from "@shared/controls";
import { InlineField } from "./ui/display";
import { TreeRow, type TreeRowDisclosure } from "./ui/tree-row";

// What a session is doing, as the daemon derives it. Distinct from whether it *exists*,
// which is `lifecycle` and is the durable half: a session with no process is asleep, not
// gone, and the next message to it forks a new worker in about 60ms.
export type SessionActivity = "working" | "waiting" | "idle" | "asleep" | "ended";

export interface SessionEntry {
  sessionId: string;
  // The session that created this one, empty for a session the user started. A session
  // composes by creating peers, and this is the edge the tree panel is built out of.
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

// Extra left-shift, beyond the raw overflow, so a fully-scrolled title comes to rest with
// its end clear of the row's trailing ⋯ actions rather than sliding underneath them. The
// button is 32px wide, so this is that plus a margin, and it matches the clear zone of the
// hover mask in globals.css — the two describe the same edge and drifted apart once already.
const MARQUEE_TAIL_CLEARANCE = 44;

// The hover card. It follows the Git bar's shape — a titled heading with the glyph that
// stands for the thing, then label/value rows — because that is already the vocabulary this
// interface uses for "here is what I know about this", and a second one would only make the
// two harder to read. A row's own tooltip used to be its title repeated back, which told a
// reader nothing they were not already looking at.
export function SessionHoverCard({
  entry, statusLabel, agents,
}: { entry: SessionEntry; statusLabel: string; agents: AgentSummary[] }) {
  const translation = useTranslations("SessionsSidebar");
  const permissions = useTranslations("SessionControls");
  const format = useFormatter();
  const title = entry.title || translation("untitledConversation");
  const created = new Date(entry.createdAt);
  // Both of these are identifiers on the wire and names on screen. The agent's own name comes
  // from the catalogue rather than from the session row, which only ever stored the id; the
  // permission mode is read out of the one definition every client already builds its controls
  // from, so the sidebar cannot come to call a mode something the picker does not.
  // `title` is the agent's own name; `name` is its slug, and reads as a code beside a
  // human-written conversation title. The same order the agent picker uses.
  const agent = agents.find((candidate) => candidate.id === entry.agent);
  const agentName = agent?.title || agent?.name || entry.agent;
  const permissionKey = PERMISSION_MODES.choices.find((choice) => choice.value === entry.permissionMode)?.labelKey;
  return (
    <Box maxW="320px">
      <Flex align="center" gap={1} mb={1} color="fg">
        <LuMessagesSquare size={12} />
        <Text fontWeight="semibold" truncate>{title}</Text>
      </Flex>
      <Flex direction="column" ps={2} gap={1}>
        <InlineField label={translation("fieldAgent")}><Text>{agentName}</Text></InlineField>
        <InlineField label={translation("fieldStatus")}>
          <Text color={entry.failed ? "red.fg" : entry.awaitingInput ? "yellow.fg" : undefined}>{statusLabel}</Text>
        </InlineField>
        {Number.isNaN(created.getTime()) ? null : (
          <InlineField label={translation("fieldCreated")}>
            <Text>{format.dateTime(created, { year: "numeric", month: "long", day: "numeric", hour: "2-digit", minute: "2-digit" })}</Text>
          </InlineField>
        )}
        {permissionKey ? (
          <InlineField label={translation("fieldPermissions")}>
            <Text>{permissions(permissionKey as Parameters<typeof permissions>[0])}</Text>
          </InlineField>
        ) : null}
        {entry.exitReason ? (
          <InlineField label={translation("fieldExitReason")}><Text color="fg.muted">{entry.exitReason}</Text></InlineField>
        ) : null}
      </Flex>
    </Box>
  );
}

// The status a session's dot reflects. "working" means it is doing something — a soft
// pulsing gray dot, shown even while it's the active session ("not finished yet"). "done"
// means it finished since you last looked — a solid blue dot, suppressed for the active
// session (you're already looking at it). Plus the two alerts: a crashed session, and one
// parked on a decision only you can make.
//
// A sleeping or idle session shows no dot at all — there is nothing to report, and a mark
// against every row would say nothing while making the few that matter harder to find. The
// dot sits at the row's leading edge and takes no space when absent.
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

// A session title that scrolls its overflow on hover (see `.sidebar-title` in globals.css). It
// measures how far the text overruns its box, adds the tail clearance, and hands the CSS both
// the travel distance and a matching duration (a fixed 50px/s, the reference's cadence) so long
// and short titles scroll at the same speed. The mask/animation itself is pure CSS, driven by
// the row hover.
export function MarqueeTitle({ text }: { text: string }) {
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

// The row itself: a status dot, the title, and the ⋯ menu, on the shared tree grid. Clicking
// anywhere that is not the chevron or the menu opens the conversation.
export function SessionRow({
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
  // Present only in a list where something can expand — the delegated-work panel. The sidebar
  // lists conversations that never nest and passes nothing, so it spends no width on a column
  // that would always be empty.
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
    (entry.failed ? "statusFailed" : ACTIVITY_LABEL_KEY[entry.activity]) as Parameters<typeof translation>[0]
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
      disclosureLabel={disclosure === "open" ? translation("hideChildSessions") : translation("showChildSessions")}
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
      // The status rides at the row's trailing edge, with any count the surface passes. Leading,
      // it held a column open on every quiet row and pushed each title in by a slot for a mark
      // most of them never show — and the moment a session started working, the whole list
      // stepped sideways. At the trailing edge it appears and disappears against the margin,
      // and the ⋯ takes its place on hover.
      badges={(badges || indicator) ? (
        <>
          {badges}
          {indicator ? (
            <Tooltip content={statusTooltip} rich openDelay={350} positioning={{ placement: "left" }}>
              <Box
                boxSize="1.5"
                borderRadius="full"
                bg={INDICATOR_COLOR[indicator]}
                className={indicator === "working" ? "status-dot-pulse" : undefined}
              />
            </Tooltip>
          ) : null}
        </>
      ) : undefined}
      actions={
        // No `data-row-actions` marker here: the row owns the reveal, and a second copy of the
        // marker on a *descendant* was hidden by the same rule that shows the slot — so the
        // menu sat inside a visible wrapper with `display: none` of its own, and no amount of
        // hovering produced a ⋯.
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
    >
      {children}
    </TreeRow>
  );
}
