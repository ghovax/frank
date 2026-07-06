"use client";

import { Badge, Box, Flex, Text } from "@chakra-ui/react";
import { memo, useEffect, useMemo, useRef, useState } from "react";
import { LuBrain, LuChevronDown, LuChevronRight } from "react-icons/lu";
import { AnimatePresence, motion } from "motion/react";
import { getToolCallDisplay } from "@/lib/tool-display";
import { iconForFilePath } from "@/lib/file-icons";
import { DiffStatBadge, RollingNumber } from "./rolling-number";
import { ToolCallLabel } from "./tool-label";
import type { PermissionDecision, QuestionAnswer, ToolEvent, ToolEventStatus } from "@/lib/tool-event";
import { ToolCall } from "./tool-call";

// Shared, grouped/collapsible stack of contiguous tool calls — the single source
// of truth for how a run of tool calls reads, used by both the chat timeline and
// the agents panel so the two stay in sync. A full-height left marker brackets
// the group; the header surfaces the most recent call; the body is a stack of
// motion cards (a new call slides in, earlier ones settle behind). The group is
// open while any call is running and auto-collapses when the batch completes
// (a manual click is remembered as an override).
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
  activePreviewId?: string | null;
  onActivatePreview?: (toolCallId: string) => void;
  // When true, the group stays expanded even after all calls complete — used by
  // the chat timeline to keep the latest group open until the assistant's text
  // response actually arrives, rather than collapsing the instant tools finish.
  keepOpen?: boolean;
  // How many reasoning ("thinking") phases happened while this batch of work ran.
  // Surfaced as a brain counter in the header (persisted: replay rebuilds the
  // thinking messages this is counted from). 0 hides it.
  thinkingCount?: number;
}

