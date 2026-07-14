"use client";

import { Badge, Box, Flex, Text } from "@chakra-ui/react";
import { memo, useEffect, useMemo, useRef, useState } from "react";
import { LuBrain, LuChevronDown, LuChevronRight, LuCircleAlert, LuCircleX, LuLoaderCircle, LuMoon } from "react-icons/lu";
import { AnimatePresence, motion } from "motion/react";
import { useTranslations } from "next-intl";
import { getToolCallDisplay } from "@/lib/tool-display";
import { iconForFilePath } from "@/lib/file-icons";
import { DiffStatBadge, RollingNumber } from "./rolling-number";
import { ToolCallLabel } from "./tool-label";
import type { PermissionDecision, QuestionAnswer, ToolEvent, ToolEventStatus } from "@/lib/tool-event";
import { ToolCall, ToolLocationBadge, collapsedHeadingLocation, isBackgroundResult } from "./tool-call";

// Shared, grouped/collapsible run of contiguous tool calls — the single source
// of truth for how a batch of tool calls reads, used by both the chat timeline
// and the agents panel so the two stay in sync. The group is a single line of
// text in the transcript: a leading disclosure caret, the most recent call's
// label (shimmering while live), then a compact tally of the tools used and any
// status/file chips — all hugging the text like a sentence, not a card. Opening
// it hangs the individual call lines off a hairline left rule, the same visual
// grammar the calls themselves (and markdown blockquotes) use.
function toolStatus(status: unknown): ToolEventStatus | undefined {
  return status === "running" || status === "completed" || status === "done" || status === "failed" || status === "input_required" ? status : undefined;
}

// Tally the tools by name, preserving first-seen order (so the recap reads in the
// order work actually happened): each distinct tool becomes one icon plus how many
// times it was invoked.
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

