"use client";

import { Box, Button, Flex, HStack } from "@chakra-ui/react";
import { useEffect, useRef, useState } from "react";
import { getToolCallDisplay } from "@/lib/tool-display";
import type { PermissionDecision, ToolEvent, ToolPermission } from "@/lib/tool-event";
import { ToolArtifacts, ToolCallView, ToolResultView, extractToolArtifacts } from "./tool-views";
import { ToolCard, ToolCardBody, ToolCardHeader, ToolRiskBadges, ToolStatusBadge } from "./tool-card";

interface ToolCallProps extends ToolEvent {
  agents?: { id: string; name: string; title?: string }[];
  onPermission?: (requestId: string, decision: PermissionDecision) => void;
}

// The human-in-the-loop approval, rendered inside the tool card it belongs to,
// only while pending. Deny sits far left and Allow once far right (so they can't
// be mis-clicked), with Always allow beside Allow once. Keys: 1 deny, 2 allow
// always, 3 allow once. Once decided, the card's own status badge (Running →
// Completed, or Failed) carries the outcome — the prompt disappears entirely.
function ToolPermissionPrompt({
  permission,
  onPermission,
}: {
  permission: ToolPermission;
  onPermission?: (requestId: string, decision: PermissionDecision) => void;
}) {
  const boxRef = useRef<HTMLDivElement>(null);

  function decide(decision: PermissionDecision) {
    onPermission?.(permission.requestId, decision);
  }

  function handleKeyDown(event: React.KeyboardEvent) {
    // 1 deny, 2 allow always, 3 allow once — mirrors the on-screen buttons.
    if (event.key === "1") {
      event.preventDefault();
      decide("deny");
    } else if (event.key === "2") {
      event.preventDefault();
      decide("allow_always");
    } else if (event.key === "3") {
      event.preventDefault();
      decide("allow_once");
    }
  }

  useEffect(() => {
    boxRef.current?.focus();
  }, []);

  // Flat — the yellow card header already signals the approval, and the command
  // is shown above, so the prompt is just the controls (no nested box).
  return (
    <Flex
      ref={boxRef}
      tabIndex={0}
      onKeyDown={handleKeyDown}
      align="center"
      justify="space-between"
      gap={2}
      _focus={{ outline: "none", boxShadow: "none" }}
    >
      <Button size="xs" colorPalette="red" variant="solid" onClick={() => decide("deny")}>
        Deny (1)
      </Button>
      <HStack gap={2}>
        <Button size="xs" colorPalette="blue" variant="subtle" onClick={() => decide("allow_always")}>
          Always allow (2)
        </Button>
        <Button size="xs" colorPalette="green" variant="solid" onClick={() => decide("allow_once")}>
          Allow once (3)
        </Button>
      </HStack>
    </Flex>
  );
}

export function ToolCall({ name, arguments: toolArguments, result, sequenceNumber, status, permission, agents = [], onPermission }: ToolCallProps) {
  const [open, setOpen] = useState(false);
  const hasArguments = !!toolArguments && Object.keys(toolArguments).length > 0;
  const resultContent = result == null ? null : typeof result === "string" ? result : JSON.stringify(result);
  // Renderable artifacts (e.g. a map) render outside the card and stay visible;
  // the textual result stays inside the collapsible body. When the result is an
  // artifact, there is no separate text to show inside.
  const artifacts = resultContent ? extractToolArtifacts(name, resultContent) : [];
  const showResultInside = resultContent != null && artifacts.length === 0;
  // Only while pending — once decided, the status badge carries the outcome.
  const showPermission = !!permission && status === "input_required";
  const collapsible = hasArguments || showResultInside || showPermission;
  // A pending approval forces the card open so the Allow/Deny controls are visible
  // without a click.
  const bodyOpen = open || status === "input_required";

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
          badges={
            <>
              <ToolRiskBadges arguments={toolArguments} />
              {status === "running" || status === "completed" || status === "failed" || status === "input_required" ? <ToolStatusBadge status={status} /> : null}
            </>
          }
          open={bodyOpen}
          collapsible={collapsible}
          shimmer={status === "running"}
          headerBg={status === "input_required" ? "yellow.subtle" : undefined}
          onToggle={() => setOpen((current) => !current)}
        />

        {collapsible && bodyOpen && (
          <ToolCardBody maxH="560px">
            {/* gap matches FieldList's own field spacing so the call's last field
                (e.g. Risk) and the result's first (e.g. PID) read as one list. */}
            <Flex direction="column" gap={2} align="stretch">
              {hasArguments && <ToolCallView name={name} args={toolArguments} agents={agents} />}
              {showPermission && permission && <ToolPermissionPrompt permission={permission} onPermission={onPermission} />}
              {showResultInside && <ToolResultView name={name} content={resultContent ?? ""} />}
            </Flex>
          </ToolCardBody>
        )}
      </ToolCard>
      <ToolArtifacts artifacts={artifacts} />
    </Flex>
  );
}
