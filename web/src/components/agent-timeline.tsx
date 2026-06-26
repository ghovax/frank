"use client";

import { Flex } from "@chakra-ui/react";
import { Fragment } from "react";
import type { AgentPart } from "@/lib/use-chat";
import { MarkdownContent } from "./markdown-content";
import { ToolCall } from "./tool-call";
import { ToolResultView } from "./tool-views";

function isBackgroundStarted(result: unknown): boolean {
  if (typeof result !== "object" || result === null) return false;
  const code = (result as Record<string, unknown>).code;
  if (typeof code !== "string") return false;
  return code.endsWith("_started") || code === "background_task_scheduled";
}

// Renders an agent step's ordered timeline — prose blocks and tool calls
// interleaved exactly as they occurred. The same building blocks the main chat
// uses (MarkdownContent for text, ToolCall for tool calls), so an agent's
// activity reads identically whether shown inline or in the agents panel.
export function AgentTimeline({
  parts,
  agents = [],
}: {
  parts: AgentPart[];
  agents?: { name: string; label: string }[];
}) {
  return (
    <Flex direction="column" gap={1.5} align="stretch">
      {parts.map((part, index) =>
        part.kind === "text" ? (
          <MarkdownContent key={`text-${index}`} content={part.content} />
        ) : (
          <Fragment key={`tool-${index}`}>
            <ToolCall
              name={part.name}
              arguments={part.arguments}
              sequenceNumber={part.sequenceNumber}
              status={part.result != null && !isBackgroundStarted(part.result) ? "completed" : undefined}
              agents={agents}
            />
            {part.result != null && !isBackgroundStarted(part.result) && (
              <ToolResultView
                name={part.name}
                content={typeof part.result === "string" ? part.result : JSON.stringify(part.result)}
              />
            )}
          </Fragment>
        )
      )}
    </Flex>
  );
}
