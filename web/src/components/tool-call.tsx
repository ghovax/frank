"use client";

import { Badge, Box, Flex } from "@chakra-ui/react";
import { useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import { getToolCallDisplay } from "@/lib/tool-display";
import { ToolCallLabel } from "./tool-label";
import type { PermissionDecision, QuestionAnswer, ToolEvent } from "@/lib/tool-event";
import { ToolCallView, ToolResultView, extractToolArtifacts } from "./tool-views";
import { ToolCard, ToolCardBody, ToolCardHeader, ToolRiskBadges, ToolStatusBadge } from "./tool-card";

interface ToolCallProps extends ToolEvent {
  agents?: { id: string; name: string; title?: string }[];
  onPermission?: (requestId: string, decision: PermissionDecision) => void;
  onQuestion?: (requestId: string, answers: QuestionAnswer[]) => void;
  // The single live web-preview id (owned by ChatPanel). Only the matching
  // iframe-type artifact mounts its frame; the rest collapse to a placeholder.
  activePreviewId?: string | null;
  onActivatePreview?: (id: string) => void;
}

function isToolErrorResult(content: string | null): boolean {
  if (!content) return false;
  try {
    const parsed = JSON.parse(content);
    return !!parsed && typeof parsed === "object" && !Array.isArray(parsed) && (parsed as Record<string, unknown>).code === "tool_error";
  } catch {
    return false;
  }
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function isBackgroundResult(result: unknown): boolean {
  const code = String(asRecord(result).code ?? "");
  return code.endsWith("_started") || code === "background_task_scheduled";
}

export function ToolCall({ name, arguments: toolArguments, result, status, agents = [] }: ToolCallProps) {
  const [open, setOpen] = useState(false);
  const hasArguments = !!toolArguments && Object.keys(toolArguments).length > 0;
  const resultContent = result == null ? null : typeof result === "string" ? result : JSON.stringify(result);
  // Renderable artifacts (e.g. a map) render outside the card and stay visible;
  // the textual result stays inside the collapsible body. When the result is an
  // artifact, there is no separate text to show inside.
  const artifacts = resultContent ? extractToolArtifacts(name, resultContent) : [];
  const showResultInside = resultContent != null && artifacts.length === 0 && !isToolErrorResult(resultContent);
  const collapsible = hasArguments || showResultInside;
  // A pending approval/question no longer forces the card open — it is surfaced in
  // an overlay above the composer (see PermissionOverlay / QuestionOverlay). The
  // card only reflects the "input required" status in its badge and header tint.
  const bodyOpen = open;
  const background = status === "running" && isBackgroundResult(result);

  const { icon: Icon, iconColor } = getToolCallDisplay(name, toolArguments);

  return (
    <Flex direction="column" gap={1.5} align="stretch">
      <ToolCard>
        <ToolCardHeader
          icon={
            <Box color={iconColor}>
              <Icon size={12} />
            </Box>
          }
          title={<ToolCallLabel name={name} args={toolArguments} />}
          badges={
            <>
              <ToolRiskBadges arguments={toolArguments} />
              {status === "running" || status === "completed" || status === "failed" || status === "input_required" ? <ToolStatusBadge status={status} /> : null}
              {background ? <Badge size="sm" variant="subtle" colorPalette="purple" borderRadius="sm" flexShrink={0}>Background</Badge> : null}
            </>
          }
          open={bodyOpen}
          collapsible={collapsible}
          shimmer={status === "running"}
          headerBg={status === "input_required" ? "yellow.subtle" : undefined}
          onToggle={() => setOpen((current) => !current)}
        />

        <AnimatePresence initial={false}>
          {collapsible && bodyOpen && (
            <motion.div
              key="body"
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: "auto" }}
              exit={{ opacity: 0, height: 0 }}
              transition={{ duration: 0.2, ease: [0.4, 0, 0.2, 1] }}
              style={{ overflow: "hidden" }}
            >
              <ToolCardBody maxH="560px">
                {/* gap matches FieldList's own field spacing so the call's last field
                    (e.g. Risk) and the result's first (e.g. PID) read as one list. */}
                <Flex direction="column" gap={2} align="stretch">
                  {hasArguments && <ToolCallView name={name} args={toolArguments} agents={agents} />}
                  {showResultInside && <ToolResultView name={name} content={resultContent ?? ""} />}
                </Flex>
              </ToolCardBody>
            </motion.div>
          )}
        </AnimatePresence>
      </ToolCard>
    </Flex>
  );
}
