"use client";

import { Box, Flex } from "@chakra-ui/react";
import { useState } from "react";
import { getToolCallDisplay } from "@/lib/tool-display";
import { ToolArtifacts, ToolCallView, ToolResultView, extractToolArtifacts } from "./tool-views";
import { ToolCard, ToolCardBody, ToolCardHeader, ToolStatusBadge } from "./tool-card";

interface ToolCallProps {
  name: string;
  arguments?: Record<string, unknown>;
  // The tool's result, rendered inside the (collapsed) card body alongside the
  // arguments so it is revealed on expand rather than filling the chat.
  result?: unknown;
  sequenceNumber?: number;
  status?: string;
  agents?: { id: string; name: string; title?: string }[];
}

export function ToolCall({ name, arguments: toolArguments, result, sequenceNumber, status, agents = [] }: ToolCallProps) {
  const [open, setOpen] = useState(false);
  const hasArguments = !!toolArguments && Object.keys(toolArguments).length > 0;
  const resultContent = result == null ? null : typeof result === "string" ? result : JSON.stringify(result);
  // Renderable artifacts (e.g. a map) render outside the card and stay visible;
  // the textual result stays inside the collapsible body. When the result is an
  // artifact, there is no separate text to show inside.
  const artifacts = resultContent ? extractToolArtifacts(name, resultContent) : [];
  const showResultInside = resultContent != null && artifacts.length === 0;
  const collapsible = hasArguments || showResultInside;

  const { icon: Icon, iconColor, label } = getToolCallDisplay(name, toolArguments);

  return (
    <Flex direction="column" gap={1.5} align="stretch">
      <ToolCard>
        <ToolCardHeader
          sequenceNumber={sequenceNumber}
          icon={
            <Box color={iconColor}>
              <Icon size={12} />
            </Box>
          }
          title={label}
          badges={status === "running" || status === "completed" ? <ToolStatusBadge status={status} /> : undefined}
          open={open}
          collapsible={collapsible}
          shimmer={status === "running"}
          onToggle={() => setOpen((current) => !current)}
        />

        {collapsible && open && (
          <ToolCardBody maxH="560px">
            <Flex direction="column" gap={3} align="stretch">
              {hasArguments && <ToolCallView name={name} args={toolArguments} agents={agents} />}
              {showResultInside && <ToolResultView name={name} content={resultContent ?? ""} />}
            </Flex>
          </ToolCardBody>
        )}
      </ToolCard>
      <ToolArtifacts artifacts={artifacts} />
    </Flex>
  );
}
