"use client";

import { Badge, Box, Flex, Text } from "@chakra-ui/react";
import { memo, useState } from "react";
import { LuChevronDown, LuChevronRight, LuWrench } from "react-icons/lu";
import { AnimatePresence, motion } from "motion/react";
import { getToolCallDisplay } from "@/lib/tool-display";
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
  read_file: { past: (n) => (n === 1 ? "read a file" : `read ${n} files`), gerund: "Reading files" },
  find_files: { past: (n) => (n === 1 ? "searched for files" : `searched for files ${n} times`), gerund: "Finding files" },
  search_content: { past: (n) => (n === 1 ? "searched file contents" : `searched file contents ${n} times`), gerund: "Searching content" },
  edit_file: { past: (n) => (n === 1 ? "edited a file" : `edited ${n} files`), gerund: "Editing files" },
  write_file: { past: (n) => (n === 1 ? "wrote a file" : `wrote ${n} files`), gerund: "Writing files" },
  fetch_url: { past: (n) => (n === 1 ? "fetched a URL" : `fetched ${n} URLs`), gerund: "Fetching URLs" },
  open_web_preview: { past: (n) => (n === 1 ? "opened a preview" : `opened ${n} previews`), gerund: "Opening previews" },
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

interface ToolGroupProps {
  tools: ToolEvent[];
  agents?: { id: string; name: string; title?: string }[];
  onPermission?: (requestId: string, decision: PermissionDecision) => void;
  onQuestion?: (requestId: string, answers: QuestionAnswer[]) => void;
  activePreviewId?: string | null;
  onActivatePreview?: (toolCallId: string) => void;
}

export const ToolGroup = memo(function ToolGroup({
  tools,
  agents = [],
  onPermission,
  onQuestion,
  activePreviewId,
  onActivatePreview,
}: ToolGroupProps) {
  const runningCount = tools.filter((tool) => toolStatus(tool.status) === "running").length;
  const inputRequired = tools.some((tool) => toolStatus(tool.status) === "input_required");
  const failedCount = tools.filter((tool) => toolStatus(tool.status) === "failed").length;
  const active = runningCount > 0 || inputRequired;
  const [manualOverride, setManualOverride] = useState<boolean | null>(null);
  const bodyOpen = manualOverride ?? active;

  // The header title is a category-aware summary of the whole batch (carrying the
  // count), not the last call's label. The icon represents the first/dominant
  // category so it matches the first phrase of the summary.
  const summary = summarizeToolGroup(tools, active);
  const dominant = tools[0]
    ? getToolCallDisplay(tools[0].name, tools[0].arguments)
    : { icon: LuWrench, iconColor: "fg.muted" };
  const DominantIcon = dominant.icon;

  const badge = inputRequired
    ? { label: "Input required", colorPalette: "yellow" }
    : failedCount > 0
      ? { label: `${failedCount} failed`, colorPalette: "red" }
      : runningCount > 0
        ? { label: `${runningCount} running`, colorPalette: "blue" }
        : { label: "Completed", colorPalette: "green" };

  return (
    <Box alignSelf="flex-start" w="100%" className="timeline-item">
      {/* Rendered as a proper card (same shell as a single ToolCall) so the group
          reads as a first-class entry in the timeline, not a bare label. The border
          tint tracks activity; the body is recessed (bg) so the nested tool cards
          raise against it. */}
      <Box
        borderRadius="sm"
        overflow="hidden"
        bg="bg.subtle"
        border="1px solid"
        borderColor={active ? "blue.muted" : "border"}
      >
        <Flex
          as="button"
          align="center"
          gap={1.5}
          w="100%"
          px={2}
          py={1.5}
          minH="8"
          color="fg.muted"
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
          <Badge size="sm" variant="subtle" colorPalette={badge.colorPalette} borderRadius="sm" flexShrink={0}>
            {badge.label}
          </Badge>
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
              <Box borderTop="1px solid" borderColor="border" bg="bg" px={2} py={2}>
                <Flex direction="column" gap={1.5}>
                  {tools.map((tool, index) => (
                    <motion.div
                      key={tool.toolCallId || `tool-${index}`}
                      layout
                      initial={{ opacity: 0, y: -6, scale: 0.985 }}
                      animate={{ opacity: 1, y: 0, scale: 1 }}
                      transition={{ type: "spring", stiffness: 420, damping: 30 }}
                    >
                    <ToolCall
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
                    </motion.div>
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
