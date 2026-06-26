"use client";

import { Box } from "@chakra-ui/react";
import { useState } from "react";
import { getToolResultDisplay } from "@/lib/tool-display";
import { ToolResultView } from "./tool-views";
import { ToolCard, ToolCardBody, ToolCardHeader } from "./tool-card";

interface ToolResultProps {
  name?: string;
  content: string;
  sequenceNumber?: number;
}

export function ToolResult({ name, content, sequenceNumber }: ToolResultProps) {
  const [open, setOpen] = useState(false);

  const { icon: Icon, iconColor, label } = getToolResultDisplay(name, content);

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
        open={open}
        collapsible
        onToggle={() => setOpen((current) => !current)}
      />

      {open && (
        <ToolCardBody maxH="320px">
          <ToolResultView name={name ?? ""} content={content} />
        </ToolCardBody>
      )}
    </ToolCard>
  );
}
