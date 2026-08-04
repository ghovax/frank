"use client";

import { Box, Flex, Text } from "@chakra-ui/react";
import { memo, useMemo, useState, type ReactNode } from "react";
import { LuBrain } from "react-icons/lu";
import { AnimatePresence, motion } from "motion/react";
import { useTranslations } from "next-intl";
import { getToolCallDisplay, type ToolDisplayTranslator } from "@/lib/tool-display";
import { iconForFilePath } from "@/lib/file-icons";
import { STATUS_ICON, STATUS_PALETTE, type StatusKind } from "@/lib/status";
import { DiffStatBadge, RollingNumber } from "./rolling-number";
import { ToolCallLabel } from "./tool-label";
import { Pill } from "./ui/pill";
import { DisclosureRow } from "./ui/disclosure-row";
import { ActivityIcon } from "./ui/activity-icon";
import type { ToolEvent } from "@/lib/tool-event";
import { hasBackgroundJobId, toolStatus } from "@/lib/tool-event";
import { ToolCall, ToolCallDetail, ToolLocationBadge, ToolRiskBadges, collapsedHeadingLocation, toolCallDetail } from "./tool-call";

// Shared, grouped/collapsible run of contiguous tool calls — the single source
// of truth for how a batch of tool calls reads. The group is a single line of
// text in the transcript: the most recent call's icon and label (shimmering while
// live), then a compact tally of the tools used and any
// status/file chips — all hugging the text like a sentence, not a card. Opening
// it hangs the individual call lines off a hairline left rule, the same visual
// grammar the calls themselves (and markdown blockquotes) use.

// Tally tools by name while preserving first-seen order.
function tallyTools(tools: ToolEvent[]): { order: string[]; counts: Map<string, number> } {
  const order: string[] = [];
  const counts = new Map<string, number>();
  for (const tool of tools) {
    const name = tool.name || "unknown";
    if (!counts.has(name)) order.push(name);
    counts.set(name, (counts.get(name) ?? 0) + 1);
  }
  return { order, counts };
}

// One tally chip in the group heading — the shared Pill carrying BOTH the icon and its
// count, so each tool/status reads as a single unit.
function TallyBadge({
  icon,
  count,
  colorPalette = "gray",
  title,
  alwaysShowCount = false,
}: {
  icon: ReactNode;
  count: number;
  colorPalette?: string;
  title?: string;
  alwaysShowCount?: boolean;
}) {
  return (
    <Pill colorPalette={colorPalette} title={title} icon={icon}>
      {alwaysShowCount || count > 1 ? <RollingNumber value={count} /> : null}
    </Pill>
  );
}

// Map a tool's icon color ("blue.fg", "green.fg", … or "fg.muted") to a Chakra colorPalette,
// so each tool's tally badge carries that tool's own accent as its background — not a flat gray.
function paletteFromIconColor(iconColor: string): string {
  return iconColor.endsWith(".fg") ? iconColor.slice(0, -3) : "gray";
}

interface FileChange {
  path: string;
  additions: number;
  deletions: number;
}

// Extract file paths and diff stats from tool arguments. Accumulates changes per
// file across multiple edit_file / write_file calls in the same group.
function extractFileChanges(tools: ToolEvent[]): FileChange[] {
  const changes = new Map<string, FileChange>();
  for (const tool of tools) {
    const filePath = tool.arguments?.file_path as string | undefined;
    if (!filePath) continue;
    const existing = changes.get(filePath) ?? {
      path: filePath,
      additions: 0,
      deletions: 0,
    };
    if (tool.name === "edit_file") {
      const oldStr = (tool.arguments?.old_string as string) ?? "";
      const newStr = (tool.arguments?.new_string as string) ?? "";
      const oldLines = oldStr.split("\n");
      const newLines = newStr.split("\n");
      const oldSet = new Set(oldLines);
      const newSet = new Set(newLines);
      existing.deletions += oldLines.filter((line) => !newSet.has(line)).length;
      existing.additions += newLines.filter((line) => !oldSet.has(line)).length;
    } else if (tool.name === "write_file") {
      const content = (tool.arguments?.content as string) ?? "";
      existing.additions += content.split("\n").length;
    }
    changes.set(filePath, existing);
  }
  return [...changes.values()];
}

interface ToolGroupProps {
  tools: ToolEvent[];
  // When true, the group stays expanded even after all calls complete — used by
  // the chat timeline to keep the latest group open until the assistant's text
  // response actually arrives, rather than collapsing the instant tools finish.
  keepOpen?: boolean;
}

