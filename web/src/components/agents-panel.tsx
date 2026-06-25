"use client";

import { Badge, Box, Flex, IconButton, Spinner, Text } from "@chakra-ui/react";
import { useEffect, useRef, useState } from "react";
import { LuCheck, LuChevronDown, LuChevronRight, LuNetwork, LuX } from "react-icons/lu";
import type { AgentStep, Orchestration } from "@/lib/use-chat";
import { MarkdownContent } from "./markdown-content";
import { ToolCall } from "./tool-call";

interface AgentsPanelProps {
  orchestrations: Orchestration[];
  open: boolean;
  onClose: () => void;
  focusedOrchestrationId: string | null;
}

function StepCard({ step }: { step: AgentStep }) {
  const [thinkingOpen, setThinkingOpen] = useState(false);

  return (
    <Box borderRadius="md" border="1px solid" borderColor="border" bg="bg.subtle" overflow="hidden">
      <Flex align="center" gap={2} px={2.5} py={1.5} borderBottom="1px solid" borderColor="border">
        <Badge size="sm" colorPalette={step.done ? "green" : "blue"} variant="subtle">
          {step.agent || "agent"}
        </Badge>
        <Text fontSize="xs" color="fg.muted" truncate flex={1}>
          {step.stepId}
        </Text>
        {step.done ? (
          <Box color="green.fg" flexShrink={0}><LuCheck size={13} /></Box>
        ) : (
          <Spinner size="xs" color="blue.fg" flexShrink={0} />
        )}
      </Flex>

      <Box px={2.5} py={2}>
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
          <Flex direction="column" gap={1} mb={step.text ? 2 : 0}>
            {step.toolCalls.map((toolCall, index) => (
              <ToolCall
                key={`${step.stepId}-tool-${index}`}
                name={toolCall.name}
                arguments={toolCall.arguments}
                sequenceNumber={toolCall.sequenceNumber}
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
      </Box>
    </Box>
  );
}

function OrchestrationCard({ orchestration }: { orchestration: Orchestration }) {
  const completed = orchestration.steps.filter((step) => step.done).length;
  const total = orchestration.steps.length;

  return (
    <Box borderRadius="md" border="1px solid" borderColor="border" overflow="hidden">
      <Flex align="center" gap={2} px={2.5} py={2} bg="bg.muted" borderBottom="1px solid" borderColor="border">
        <Box color="orange.fg" flexShrink={0}><LuNetwork size={14} /></Box>
        <Text fontSize="xs" fontWeight="bold" truncate flex={1}>
          {orchestration.justification || "Orchestration"}
        </Text>
        <Badge size="sm" variant="surface" flexShrink={0}>
          {completed}/{total}
        </Badge>
      </Flex>
      <Flex direction="column" gap={2} p={2.5}>
        {orchestration.steps.map((step) => (
          <StepCard key={step.stepId} step={step} />
        ))}
      </Flex>
    </Box>
  );
}

export function AgentsPanel({ orchestrations, open, onClose, focusedOrchestrationId }: AgentsPanelProps) {
  const cardRefs = useRef<Map<string, HTMLDivElement>>(new Map());

  useEffect(() => {
    if (!open || !focusedOrchestrationId) return;
    const node = cardRefs.current.get(focusedOrchestrationId);
    node?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [open, focusedOrchestrationId]);

  if (!open) return null;

  return (
    <Box position="fixed" inset={0} zIndex={1000}>
      <Box position="absolute" inset={0} bg="blackAlpha.500" onClick={onClose} />
      <Flex
        position="absolute"
        top={0}
        right={0}
        bottom={0}
        w={{ base: "100%", md: "480px" }}
        direction="column"
        bg="bg"
        borderLeft="1px solid"
        borderColor="border"
        boxShadow="-4px 0 16px rgba(0,0,0,0.2)"
      >
        <Flex align="center" gap={2} px={3} py={2} borderBottom="1px solid" borderColor="border" flexShrink={0}>
          <Box color="orange.fg"><LuNetwork size={15} /></Box>
          <Text fontSize="sm" fontWeight="bold" flex={1}>Agents</Text>
          <IconButton aria-label="Close agents panel" size="xs" variant="ghost" borderRadius="sm" onClick={onClose}>
            <LuX size={15} />
          </IconButton>
        </Flex>

        <Box flex={1} overflowY="auto" px={3} py={3}>
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
                  <OrchestrationCard orchestration={orchestration} />
                </Box>
              ))}
            </Flex>
          )}
        </Box>
      </Flex>
    </Box>
  );
}
