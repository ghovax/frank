"use client";

import { Flex } from "@chakra-ui/react";
import type { ToolEvent } from "@/lib/tool-event";
import type { AgentPart } from "@/lib/use-chat";
import { MarkdownContent } from "./markdown-content";
import { ToolCall } from "./tool-call";
import { ToolGroup } from "./tool-group";

type TimelineItem =
  | { kind: "text"; content: string }
  | { kind: "tool"; tool: ToolEvent }
  | { kind: "tools"; id: string; tools: ToolEvent[] };

// Renders an agent step's ordered timeline, mirroring the main chat's decisions:
// reasoning is not shown as interleaved cards (it's a live status elsewhere), and
// contiguous tool calls collapse into the same grouped/stacked ToolGroup the chat
// uses — so an agent's activity reads identically in the panel and inline.
function buildTimelineItems(parts: AgentPart[]): TimelineItem[] {
  const items: TimelineItem[] = [];
  let index = 0;
  while (index < parts.length) {
    const part = parts[index];
    // Thinking is never rendered as a row — same as the chat timeline.
    if (part.kind === "thinking") {
      index += 1;
      continue;
    }
    if (part.kind === "text") {
      items.push({ kind: "text", content: part.content });
      index += 1;
      continue;
    }
    // tool: gather the contiguous run. A lone call renders bare (like the chat);
    // two or more collapse into a ToolGroup.
    const run: ToolEvent[] = [];
    while (index < parts.length && parts[index].kind === "tool") {
      run.push(parts[index] as ToolEvent);
      index += 1;
    }
    if (run.length === 1) {
      items.push({ kind: "tool", tool: run[0] });
    } else if (run.length > 1) {
      // Key by the first tool only so the group stays mounted as tools stream in
      // (a first+last key would remount on every new tool and replay animations).
      const first = run[0].toolCallId || `tools-${index}`;
      items.push({ kind: "tools", id: first, tools: run });
    }
  }
  return items;
}

export function AgentTimeline({
  parts,
  agents = [],
}: {
  parts: AgentPart[];
  agents?: { id: string; name: string; title?: string }[];
}) {
  const items = buildTimelineItems(parts);
  return (
    <Flex direction="column" gap={1.5} align="stretch">
      {items.map((item, itemIndex) => {
        if (item.kind === "text") {
          return <MarkdownContent key={`text-${itemIndex}`} content={item.content} />;
        }
        if (item.kind === "tool") {
          const tool = item.tool;
          return (
            <ToolCall
              key={`tool-${tool.toolCallId || itemIndex}`}
              name={tool.name}
              arguments={tool.arguments}
              result={tool.result}
              toolCallId={tool.toolCallId}
              status={tool.status}
              permission={tool.permission}
              question={tool.question}
              agents={agents}
            />
          );
        }
        return <ToolGroup key={`tools-${item.id}`} tools={item.tools} agents={agents} />;
      })}
    </Flex>
  );
}
