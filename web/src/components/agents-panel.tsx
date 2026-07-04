"use client";

import { Badge, Box, EmptyState, Flex, IconButton, Text, VStack } from "@chakra-ui/react";
import { useEffect, useRef, useState, type PointerEvent } from "react";
import { LuBot, LuNetwork, LuX } from "react-icons/lu";
import { motion } from "motion/react";
import type { AgentStep, AgentGroup, TaskState } from "@/lib/use-chat";
import { AgentTimeline } from "./agent-timeline";
import { ToolCard, ToolCardBody, ToolCardHeader, ToolMetaRow } from "./tool-card";

// A Chakra Flex that is also a motion component, so the agents sidebar can
// animate its open/close (opacity + slide) without losing its flex-layout props.
const MotionFlex = motion.create(Flex);

// Maps an A2A TaskState to a status badge. Completed steps carry no badge —
// the settled state is self-evident from the card. Only non-default states show.
function AgentStateBadge({ state }: { state: TaskState }) {
  if (state === "completed") return null;
  const { label, palette } =
    state === "failed"
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
}: {
  step: AgentStep;
  agentLabel: string;
  agents: { id: string; name: string; title?: string }[];
}) {
  const [open, setOpen] = useState(true);

  return (
    <ToolCard>
      <ToolCardHeader
        icon={<Box color="fg.muted"><LuBot size={12} /></Box>}
        title={step.goal || "Agent task"}
        badges={
          <>
            <Badge size="sm" variant="subtle" colorPalette="gray" borderRadius="sm" flexShrink={0}>
              {agentLabel || "Agent"}
            </Badge>
            <AgentStateBadge state={step.state} />
          </>
        }
        open={open}
        collapsible
        onToggle={() => setOpen((current) => !current)}
      />

      {open && step.parts.length > 0 && (
        <ToolCardBody>
          <AgentTimeline parts={step.parts} agents={agents} />
        </ToolCardBody>
      )}
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

  useEffect(() => {
    if (!open || !focusedGroupId) return;
    const node = cardRefs.current.get(focusedGroupId);
    node?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [open, focusedGroupId]);

  return (
    <MotionFlex
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
      display="flex"
      initial={{ opacity: 0, x: 24 }}
      animate={{ opacity: 1, x: 0 }}
      exit={{ opacity: 0, x: 24 }}
      transition={{ duration: 0.2, ease: [0.4, 0, 0.2, 1] }}
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
            {agentGroups.map((group) => (
              <Box
                key={group.groupId}
                ref={(node: HTMLDivElement | null) => {
                  if (node) cardRefs.current.set(group.groupId, node);
                  else cardRefs.current.delete(group.groupId);
                }}
              >
                <Flex direction="column" gap={2}>
                  {group.steps.map((step) => (
                    <StepCard
                      key={step.stepId}
                      step={step}
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
    </MotionFlex>
  );
}
