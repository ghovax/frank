"use client";

import { Box } from "@chakra-ui/react";
import { useState } from "react";
import { getToolCallDisplay } from "@/lib/tool-display";
import { ToolCallView } from "./tool-views";
import { ToolCard, ToolCardBody, ToolCardHeader, ToolStatusBadge } from "./tool-card";

interface ToolCallProps {
  name: string;
  arguments?: Record<string, unknown>;
  sequenceNumber?: number;
  status?: string;
  agents?: { id: string; name: string }[];
}

export function ToolCall({ name, arguments: toolArguments, sequenceNumber, status, agents = [] }: ToolCallProps) {
  const [open, setOpen] = useState(false);
  const hasArguments = !!toolArguments && Object.keys(toolArguments).length > 0;

  const { icon: Icon, iconColor, label } = getToolCallDisplay(name, toolArguments);

  return (
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
        collapsible={hasArguments}
        shimmer={status === "running"}
        onToggle={() => setOpen((current) => !current)}
      />

      {hasArguments && open && (
        <ToolCardBody maxH="320px">
          <ToolCallView name={name} args={toolArguments} agents={agents} />
        </ToolCardBody>
      )}
    </ToolCard>
  );
}
