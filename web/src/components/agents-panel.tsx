"use client";

import { Badge, Box, Flex, IconButton, Text } from "@chakra-ui/react";
import { useEffect, useRef, useState, type PointerEvent } from "react";
import { LuChevronDown, LuChevronRight, LuNetwork, LuX } from "react-icons/lu";
import type { AgentStep, AgentGroup, TaskState } from "@/lib/use-chat";
import { isStepDone } from "@/lib/use-chat";
import { getFocusIcon } from "@/lib/focus-icon";
import { AgentTimeline } from "./agent-timeline";
import { ToolCard, ToolCardBody, ToolCardHeader, ToolMetaRow } from "./tool-card";

// Maps an A2A TaskState to a status badge. The badge is the canonical lifecycle
// state, so a failed or cancelled step reads differently from a clean finish.
function AgentStateBadge({ state }: { state: TaskState }) {
  const { label, palette } =
    state === "completed"
      ? { label: "Done", palette: "green" }
      : state === "failed"
        ? { label: "Failed", palette: "red" }
        : state === "rejected"
          ? { label: "Rejected", palette: "red" }
          : state === "canceled"
            ? { label: "Canceled", palette: "gray" }
            : state === "input-required" || state === "auth-required"
              ? { label: "Waiting", palette: "yellow" }
              : { label: "Running", palette: "blue" };
  return (
    <Badge size="sm" variant="subtle" colorPalette={palette} borderRadius="sm" flexShrink={0}>
      {label}
    </Badge>
  );
}

interface AgentsPanelProps {
  agentGroups: AgentGroup[];
  agents: { name: string; label: string }[];
  open: boolean;
  onClose: () => void;
  focusedGroupId: string | null;
  width: number;
  onResizeStart: (event: PointerEvent<HTMLDivElement>) => void;
}

function StepCard({ step, agentLabel, agents }: { step: AgentStep; agentLabel: string; agents: { name: string; label: string }[] }) {
  const done = isStepDone(step);
  const [open, setOpen] = useState(true);
  const [thinkingOpen, setThinkingOpen] = useState(false);
  const { icon: FocusIcon, color: focusColor } = getFocusIcon(step.icon, step.focus);

  return (
    <ToolCard>
      <ToolCardHeader
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
        {step.thinking && (
          <Box mb={step.parts.length > 0 ? 2 : 0}>
            <Flex
              align="center"
              gap={1}
              cursor="pointer"
              color="fg.subtle"
              onClick={() => setThinkingOpen((current) => !current)}
              userSelect="none"
            >
              {thinkingOpen ? <LuChevronDown size={11} /> : <LuChevronRight size={11} />}
              <Box color={focusColor} fontSize="sm" flexShrink={0}>
                <FocusIcon size={11} />
              </Box>
              {step.focus && <Text fontSize="xs" fontWeight="medium">{step.focus}</Text>}
            </Flex>
            {thinkingOpen && (
              <Box mt={1} pl={3} borderLeft="2px solid" borderColor="border" color="fg.muted" fontSize="xs" whiteSpace="pre-wrap">
                {step.thinking}
              </Box>
            )}
          </Box>
        )}

        {step.parts.length > 0 ? (
          <AgentTimeline parts={step.parts} agents={agents} />
        ) : (
          !done && !step.thinking && step.focus && (
            <Flex align="center" gap={1} color="fg.subtle">
              <Box color={focusColor} fontSize="sm" flexShrink={0}>
                <FocusIcon size={11} />
              </Box>
              <Text fontSize="xs">{step.focus}</Text>
            </Flex>
          )
        )}
      </ToolCardBody>}
    </ToolCard>
  );
}

function AgentGroupCard({
  group,
  agentLabels,
  agents,
  sequenceNumber,
}: {
  group: AgentGroup;
  agentLabels: Map<string, string>;
  agents: { name: string; label: string }[];
  sequenceNumber?: number;
}) {
  const completed = group.steps.filter((step) => isStepDone(step)).length;
  const total = group.steps.length;
  const active = total - completed;
  const [open, setOpen] = useState(true);

  return (
    <ToolCard>
      <ToolCardHeader
        sequenceNumber={sequenceNumber}
        icon={<Box color="fg.muted"><LuNetwork size={12} /></Box>}
        title={group.justification || "Sub-agents"}
        badges={
          <Badge size="sm" variant="surface" colorPalette={active > 0 ? "blue" : "green"} borderRadius="sm" flexShrink={0}>
            {active > 0 ? `${active} running` : "Done"}
          </Badge>
        }
        open={open}
        collapsible
        onToggle={() => setOpen((current) => !current)}
      />
      {open && <ToolCardBody>
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
  const agentLabels = new Map(agents.map((agent) => [agent.name, agent.label]));

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
          <Flex h="100%" align="center" justify="center">
            <Text fontSize="xs" color="fg.muted">No agent activity yet.</Text>
          </Flex>
        ) : (
          <Flex direction="column" gap={3}>
            {agentGroups.map((group, index) => (
              <Box
                key={group.groupId}
                ref={(node: HTMLDivElement | null) => {
                  if (node) cardRefs.current.set(group.groupId, node);
                  else cardRefs.current.delete(group.groupId);
                }}
              >
                <AgentGroupCard group={group} agentLabels={agentLabels} agents={agents} sequenceNumber={index + 1} />
              </Box>
            ))}
          </Flex>
        )}
      </Box>
    </Flex>
  );
}