// The small tally count beside a tool/reasoning/status icon in the group heading.
// One shared chip so every count reads with the same size and weight (fieldLabel),
// tinted by its icon's accent.
function CountChip({ value, color }: { value: number; color: string }) {
  return (
    <Box as="span" display="inline-flex" alignItems="center" lineHeight="1" textStyle="fieldLabel" color={color}>
      <RollingNumber value={value} />
    </Box>
  );
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
  const [manualOverride, setManualOverride] = useState<boolean | null>(null);
  const bodyOpen = manualOverride ?? false;
  const bodyRef = useRef<HTMLDivElement>(null);

  // Auto-scroll the body to the bottom when new tool calls arrive.
  useEffect(() => {
    if (bodyOpen && bodyRef.current) {
      bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
    }
  }, [tools.length, bodyOpen]);

  // Extract file changes from tool arguments for the heading: when the group includes
  // file operations (edit_file, write_file) it shows the changed file with its
  // extension icon and diff stat, alongside the status chips.
  const fileChanges = useMemo(() => extractFileChanges(tools), [tools]);
  const hasFileChanges = fileChanges.length > 0;
  // After the caret, a live recap: one icon per distinct tool with its invocation
  // count, preceded by a live status line that shimmers while active.
  const tally = useMemo(() => tallyTools(tools), [tools]);
  // The status line shows the most recent tool's own label (its justification),
  // animated as work streams in and left in place when the batch finishes — more
  // informative than a static "Still working" / "Actions taken".
  const latestTool = tools[tools.length - 1];
  // When the batch touched a single remote place, badge the collapsed heading with it
  // (local-only batches show nothing — local is the implied default).
  const groupLocation = useMemo(() => collapsedHeadingLocation(tools.map((tool) => tool.arguments)), [tools]);
  const latestLabel = latestTool ? getToolCallDisplay(latestTool.name, latestTool.arguments).label : "";
  // A tools-less group is a live "thinking before acting" phase. Reasoning is
  // transient — a shimmering status while it happens, gone once the turn moves
  // on — unlike tool calls, which are records that stay. So it renders only
  // while live (keepOpen); a settled thinking-only group renders nothing.
  const thinkingOnly = tools.length === 0;
  const headingText = latestLabel || (thinkingOnly ? t("thinking") : active ? t("working") : t("actionsTaken"));
  // A thinking-only heading has no body to reveal, so it is not interactive.
  const interactive = !thinkingOnly;

  // Status chips: one colored icon (+ count) per state, in the same visual grammar as the
  // tool tally beside them, readable at a glance with no prose badge to parse. Completed
  // calls carry no chip; the settled line speaks for itself.
  const statusChips = [
    inputRequiredCount > 0 && {
      key: "input", Icon: LuCircleAlert, color: "yellow.fg",
      count: inputRequiredCount, title: t("inputRequired"),
    },
    failedCount > 0 && {
      key: "failed", Icon: LuCircleX, color: "red.fg",
      count: failedCount, title: t("failedCount", { count: failedCount }),
    },
    runningCount > 0 && {
      key: "running", Icon: LuLoaderCircle, color: "blue.fg", spin: true,
      count: runningCount, title: t("runningCount", { count: runningCount }),
    },
    backgroundCount > 0 && {
      key: "background", Icon: LuMoon, color: "purple.fg",
      count: backgroundCount, title: t("backgroundCount", { count: backgroundCount }),
    },
  ].filter((chip): chip is { key: string; Icon: typeof LuCircleX; color: string; count: number; title: string; spin?: boolean } => Boolean(chip));

  if (thinkingOnly && !active) return null;

  return (
    <Box alignSelf="flex-start" w="100%" minW={0}>
      {/* One text-height line, only as wide as its content — the same fit-content
          treatment as a ToolCall line, so the click target and hover brighten stop
          where the text does. The heading's base color carries the settled tone;
          the caret and label inherit it so hover is a single rule. */}
      <Flex
        as={interactive ? "button" : "div"}
        align="center"
        gap={1.5}
        w="fit-content"
        maxW="100%"
        minW={0}
        h={6}
        color={active ? "fg" : "fg.muted"}
        textAlign="left"
        cursor={interactive ? "pointer" : "default"}
        _hover={interactive ? { color: "fg" } : undefined}
        onClick={interactive ? () => setManualOverride((current) => current === null ? true : !current) : undefined}
      >
        {/* Leading slot: the disclosure caret (this line unfolds into its calls), or
            the reasoning glyph for a thinking-only phase that has nothing to unfold. */}
        <Box display="flex" alignItems="center" flexShrink={0} color={thinkingOnly ? "purple.fg" : "inherit"} opacity={thinkingOnly ? 1 : 0.7}>
          {thinkingOnly ? <LuBrain size={13} /> : bodyOpen ? <LuChevronDown size={12} /> : <LuChevronRight size={12} />}
        </Box>
        {/* Status line — the latest tool's label. When the justification changes, the new
            label blurs and fades in (the same token-in look as the streaming message, applied
            to the whole label since it swaps as a unit — per-token spans would break this
            single-line ellipsis), while the outgoing one fades out. The crossfade keeps the
            entering and exiting labels in the SAME grid cell (both at gridArea 1/1) so they
            overlap without reflow, while the cell sizes itself to one line of text — so the box
            gets its height naturally from its content, no hand-tuned minH. `minmax(0,1fr)` lets
            the single column shrink so the label truncates with an ellipsis. `initial={false}`
            means the first label just appears; only a change animates. */}
        <Box minW={0} flexShrink={1} overflow="hidden" display="grid" gridTemplateColumns="minmax(0, 1fr)">
          <AnimatePresence initial={false}>
            <motion.div
              key={headingText}
              initial={{ opacity: 0, filter: "blur(2px)" }}
              animate={{ opacity: 1, filter: "blur(0px)" }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.22, ease: "easeOut" }}
              style={{ gridArea: "1 / 1", minWidth: 0, display: "flex", alignItems: "center" }}
            >
              <Text
                textStyle="fieldLabel"
                whiteSpace="nowrap"
                overflow="hidden"
                textOverflow="ellipsis"
                // While active, leave the color unset so the shimmer class controls it:
                // an inline color would override the gradient's transparent fill (inline
                // beats class) and the shimmer would silently not render.
                className={active ? "running-title-shimmer" : undefined}
              >
                {latestTool ? <ToolCallLabel name={latestTool.name} args={latestTool.arguments} /> : headingText}
              </Text>
            </motion.div>
          </AnimatePresence>
        </Box>
        <Flex align="center" gap={1.5} flexShrink={0}>
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
                  <Flex
                    align="center"
                    gap={1}
                    flexShrink={0}
                    title={display.label}
                    color={active ? display.iconColor : "fg.muted"}
                  >
                    <ToolIcon size={13} />
                    {count > 1 && <CountChip value={count} color="fg.muted" />}
                  </Flex>
                </motion.div>
              );
            })}
          </AnimatePresence>
        </Flex>
        {hasFileChanges && fileChanges.length > 0 && (
          <Flex align="center" gap={1.5} flexShrink={0} minW={0} overflow="hidden">
            {fileChanges.length === 1 ? fileChanges.map((file) => {
              const FileIcon = iconForFilePath(file.path).icon;
              return (
                <Flex key={file.path} align="center" gap={1.5} minW={0} maxW="180px">
                  <Box color="fg.muted" display="flex" alignItems="center" flexShrink={0}>
                    <FileIcon size={13} />
                  </Box>
                  <Text textStyle="fieldLabel" truncate>
                    {file.path.split("/").pop() ?? file.path}
                  </Text>
                  <DiffStatBadge additions={file.additions} deletions={file.deletions} />
                </Flex>
              );
            }) : (
              <Badge size="sm" variant="subtle" colorPalette="gray" borderRadius="sm" flexShrink={0}>
                {t("filesCount", { count: fileChanges.length })}
              </Badge>
            )}
          </Flex>
        )}
        <ToolLocationBadge arguments={groupLocation} />
        <AnimatePresence initial={false}>
          {statusChips.map(({ key, Icon: ChipIcon, color, count, title, spin }) => (
            <motion.div
              key={key}
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.12, ease: "easeOut" }}
              style={{ display: "inline-flex", alignItems: "center" }}
            >
              <Flex align="center" gap={1} flexShrink={0} title={title} color={color}>
                <ChipIcon size={13} className={spin ? "tool-status-spin" : undefined} />
                {count > 1 && <CountChip value={count} color={color} />}
              </Flex>
            </motion.div>
          ))}
        </AnimatePresence>
      </Flex>
      {bodyOpen && interactive && (
        // The unfolded batch: call lines stacked against a hairline rule, indented so
        // the rule sits under the caret above. Bounded height keeps a long batch from
        // owning the viewport; the auto-scroll effect follows new calls streaming in.
        <Box
          ref={bodyRef}
          ml="5px"
          mt={0.5}
          mb={1}
          pl={3}
          borderLeft="2px solid"
          borderColor="border.muted"
          maxH={80}
          overflowY="auto"
        >
          <Flex direction="column" gap={0.5}>
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
        </Box>
      )}
    </Box>
  );
});
