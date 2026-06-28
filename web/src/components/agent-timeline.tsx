"use client";

import { Flex } from "@chakra-ui/react";
import type { AgentPart } from "@/lib/use-chat";
import { MarkdownContent } from "./markdown-content";
import { ThinkingIndicator } from "./thinking-indicator";
import { ToolCall } from "./tool-call";

// Renders an agent step's ordered timeline — prose, reasoning, and tool calls
// interleaved exactly as they occurred. The same building blocks the main chat
// uses (MarkdownContent for text, ThinkingIndicator for reasoning,
// ToolCall for tool calls), so an agent's activity reads identically whether
// shown inline or in the agents panel.
export function AgentTimeline({
  parts,
  agents = [],
}: {
  parts: AgentPart[];
  agents?: { id: string; name: string; title?: string }[];
}) {
  return (
    <Flex direction="column" gap={1.5} align="stretch">
      {parts.map((part, index) =>
        part.kind === "text" ? (
          <MarkdownContent key={`text-${index}`} content={part.content} />
        ) : part.kind === "thinking" ? (
          <ThinkingIndicator key={`thinking-${index}`} content={part.content} status={part.status} durationMs={part.durationMs} />
        ) : (
          <ToolCall
            key={`tool-${index}`}
            name={part.name}
            arguments={part.arguments}
            result={part.result}
            sequenceNumber={part.sequenceNumber}
            status={part.status}
            agents={agents}
          />
        )
      )}
    </Flex>
  );
}