export const ToolGroup = memo(function ToolGroup({
  tools,
  keepOpen = false,
}: ToolGroupProps) {
  const translation = useTranslations("ToolGroup");
  const tDisplay = useTranslations("ToolDisplay") as unknown as ToolDisplayTranslator;
  const backgroundCount = tools.filter(
    (tool) => toolStatus(tool.status) === "running" && hasBackgroundJobId(tool.result),
  ).length;
  const runningCount = tools.filter((tool) => toolStatus(tool.status) === "running").length - backgroundCount;
  const inputRequiredCount = tools.filter((tool) => toolStatus(tool.status) === "input_required").length;
  const inputRequired = inputRequiredCount > 0;
  const failedCount = tools.filter((tool) => toolStatus(tool.status) === "failed").length;
  const active = runningCount > 0 || backgroundCount > 0 || inputRequired || keepOpen;
  // Tri-state so the group can be toggled either way from its auto default: null =
  // never touched (follow the default), else the user's explicit open/closed choice.
  const [manualOverride, setManualOverride] = useState<boolean | null>(null);
  const bodyOpen = manualOverride ?? false;

  // Extract file changes from tool arguments for the heading: when the group includes
  // file operations (edit_file, write_file) it shows the changed file with its
  // extension icon and diff stat, alongside the status chips.
  const fileChanges = useMemo(() => extractFileChanges(tools), [tools]);
  const hasFileChanges = fileChanges.length > 0;
  // The left icon owns the latest call. The trailing tally therefore counts only
  // earlier calls, preventing the latest tool from appearing twice in the same row.
  const tally = useMemo(() => tallyTools(tools.slice(0, -1)), [tools]);
  // The status line shows the most recent tool's own label (its explanation),
  // animated as work streams in and left in place when the batch finishes — more
  // informative than a static "Still working" / "Actions taken".
  const latestTool = tools[tools.length - 1];
  const headingDisplay = latestTool ? getToolCallDisplay(latestTool.name, latestTool.arguments, tDisplay) : null;
  const HeadingIcon = headingDisplay?.icon ?? LuBrain;
  const headingIconColor = headingDisplay?.iconColor ?? "purple.fg";
  // When the batch touched a single remote place, badge the collapsed heading with it
  // (local-only batches show nothing — local is the implied default).
  const groupLocation = useMemo(() => collapsedHeadingLocation(tools.map((tool) => tool.arguments)), [tools]);
  const latestLabel = latestTool ? getToolCallDisplay(latestTool.name, latestTool.arguments, tDisplay).label : "";
  // A tools-less group is a "thinking before acting" phase and owns the leading brain icon.
  const thinkingOnly = tools.length === 0;
  const headingText = latestLabel || (thinkingOnly ? translation("thinking") : active ? translation("working") : translation("actionsTaken"));
  // A group of exactly one call skips the per-call line and opens straight onto that call's
  // detail. The line was a duplicate of the heading — the heading already carries that call's
  // icon, label and location, because with one call it *is* that call — so opening the group
  // showed a row saying what you had just read, with the thing you actually wanted behind a
  // second chevron. One expansion, one thing revealed.
  const soleTool = tools.length === 1 ? tools[0] : null;
  const soleDetail = soleTool ? toolCallDetail(soleTool.name, soleTool.arguments, soleTool.result, soleTool.status) : null;
  // Any call can be opened, including a single one. The rule used to be "more than one",
  // on the reasoning that one call is already represented by the summary row — but the row
  // carries the call's *label*, and the body carries what it did: the script, the output, the
  // error and its traceback. A lone failing call was therefore the one case where none of that
  // could be reached, which is precisely when a person most wants it.
  //
  // A lone call with nothing to show is the exception, and it has to be: with the per-call
  // line gone there is no longer anything to put inside, so an openable group would reveal an
  // empty rail — the very defect `toolCallDetail` exists to decide away.
  const interactive = soleTool ? !!soleDetail?.collapsible : tools.length > 0;

  // Status chips surface states that need separate attention. Running and completed calls
  // carry no chip: the live shimmer already communicates activity, while the settled line
  // speaks for itself.
  const statusChips = [
    inputRequiredCount > 0 && { kind: "input_required" as StatusKind, count: inputRequiredCount, title: translation("inputRequired") },
    failedCount > 0 && { kind: "failed" as StatusKind, count: failedCount, title: translation("failedCount", { count: failedCount }) },
    backgroundCount > 0 && { kind: "background" as StatusKind, count: backgroundCount, title: translation("backgroundCount", { count: backgroundCount }) },
  ].filter((chip): chip is { kind: StatusKind; count: number; title: string } => Boolean(chip));

  // The animated label slot: the latest tool's label crossfades as work streams in,
  // with both labels in the same grid cell so nothing reflows, and shimmers while active.
  // `minmax(0,1fr)` lets it truncate with an ellipsis.
  const titleSlot = (
    // The shimmer belongs to this box, not to the label inside it. A CSS animation restarts
    // whenever its element mounts, and the label below is keyed by its own text so that a new
    // tool crossfades in — so the gradient was starting over partway through every time the
    // batch moved on, which reads as the animation stuttering rather than running. This box
    // outlives every label it shows. `background-clip: text` still paints only glyphs,
    // because it clips to the text of descendants too.
    <Box
      minW={0}
      display="grid"
      gridTemplateColumns="minmax(0, 1fr)"
      position="relative"
      className={active ? "running-title-shimmer" : undefined}
    >
      {/* No cross-fade. This was keyed on the heading text, so every change of the running
          tool's explanation faded the whole line out and a new one in — and during a burst of
          tool calls the explanation changes every second or two, which read as the line
          flickering rather than as anything meaning something. A label that is simply replaced
          is easier to read, and the shimmer on the container above already says work is under
          way, which is the only thing the animation was really communicating. */}
      <Box gridArea="1 / 1" minW={0} display="flex" alignItems="center">
        <Text
          textStyle="sm"
          fontWeight="normal"
          whiteSpace="nowrap"
          overflow="hidden"
          textOverflow="ellipsis"
        >
          {latestTool ? <ToolCallLabel name={latestTool.name} args={latestTool.arguments} /> : headingText}
        </Text>
      </Box>
    </Box>
  );

  // The heading's chip cluster: prior-tool tallies, any file-change chip, the remote
  // badge, and status chips — all animated in/out.
  const hasBadges = tally.order.length > 0 || statusChips.length > 0
    || fileChanges.length > 0 || !!groupLocation || !!soleTool;
  const badgeSlot = (
    <>
      {/* The write/risk markers of a lone call, which used to ride on its own line. That line
          is gone for a one-call group, and these are the one thing on it the heading did not
          already say — so they move up rather than disappearing. They are safety markers; a
          simplification that quietly drops them is not a simplification. */}
      {soleTool ? <ToolRiskBadges name={soleTool.name} arguments={soleTool.arguments} /> : null}
      <AnimatePresence initial={false}>
        {tally.order.map((name) => {
          const display = getToolCallDisplay(name, undefined, tDisplay);
          const ToolIcon = display.icon;
          const count = tally.counts.get(name) ?? 0;
          return (
            <motion.div
              key={name}
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.12, ease: "easeOut" }}
              style={{ display: "inline-flex", alignItems: "center" }}
            >
              <TallyBadge title={display.label} count={count} colorPalette={paletteFromIconColor(display.iconColor)} icon={<ToolIcon />} alwaysShowCount />
            </motion.div>
          );
        })}
      </AnimatePresence>
      {hasFileChanges && fileChanges.length > 0 && (
        fileChanges.length === 1 ? fileChanges.map((file) => {
          const FileIcon = iconForFilePath(file.path).icon;
          return (
            <Flex key={file.path} align="center" gap={1.5} minW={0} maxW="180px">
              <Box color="fg.muted" display="flex" alignItems="center" flexShrink={0}>
                <ActivityIcon><FileIcon /></ActivityIcon>
              </Box>
              <Text textStyle="fieldLabel" truncate>
                {file.path.split("/").pop() ?? file.path}
              </Text>
              <DiffStatBadge additions={file.additions} deletions={file.deletions} />
            </Flex>
          );
        }) : (
          <Pill colorPalette="gray">{translation("filesCount", { count: fileChanges.length })}</Pill>
        )
      )}
      <ToolLocationBadge arguments={groupLocation} />
      <AnimatePresence initial={false}>
        {statusChips.map(({ kind, count, title }) => {
          const palette = STATUS_PALETTE[kind];
          const ChipIcon = STATUS_ICON[kind];
          return (
            <motion.div
              key={kind}
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.12, ease: "easeOut" }}
              style={{ display: "inline-flex", alignItems: "center" }}
            >
              <TallyBadge
                title={title}
                count={count}
                colorPalette={palette}
                icon={ChipIcon ? <ChipIcon /> : null}
              />
            </motion.div>
          );
        })}
      </AnimatePresence>
    </>
  );

  return (
    <Box alignSelf="flex-start" w="100%" minW={0}>
      <DisclosureRow
        open={bodyOpen}
        onOpenChange={interactive ? () => setManualOverride((current) => (current === null ? true : !current)) : undefined}
        tone={active ? "active" : "muted"}
        maxH={80}
        followTailKey={tools.length}
        icon={<Box color={headingIconColor}><HeadingIcon /></Box>}
        title={titleSlot}
        // `undefined`, not an empty fragment, when the group has nothing to badge. A fragment is
        // truthy, so `DisclosureRow` rendered its badge Flex — which carries a gap — and the
        // chevron sat a few pixels right of where it does on a row that genuinely has no badges.
        badges={hasBadges ? badgeSlot : undefined}
      >
        {!interactive ? undefined : soleTool ? (
          <ToolCallDetail
            name={soleTool.name}
            arguments={soleTool.arguments}
            result={soleTool.result}
            toolCallId={soleTool.toolCallId}
            status={soleTool.status}
          />
        ) : (
          <Flex direction="column" gap={1}>
            {tools.map((tool, index) => (
              <ToolCall
                key={tool.toolCallId || `tool-${index}`}
                name={tool.name}
                arguments={tool.arguments}
                result={tool.result}
                toolCallId={tool.toolCallId}
                status={tool.status}
                permission={tool.permission}
                question={tool.question}
              />
            ))}
          </Flex>
        )}
      </DisclosureRow>
    </Box>
  );
});
