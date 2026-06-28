"use client";

import { Badge, Box, EmptyState, Flex, IconButton, Text, VStack } from "@chakra-ui/react";
import { useEffect, useRef, useState, type PointerEvent } from "react";
import { LuBot, LuNetwork, LuX } from "react-icons/lu";
import type { AgentStep, AgentGroup, TaskState } from "@/lib/use-chat";
import { AgentTimeline } from "./agent-timeline";
import { ToolCard, ToolCardBody, ToolCardHeader, ToolMetaRow } from "./tool-card";

// Maps an A2A TaskState to a status badge. The badge is the canonical lifecycle
// state, so a failed or cancelled step reads differently from a clean finish.
function AgentStateBadge({ state }: { state: TaskState }) {
  const { label, palette } =
    state === "completed"
      ? { label: "Completed", palette: "green" }
      : state === "failed"
        ? { label: "Failed", palette: "red" }
        : state === "rejected"
          ? { label: "Rejected", palette: "red" }
          : state === "canceled"
            ? { label: "Canceled", palette: "gray" }
            : state === "input-required"
              ? { label: "Input required", palette: "yellow" }
              : state === "auth-required"
                ? { label: "Authentication required", palette: "yellow" }
                : state === "working"
                  ? { label: "Working", palette: "blue" }
                  : { label: "Submitted", palette: "blue" };
  return (
    <Badge size="sm" variant="subtle" colorPalette={palette} borderRadius="sm" flexShrink={0}>
      {label}
    </Badge>
  );
}

interface AgentsPanelProps {
  agentGroups: AgentGroup[];
  agents: { id: string; name: string; title?: string }[];
  open: boolean;
  onClose: () => void;
  focusedGroupId: string | null;
  width: number;
  onResizeStart: (event: PointerEvent<HTMLDivElement>) => void;
}

function StepCard({
  step,
  agentLabel,
  agents,
  sequenceNumber,
}: {
  step: AgentStep;
  agentLabel: string;
  agents: { id: string; name: string; title?: string }[];
  sequenceNumber?: number;
}) {
  const [open, setOpen] = useState(true);

  return (
    <ToolCard>
      <ToolCardHeader
        sequenceNumber={sequenceNumber}
        icon={<Box color="fg.muted"><LuBot size={12} /></Box>}
        title={step.goal || "Agent task"}
        badges={
          <>
            <Badge size="sm" variant="surface" colorPalette="gray" borderRadius="sm" flexShrink={0}>
              {agentLabel || "Agent"}
            </Badge>
            <AgentStateBadge state={step.state} />
          </>
        }
        open={open}
        collapsible
        onToggle={() => setOpen((current) => !current)}
      />

      {open && <ToolCardBody>
        {step.stepId && (
          <Box mb={2}>
            <ToolMetaRow label="Step">
              <Text truncate>{step.stepId}</Text>
            </ToolMetaRow>
          </Box>
        )}
        {step.parts.length > 0 && (
          <AgentTimeline parts={step.parts} agents={agents} />
        )}
      </ToolCardBody>}
    </ToolCard>
  );
}

export function AgentsPanel({
  agentGroups,
  agents,
  open,
  onClose,
  focusedGroupId,
  width,
  onResizeStart,
}: AgentsPanelProps) {
  const cardRefs = useRef<Map<string, HTMLDivElement>>(new Map());
  const agentLabels = new Map(agents.map((agent) => [agent.id, agent.title || agent.name]));

  // Sub-agents read as one flat, numbered list across all groups — the group is
  // a logical anchor (for scroll-to focus), not a visual box. Each group's steps
  // continue the running number, so offsets are precomputed.
  const groupNumberOffsets = agentGroups.reduce<number[]>((offsets, group, index) => {
    offsets.push(index === 0 ? 0 : offsets[index - 1] + agentGroups[index - 1].steps.length);
    return offsets;
  }, []);

  useEffect(() => {
    if (!open || !focusedGroupId) return;
    const node = cardRefs.current.get(focusedGroupId);
    node?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [open, focusedGroupId]);

  if (!open) return null;

  return (
    <Flex
      w={{ base: "100%", md: `${width}px` }}
      maxW={{ base: "100%", md: "52vw" }}
      minW={{ base: "100%", md: "300px" }}
      h={{ base: "100dvh", md: "auto" }}
      direction="column"
      bg="bg"
      borderLeft="1px solid"
      borderColor="border"
      flexShrink={0}
      position={{ base: "fixed", md: "relative" }}
      inset={{ base: 0, md: "auto" }}
      zIndex={{ base: 1000, md: "auto" }}
      minH={0}
      display={{ base: open ? "flex" : "none", md: "flex" }}
    >
      <Box
        display={{ base: "none", md: "block" }}
        position="absolute"
        top={0}
        bottom={0}
        left="-4px"
        w="8px"
        cursor="col-resize"
        zIndex={1}
        onPointerDown={onResizeStart}
      />
      <Flex align="center" gap={2} px={3} py={2} borderBottom="1px solid" borderColor="border" flexShrink={0}>
        <Box color="fg.muted"><LuNetwork size={15} /></Box>
        <Text fontSize="sm" fontWeight="bold" flex={1}>Agents</Text>
        <IconButton aria-label="Collapse agents sidebar" size="xs" variant="ghost" borderRadius="sm" onClick={onClose}>
          <LuX size={15} />
        </IconButton>
      </Flex>

      <Box flex={1} minH={0} overflowY="auto" px={3} py={3}>
        {agentGroups.length === 0 ? (
          <Flex direction="column" align="center" justify="center" minH="100%" gap={6} px={2} pt={4} pb={12}>
            <EmptyState.Root>
              <EmptyState.Content>
                <EmptyState.Indicator>
                  <LuBot />
                </EmptyState.Indicator>
                <VStack gap={1}>
                  <EmptyState.Title>No agent activity yet</EmptyState.Title>
                  <EmptyState.Description>
                    Spawned agents will appear here
                  </EmptyState.Description>
                </VStack>
              </EmptyState.Content>
            </EmptyState.Root>
          </Flex>
        ) : (
          <Flex direction="column" gap={2}>
            {agentGroups.map((group, groupIndex) => (
              <Box
                key={group.groupId}
                ref={(node: HTMLDivElement | null) => {
                  if (node) cardRefs.current.set(group.groupId, node);
                  else cardRefs.current.delete(group.groupId);
                }}
              >
                <Flex direction="column" gap={2}>
                  {group.steps.map((step, stepIndex) => (
                    <StepCard
                      key={step.stepId}
                      step={step}
                      sequenceNumber={groupNumberOffsets[groupIndex] + stepIndex + 1}
                      agentLabel={agentLabels.get(step.agent) ?? step.agent}
                      agents={agents}
                    />
                  ))}
                </Flex>
              </Box>
            ))}
          </Flex>
        )}
      </Box>
    </Flex>
  );
}
