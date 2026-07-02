"use client";

import { Badge, Box, Flex, Text } from "@chakra-ui/react";
import { memo, useEffect, useMemo, useRef, useState } from "react";
import { LuChevronDown, LuChevronRight, LuWrench } from "react-icons/lu";
import { AnimatePresence, motion } from "motion/react";
import { getToolCallDisplay } from "@/lib/tool-display";
import { iconForFilePath } from "@/lib/file-icons";
import { DiffStatBadge } from "./rolling-number";
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

// Per-tool phrasing for the group header summary: a past-tense phrase (carrying
// its count) for the completed state, and a gerund for the running state. This is
// what lets a group read as "Searched the web 8 times" or "Ran 2 commands and
// edited a file" instead of a generic "8 tool calls".
const TOOL_SUMMARY: Record<string, { past: (count: number) => string; gerund: string }> = {
  web_search: { past: (n) => (n === 1 ? "searched the web" : `searched the web ${n} times`), gerund: "Searching the web" },
  bash: { past: (n) => (n === 1 ? "ran a command" : `ran ${n} commands`), gerund: "Running commands" },
  read_file: { past: (n) => (n === 1 ? "read a file" : `read files ${n} times`), gerund: "Reading files" },
  find_files: { past: (n) => (n === 1 ? "searched for files" : `searched for files ${n} times`), gerund: "Finding files" },
  search_content: { past: (n) => (n === 1 ? "searched file contents" : `searched file contents ${n} times`), gerund: "Searching content" },
  edit_file: { past: (n) => (n === 1 ? "edited a file" : `edited ${n} files`), gerund: "Editing files" },
  write_file: { past: (n) => (n === 1 ? "wrote a file" : `wrote ${n} files`), gerund: "Writing files" },
  fetch_url: { past: (n) => (n === 1 ? "fetched a URL" : `fetched ${n} URLs`), gerund: "Fetching URLs" },
  open_preview: { past: (n) => (n === 1 ? "opened a preview" : `opened ${n} previews`), gerund: "Opening previews" },
  render_widget: { past: (n) => (n === 1 ? "rendered a widget" : `rendered ${n} widgets`), gerund: "Rendering widgets" },
  spawn_agent: { past: (n) => (n === 1 ? "delegated to an agent" : `delegated to ${n} agents`), gerund: "Delegating to agents" },
  load_skill: { past: (n) => (n === 1 ? "loaded a skill" : `loaded ${n} skills`), gerund: "Loading skills" },
  ask_user: { past: (n) => (n === 1 ? "asked a question" : `asked ${n} questions`), gerund: "Asking" },
  call_mcp_tool: { past: (n) => (n === 1 ? "called an MCP tool" : `called ${n} MCP tools`), gerund: "Calling MCP tools" },
  read_mcp_resource: { past: (n) => (n === 1 ? "read an MCP resource" : `read ${n} MCP resources`), gerund: "Reading MCP resources" },
  read_task: { past: (n) => (n === 1 ? "read a task" : `read ${n} tasks`), gerund: "Reading tasks" },
};
const DEFAULT_TOOL_SUMMARY = {
  past: (n: number) => (n === 1 ? "ran a tool call" : `ran ${n} tool calls`),
  gerund: "Working",
};

// Tally the tools by name, preserving first-seen order (so the summary reads in
// the order work actually happened).
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

function summarizeToolGroup(tools: ToolEvent[], active: boolean): string {
  const { order, counts } = tallyTools(tools);
  if (active) {
    // While running: the gerund of the work underway. A single category is named
    // specifically; a mixed batch falls back to a generic "Working on N…".
    if (order.length === 1) {
      return `${(TOOL_SUMMARY[order[0]] ?? DEFAULT_TOOL_SUMMARY).gerund}…`;
    }
    return `Working on ${tools.length} ${tools.length === 1 ? "tool call" : "tool calls"}…`;
  }
  // Done: compose a past-tense summary that carries the counts, e.g.
  // "Searched the web 8 times" or "Ran 2 commands and edited a file".
  const phrases = order.map((name) => (TOOL_SUMMARY[name] ?? DEFAULT_TOOL_SUMMARY).past(counts.get(name)!));
  const joined = phrases.length === 1
    ? phrases[0]
    : `${phrases.slice(0, -1).join(", ")} and ${phrases[phrases.length - 1]}`;
  return joined.charAt(0).toUpperCase() + joined.slice(1);
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
}

export const ToolGroup = memo(function ToolGroup({
  tools,
  agents = [],
  onPermission,
  onQuestion,
  activePreviewId,
  onActivatePreview,
  keepOpen = false,
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

  // Extract file changes from tool arguments for the file-centric heading. When the
  // group includes file operations (edit_file, write_file) the heading shows the
  // changed files with their extension icons and diff stats; other tools (bash,
  // web_search, etc.) keep the original summary text.
  const fileChanges = useMemo(() => extractFileChanges(tools), [tools]);
  const hasFileChanges = fileChanges.length > 0;
  const summary = summarizeToolGroup(tools, active);
  const dominant = tools[0]
    ? getToolCallDisplay(tools[0].name, tools[0].arguments)
    : { icon: LuWrench, iconColor: "fg.muted" };
  const DominantIcon = dominant.icon;

  const badge = inputRequired
    ? { label: "Input required", colorPalette: "yellow" }
    : failedCount > 0
      ? { label: "Failed", colorPalette: "red" }
      : runningCount > 0
        ? { label: "Running", colorPalette: "blue" }
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
          onClick={() => setManualOverride((current) => !(current ?? active))}
        >
          <Box fontSize="sm" flexShrink={0} color={active ? dominant.iconColor : "fg.muted"}>
            <DominantIcon size={13} />
          </Box>
          <Text
            fontSize="xs"
            fontWeight="medium"
            flex={1}
            minW={0}
            truncate
            className={active ? "running-title-shimmer" : undefined}
          >
            {summary}
          </Text>
          {hasFileChanges && fileChanges.length > 0 && (
            <Flex align="center" gap={2} flexShrink={0} overflow="visible" flexWrap="wrap">
              {fileChanges.map((file) => {
                const FileIcon = iconForFilePath(file.path).icon;
                return (
                  <Flex key={file.path} align="center" gap={1.5} minW={0}>
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
