"use client";

import { Flex } from "@chakra-ui/react";
import type { PermissionDecision, QuestionAnswer, ToolEvent } from "@/lib/tool-event";
import type { AgentPart } from "@/lib/use-chat";
import { MarkdownContent } from "./markdown-content";
import { ToolGroup } from "./tool-group";

type TimelineItem =
  | { kind: "text"; content: string }
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
    // tool: gather the contiguous run and always wrap in a ToolGroup so the
    // transition from 1→2 tools is a smooth addition, not a component swap.
    const run: ToolEvent[] = [];
    while (index < parts.length && parts[index].kind === "tool") {
      run.push(parts[index] as ToolEvent);
      index += 1;
    }
    if (run.length > 0) {
      const first = run[0].toolCallId || `tools-${index}`;
      items.push({ kind: "tools", id: first, tools: run });
    }
  }
  return items;
}

export function AgentTimeline({
  parts,
  agents = [],
  onPermission,
  onQuestion,
}: {
  parts: AgentPart[];
  agents?: { id: string; name: string; title?: string }[];
  // A sub-agent's parked gate is resolved through the same handlers as a root prompt;
  // routing is by request id, so the shared resolve reaches the parked sub-agent runtime.
  onPermission?: (requestId: string, decision: PermissionDecision) => void;
  onQuestion?: (requestId: string, answers: QuestionAnswer[]) => void;
}) {
  const items = buildTimelineItems(parts);
  return (
    // The enclosing DisclosureRow body owns the top breathing room now, so this
    // timeline just stacks its items.
    <Flex direction="column" gap={1} align="stretch">
      {items.map((item, itemIndex) => {
        if (item.kind === "text") {
          return <MarkdownContent key={`text-${itemIndex}`} content={item.content} />;
        }
        return (
          <ToolGroup
            key={`tools-${item.id}`}
            tools={item.tools}
            agents={agents}
            onPermission={onPermission}
            onQuestion={onQuestion}
          />
        );
      })}
    </Flex>
  );
}