export const ToolGroup = memo(function ToolGroup({
  tools,
  agents = [],
  onPermission,
  onQuestion,
  activePreviewId,
  onActivatePreview,
  keepOpen = false,
  thinkingCount = 0,
}: ToolGroupProps) {
  const runningCount = tools.filter((tool) => toolStatus(tool.status) === "running").length;
  const inputRequired = tools.some((tool) => toolStatus(tool.status) === "input_required");
  const failedCount = tools.filter((tool) => toolStatus(tool.status) === "failed").length;
  const active = runningCount > 0 || inputRequired || keepOpen;
  const [manualOverride, setManualOverride] = useState<boolean | null>(null);
  const bodyOpen = manualOverride ?? false;
  const bodyRef = useRef<HTMLDivElement>(null);

  // Auto-scroll the body to the bottom when new tool calls arrive.
  useEffect(() => {
    if (bodyOpen && bodyRef.current) {
      bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
    }
  }, [tools.length, bodyOpen]);

  // Extract file changes from tool arguments for the right side of the header: when
  // the group includes file operations (edit_file, write_file) it shows the changed
  // files with their extension icons and diff stats, alongside the status badge.
  const fileChanges = useMemo(() => extractFileChanges(tools), [tools]);
  const hasFileChanges = fileChanges.length > 0;
  // The left side is a live recap: one icon per distinct tool with its invocation
  // count, preceded by a live status line that shimmers while active.
  const tally = useMemo(() => tallyTools(tools), [tools]);
  // The status line shows the most recent tool's own label (its justification),
  // animated as work streams in and left in place when the batch finishes — more
  // informative than a static "Still working" / "Actions taken".
  const latestTool = tools[tools.length - 1];
  const latestLabel = latestTool ? getToolCallDisplay(latestTool.name, latestTool.arguments).label : "";
  const headingText = latestLabel || (active ? "Working" : "Actions taken");

  const badge = inputRequired
    ? { label: "Input required", colorPalette: "yellow" }
    : failedCount > 0
      ? { label: `${failedCount} failed`, colorPalette: "red" }
      : runningCount > 0
        ? { label: `${runningCount} running`, colorPalette: "blue" }
        : null;

  return (
    <Box alignSelf="flex-start" w="100%">
      {/* Rendered as a proper card (same shell as a single ToolCall) so the group
          reads as a first-class entry in the timeline, not a bare label. The border
          tint tracks activity; the body is recessed (bg) so the nested tool cards
          raise against it. */}
      <Box
        borderRadius="sm"
        overflow="hidden"
        bg="bg.subtle"
        border="1px solid"
        borderColor="border"
      >
        <Flex
          as="button"
          align="center"
          gap={1.5}
          w="100%"
          px={2}
          py={1.5}
          minH="8"
          color="fg"
          textAlign="left"
          cursor="pointer"
          _hover={{ bg: "bg.muted" }}
          onClick={() => setManualOverride((current) => current === null ? true : !current)}
        >
          <Flex align="center" gap={2} flex={1} minW={0}>
            {/* Animated status line — the latest tool's label slides in as work
                streams and stays put when it finishes. Fixed single-line height so
                the vertical slide is clipped rather than nudging the row. */}
            <Box minW={0} flexShrink={1} overflow="hidden" h="1.15em" display="flex" alignItems="center">
              <AnimatePresence mode="wait" initial={false}>
                <motion.div
                  key={headingText}
                  initial={{ opacity: 0, y: 5 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, y: -5 }}
                  transition={{ duration: 0.16, ease: [0.4, 0, 0.2, 1] }}
                  style={{ minWidth: 0, maxWidth: "100%" }}
                >
                  <Text
                    fontSize="xs"
                    fontWeight="medium"
                    whiteSpace="nowrap"
                    overflow="hidden"
                    textOverflow="ellipsis"
                    // While active, leave the color unset so the shimmer class controls it:
                    // an inline color would override the gradient's transparent fill (inline
                    // beats class) and the shimmer would silently not render.
                    color={active ? undefined : "fg.muted"}
                    className={active ? "running-title-shimmer" : undefined}
                  >
                    {latestTool ? <ToolCallLabel name={latestTool.name} args={latestTool.arguments} /> : headingText}
                  </Text>
                </motion.div>
              </AnimatePresence>
            </Box>
            <Flex align="center" gap={1.5} flexShrink={0} flexWrap="wrap">
              <AnimatePresence initial={false}>
                {tally.order.map((name) => {
                  const display = getToolCallDisplay(name);
                  const ToolIcon = display.icon;
                  const count = tally.counts.get(name) ?? 0;
                  return (
                    <motion.div
                      key={name}
                      layout
                      initial={{ opacity: 0, scale: 0.5 }}
                      animate={{ opacity: 1, scale: 1 }}
                      exit={{ opacity: 0, scale: 0.5 }}
                      transition={{ duration: 0.18, ease: [0.34, 1.56, 0.64, 1] }}
                      style={{ display: "inline-flex" }}
                    >
                      <Flex
                        align="center"
                        gap={1}
                        flexShrink={0}
                        title={display.label}
                        color={active ? display.iconColor : "fg.muted"}
                      >
                        <ToolIcon size={13} />
                        {count > 1 && (
                          <Box as="span" display="inline-flex" alignItems="center" lineHeight="1" fontSize="xs" fontWeight="medium" color="fg.muted">
                            <RollingNumber value={count} />
                          </Box>
                        )}
                      </Flex>
                    </motion.div>
                  );
                })}
              </AnimatePresence>
              {/* Reasoning counter — how many times the agent stopped to think
                  while doing this batch. Same rolling-number treatment as the
                  tool counts, in the thinking (purple) accent. */}
              {thinkingCount > 0 && (
                <Flex
                  align="center"
                  gap={1}
                  flexShrink={0}
                  color={active ? "purple.fg" : "fg.muted"}
                  title={`Thought ${thinkingCount} time${thinkingCount === 1 ? "" : "s"}`}
                >
                  <LuBrain size={13} />
                  {thinkingCount > 1 && (
                    <Box as="span" display="inline-flex" alignItems="center" lineHeight="1" fontSize="xs" fontWeight="medium" color="fg.muted">
                      <RollingNumber value={thinkingCount} />
                    </Box>
                  )}
                </Flex>
              )}
            </Flex>
          </Flex>
          {hasFileChanges && fileChanges.length > 0 && (
            <Flex align="center" gap={1.5} flexShrink={1} minW={0} overflow="hidden">
              {fileChanges.slice(0, 3).map((file) => {
                const FileIcon = iconForFilePath(file.path).icon;
                return (
                  <Flex key={file.path} align="center" gap={1.5} minW={0} flexShrink={1}>
                    <Box color="fg.muted" display="flex" alignItems="center" flexShrink={0}>
                      <FileIcon size={13} />
                    </Box>
                    <Text fontSize="xs" fontWeight="medium" truncate>
                      {file.path.split("/").pop() ?? file.path}
                    </Text>
                    <DiffStatBadge additions={file.additions} deletions={file.deletions} />
                  </Flex>
                );
              })}
              {fileChanges.length > 3 && (
                <Badge size="sm" variant="surface" colorPalette="gray" borderRadius="sm" flexShrink={0}>
                  {fileChanges.length} files
                </Badge>
              )}
            </Flex>
          )}
          {badge && (
            <Badge size="sm" variant="subtle" colorPalette={badge.colorPalette} borderRadius="sm" flexShrink={0}>
              {badge.label}
            </Badge>
          )}
          <Box color="fg.muted" fontSize="xs" flexShrink={0}>
            {bodyOpen ? <LuChevronDown size={12} /> : <LuChevronRight size={12} />}
          </Box>
        </Flex>
        <AnimatePresence initial={false}>
          {bodyOpen && (
            <motion.div
              key="body"
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: "auto" }}
              exit={{ opacity: 0, height: 0 }}
              transition={{ duration: 0.2, ease: [0.4, 0, 0.2, 1] }}
              style={{ overflow: "hidden" }}
            >
              <Box ref={bodyRef} borderTop="1px solid" borderColor="border" bg="bg" px={2} py={2} maxH="320px" overflowY="auto">
                <Flex direction="column" gap={1.5}>
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
                      activePreviewId={activePreviewId}
                      onActivatePreview={onActivatePreview}
                    />
                  ))}
                </Flex>
              </Box>
            </motion.div>
          )}
        </AnimatePresence>
      </Box>
    </Box>
  );
});
