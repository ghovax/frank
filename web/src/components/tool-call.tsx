"use client";

import { Box, Button, Flex, HStack, Text } from "@chakra-ui/react";
import { useEffect, useRef, useState } from "react";
import { getToolCallDisplay } from "@/lib/tool-display";
import type { PermissionDecision, ToolEvent, ToolPermission } from "@/lib/tool-event";
import { ToolArtifacts, ToolCallView, ToolResultView, extractToolArtifacts } from "./tool-views";
import { ToolCard, ToolCardBody, ToolCardHeader, ToolRiskBadges, ToolStatusBadge } from "./tool-card";

interface ToolCallProps extends ToolEvent {
  agents?: { id: string; name: string; title?: string }[];
  onPermission?: (requestId: string, decision: PermissionDecision) => void;
}

const DECIDED_LABEL: Record<PermissionDecision, { label: string; color: string }> = {
  deny: { label: "Denied", color: "red.fg" },
  allow_once: { label: "Allowed", color: "green.fg" },
  allow_always: { label: "Always allowed", color: "blue.fg" },
};

// The human-in-the-loop approval, rendered inside the tool card it belongs to.
// Deny sits far left and Allow once far right (so they can't be mis-clicked),
// with Always allow tucked beside Allow once. Keys: 1 deny, 2 allow always,
// ⌘/Ctrl+Enter allow once. Once decided it collapses to a short outcome line and
// the card resumes its normal running/done lifecycle.
function ToolPermissionPrompt({
  permission,
  onPermission,
}: {
  permission: ToolPermission;
  onPermission?: (requestId: string, decision: PermissionDecision) => void;
}) {
  const boxRef = useRef<HTMLDivElement>(null);
  const pending = !permission.decision;

  function decide(decision: PermissionDecision) {
    onPermission?.(permission.requestId, decision);
  }

  function handleKeyDown(event: React.KeyboardEvent) {
    if (event.key === "1") {
      event.preventDefault();
      decide("deny");
    } else if (event.key === "2") {
      event.preventDefault();
      decide("allow_always");
    } else if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      decide("allow_once");
    }
  }

  useEffect(() => {
    if (pending) boxRef.current?.focus();
  }, [pending]);

  if (!pending) {
    const decided = DECIDED_LABEL[permission.decision!];
    return (
      <Text fontSize="xs" fontWeight="bold" color={decided.color}>
        {decided.label}
      </Text>
    );
  }

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
          Allow once (⌘↵)
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
  const showPermission = !!permission && (status === "input_required" || !!permission.decision);
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
            <Flex direction="column" gap={3} align="stretch">
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
