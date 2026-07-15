"use client";

import { Box, Flex, Text } from "@chakra-ui/react";
import { memo, useMemo, useState, type ReactNode } from "react";
import { LuBrain } from "react-icons/lu";
import { AnimatePresence, motion } from "motion/react";
import { useTranslations } from "next-intl";
import { getToolCallDisplay } from "@/lib/tool-display";
import { iconForFilePath } from "@/lib/file-icons";
import { STATUS_ICON, STATUS_PALETTE, type StatusKind } from "@/lib/status";
import { DiffStatBadge, RollingNumber } from "./rolling-number";
import { ToolCallLabel } from "./tool-label";
import { Pill } from "./ui/pill";
import { DisclosureRow } from "./ui/disclosure-row";
import { ActivityIcon, ActivitySpinner } from "./ui/activity-icon";
import type { PermissionDecision, QuestionAnswer, ToolEvent } from "@/lib/tool-event";
import { isBackgroundResult, toolStatus } from "@/lib/tool-event";
import { ToolCall, ToolLocationBadge, collapsedHeadingLocation } from "./tool-call";

// Shared, grouped/collapsible run of contiguous tool calls — the single source
// of truth for how a batch of tool calls reads, used by both the chat timeline
// and the agents panel so the two stay in sync. The group is a single line of
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
  agents?: { id: string; name: string; title?: string }[];
  onPermission?: (requestId: string, decision: PermissionDecision) => void;
  onQuestion?: (requestId: string, answers: QuestionAnswer[]) => void;
  activeArtifactId?: string | null;
  onActivateArtifact?: (toolCallId: string) => void;
  // When true, the group stays expanded even after all calls complete — used by
  // the chat timeline to keep the latest group open until the assistant's text
  // response actually arrives, rather than collapsing the instant tools finish.
  keepOpen?: boolean;
}

export const ToolGroup = memo(function ToolGroup({
  tools,
  agents = [],
  onPermission,
  onQuestion,
  activeArtifactId,
  onActivateArtifact,
  keepOpen = false,
}: ToolGroupProps) {
  const t = useTranslations("ToolGroup");
  const backgroundCount = tools.filter(
    (tool) => toolStatus(tool.status) === "running" && isBackgroundResult(tool.result),
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
  // The status line shows the most recent tool's own label (its justification),
  // animated as work streams in and left in place when the batch finishes — more
  // informative than a static "Still working" / "Actions taken".
  const latestTool = tools[tools.length - 1];
  const headingDisplay = latestTool ? getToolCallDisplay(latestTool.name, latestTool.arguments) : null;
  const HeadingIcon = headingDisplay?.icon ?? LuBrain;
  const headingIconColor = headingDisplay?.iconColor ?? "purple.fg";
  // When the batch touched a single remote place, badge the collapsed heading with it
  // (local-only batches show nothing — local is the implied default).
  const groupLocation = useMemo(() => collapsedHeadingLocation(tools.map((tool) => tool.arguments)), [tools]);
  const latestLabel = latestTool ? getToolCallDisplay(latestTool.name, latestTool.arguments).label : "";
  // A tools-less group is a "thinking before acting" phase and owns the leading brain icon.
  const thinkingOnly = tools.length === 0;
  const headingText = latestLabel || (thinkingOnly ? t("thinking") : active ? t("working") : t("actionsTaken"));
  // One call is already fully represented by the summary row. The grouped body only
  // becomes useful once it can reveal multiple calls instead of repeating that row.
  const interactive = tools.length > 1;

  // Status chips: one colored icon (+ count) per state, in the same visual grammar as the
  // tool tally beside them, readable at a glance with no prose badge to parse. The colour
  // and glyph come from the shared status table, so a status reads the same here as on an
  // individual call. Completed calls carry no chip; the settled line speaks for itself.
  const statusChips = [
    inputRequiredCount > 0 && { kind: "input_required" as StatusKind, count: inputRequiredCount, title: t("inputRequired") },
    failedCount > 0 && { kind: "failed" as StatusKind, count: failedCount, title: t("failedCount", { count: failedCount }) },
    runningCount > 0 && { kind: "running" as StatusKind, count: runningCount, title: t("runningCount", { count: runningCount }) },
    backgroundCount > 0 && { kind: "background" as StatusKind, count: backgroundCount, title: t("backgroundCount", { count: backgroundCount }) },
  ].filter((chip): chip is { kind: StatusKind; count: number; title: string } => Boolean(chip));

  // The animated label slot: the latest tool's label crossfades as work streams in,
  // with both labels in the same grid cell so nothing reflows, and shimmers while active.
  // `minmax(0,1fr)` lets it truncate with an ellipsis.
  const titleSlot = (
    <Box minW={0} display="grid" gridTemplateColumns="minmax(0, 1fr)" position="relative">
      <AnimatePresence initial={false} mode="popLayout">
        <motion.div
          key={headingText}
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0, transition: { duration: 0.1, ease: "easeOut" } }}
          transition={{ duration: 0.22, ease: "easeOut" }}
          style={{ gridArea: "1 / 1", minWidth: 0, display: "flex", alignItems: "center" }}
        >
          <Text
            textStyle="sm"
            fontWeight="normal"
            whiteSpace="nowrap"
            overflow="hidden"
            textOverflow="ellipsis"
            // While active, leave the color unset so the shimmer class controls it: an
            // inline color would override the gradient's transparent fill and the shimmer
            // would silently not render.
            className={active ? "running-title-shimmer" : undefined}
          >
            {latestTool ? <ToolCallLabel name={latestTool.name} args={latestTool.arguments} /> : headingText}
          </Text>
        </motion.div>
      </AnimatePresence>
    </Box>
  );

  // The heading's chip cluster: prior-tool tallies, any file-change chip, the remote
  // badge, and status chips — all animated in/out.
  const badgeSlot = (
    <>
      <AnimatePresence initial={false}>
        {tally.order.map((name) => {
          const display = getToolCallDisplay(name);
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
          <Pill colorPalette="gray">{t("filesCount", { count: fileChanges.length })}</Pill>
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
                icon={kind === "running"
                  ? <ActivitySpinner color={`${palette}.fg`} />
                  : ChipIcon ? <ChipIcon /> : null}
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
        badges={badgeSlot}
      >
        {interactive ? (
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
                agents={agents}
                onPermission={onPermission}
                onQuestion={onQuestion}
                activeArtifactId={activeArtifactId}
                onActivateArtifact={onActivateArtifact}
              />
            ))}
          </Flex>
        ) : undefined}
      </DisclosureRow>
    </Box>
  );
});
