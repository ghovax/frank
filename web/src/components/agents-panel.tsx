"use client";

import { Badge, Box, Flex, IconButton, Text } from "@chakra-ui/react";
import { useEffect, useRef, useState, type PointerEvent } from "react";
import { LuChevronDown, LuChevronRight, LuNetwork, LuX } from "react-icons/lu";
import type { AgentStep, Orchestration } from "@/lib/use-chat";
import { MarkdownContent } from "./markdown-content";
import { ToolCall } from "./tool-call";
import { ToolCard, ToolCardBody, ToolCardHeader, ToolMetaRow, ToolStatusBadge } from "./tool-card";

interface AgentsPanelProps {
  orchestrations: Orchestration[];
  agents: { name: string; label: string }[];
  open: boolean;
  onClose: () => void;
  focusedOrchestrationId: string | null;
  width: number;
  onResizeStart: (event: PointerEvent<HTMLDivElement>) => void;
}

function StepCard({ step, agentLabel, agents }: { step: AgentStep; agentLabel: string; agents: { name: string; label: string }[] }) {
  const [open, setOpen] = useState(!step.done);
  const [thinkingOpen, setThinkingOpen] = useState(false);

  useEffect(() => {
    if (step.done) setOpen(false);
  }, [step.done]);

  return (
    <ToolCard>
      <ToolCardHeader
        title={step.goal || "Agent task"}
        badges={
          <>
            <Badge size="sm" variant="surface" colorPalette="purple" borderRadius="sm" flexShrink={0}>
              {agentLabel || "Agent"}
            </Badge>
            <ToolStatusBadge status={step.done ? "done" : "running"} />
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
          <Box mb={step.toolCalls.length > 0 || step.text ? 2 : 0}>
            <Flex
              align="center"
              gap={1}
              cursor="pointer"
              color="fg.subtle"
              onClick={() => setThinkingOpen((current) => !current)}
              userSelect="none"
            >
              {thinkingOpen ? <LuChevronDown size={11} /> : <LuChevronRight size={11} />}
              <Text fontSize="xs" fontWeight="medium">Reasoning</Text>
            </Flex>
            {thinkingOpen && (
              <Box mt={1} pl={3} borderLeft="2px solid" borderColor="border" color="fg.muted" fontSize="xs" whiteSpace="pre-wrap">
                {step.thinking}
              </Box>
            )}
          </Box>
        )}

        {step.toolCalls.length > 0 && (
          <Flex direction="column" gap={1.5} mb={step.text ? 2 : 0}>
            {step.toolCalls.map((toolCall, index) => (
              <ToolCall
                key={`${step.stepId}-tool-${index}`}
                name={toolCall.name}
                arguments={toolCall.arguments}
                sequenceNumber={toolCall.sequenceNumber}
                agents={agents}
              />
            ))}
          </Flex>
        )}

        {step.text ? (
          <MarkdownContent content={step.text} />
        ) : (
          !step.done && step.toolCalls.length === 0 && !step.thinking && (
            <Text fontSize="xs" color="fg.subtle">Working…</Text>
          )
        )}
      </ToolCardBody>}
    </ToolCard>
  );
}

function OrchestrationCard({
  orchestration,
  agentLabels,
  agents,
}: {
  orchestration: Orchestration;
  agentLabels: Map<string, string>;
  agents: { name: string; label: string }[];
}) {
  const completed = orchestration.steps.filter((step) => step.done).length;
  const total = orchestration.steps.length;
  const active = total - completed;
  const [open, setOpen] = useState(active > 0);

  useEffect(() => {
    if (active === 0) setOpen(false);
    else setOpen(true);
  }, [active]);

  return (
    <ToolCard>
      <ToolCardHeader
        icon={<Box color="orange.fg"><LuNetwork size={12} /></Box>}
        title={orchestration.justification || "Orchestration"}
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
          {orchestration.steps.map((step) => (
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
  orchestrations,
  agents,
  open,
  onClose,
  focusedOrchestrationId,
  width,
  onResizeStart,
}: AgentsPanelProps) {
  const cardRefs = useRef<Map<string, HTMLDivElement>>(new Map());
  const agentLabels = new Map(agents.map((agent) => [agent.name, agent.label]));

  useEffect(() => {
    if (!open || !focusedOrchestrationId) return;
    const node = cardRefs.current.get(focusedOrchestrationId);
    node?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [open, focusedOrchestrationId]);

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
        <Box color="orange.fg"><LuNetwork size={15} /></Box>
        <Text fontSize="sm" fontWeight="bold" flex={1}>Agents</Text>
        <IconButton aria-label="Collapse agents sidebar" size="xs" variant="ghost" borderRadius="sm" onClick={onClose}>
          <LuX size={15} />
        </IconButton>
      </Flex>

      <Box flex={1} minH={0} overflowY="auto" px={3} py={3}>
        {orchestrations.length === 0 ? (
          <Flex h="100%" align="center" justify="center">
            <Text fontSize="xs" color="fg.muted">No agent activity yet.</Text>
          </Flex>
        ) : (
          <Flex direction="column" gap={3}>
            {orchestrations.map((orchestration) => (
              <Box
                key={orchestration.orchestrationId}
                ref={(node: HTMLDivElement | null) => {
                  if (node) cardRefs.current.set(orchestration.orchestrationId, node);
                  else cardRefs.current.delete(orchestration.orchestrationId);
                }}
              >
                <OrchestrationCard orchestration={orchestration} agentLabels={agentLabels} agents={agents} />
              </Box>
            ))}
          </Flex>
        )}
      </Box>
    </Flex>
  );
}
