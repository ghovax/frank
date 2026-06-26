"use client";

import { Flex } from "@chakra-ui/react";
import type { AgentPart } from "@/lib/use-chat";
import { MarkdownContent } from "./markdown-content";
import { ToolCall } from "./tool-call";

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
          <ToolCall
            key={`tool-${index}`}
            name={part.name}
            arguments={part.arguments}
            sequenceNumber={part.sequenceNumber}
            agents={agents}
          />
        )
      )}
    </Flex>
  );
}
