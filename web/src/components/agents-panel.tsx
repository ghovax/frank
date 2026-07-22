"use client";

import { Box, Flex, IconButton } from "@chakra-ui/react";
import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { LuBot, LuNetwork, LuSquare } from "react-icons/lu";
import { cancelAgent } from "@/lib/api";
import type { ToolEvent } from "@/lib/tool-event";
import { isStepDone, type AgentStep, type AgentGroup, type TaskState } from "@/lib/use-chat";
import { AgentTimeline } from "./agent-timeline";
import { ToolLocationBadge, collapsedHeadingLocation } from "./tool-call";
import { DisclosureLabel, DisclosureRow } from "./ui/disclosure-row";
import { Pill } from "./ui/pill";
import { Tooltip } from "./ui/tooltip";
import { ActivityIcon, ActivitySpinner } from "./ui/activity-icon";
import { toaster } from "./ui/toaster";
import { STATUS_PALETTE, taskStateKind } from "@/lib/status";
import { PanelCard, PanelHeader, PanelBody, PanelEmptyState } from "@/components/ui/panel";

// The per-state label; the colour comes from the shared status palette (via the
// normalized kind), so agent-step colours track the rest of the app. Completed steps
// carry no badge — the settled state is self-evident.
const AGENT_STATE_LABEL_KEY: Partial<Record<TaskState, string>> = {
  failed: "stateFailed",
  rejected: "stateRejected",
  canceled: "stateCanceled",
  "input-required": "stateInputRequired",
  "auth-required": "stateAuthRequired",
  working: "stateWorking",
  submitted: "stateSubmitted",
};

function AgentStateBadge({ state }: { state: TaskState }) {
  const translation = useTranslations("AgentsPanel");
  if (state === "completed") return null;
  const labelKey = AGENT_STATE_LABEL_KEY[state] ?? "stateSubmitted";
  return (
    <Pill colorPalette={STATUS_PALETTE[taskStateKind(state)]}>
      {translation(labelKey as Parameters<typeof translation>[0])}
    </Pill>
  );
}

interface AgentsPanelProps {
  agentGroups: AgentGroup[];
  agents: { id: string; name: string; title?: string }[];
  sessionId: string | null;
  open: boolean;
  onClose: () => void;
  focusedGroupId: string | null;
}

function StepCard({
  step,
  agentLabel,
  agents,
  sessionId,
}: {
  step: AgentStep;
  agentLabel: string;
  agents: { id: string; name: string; title?: string }[];
  sessionId: string | null;
}) {
  const translation = useTranslations("AgentsPanel");
  const [stopping, setStopping] = useState(false);
  const active = !isStepDone(step);
  // The remote the step ran against (if any) — surfaced on the step's own (top)
  // disclosure, mirroring the grouped tool-call heading.
  const stepLocation = collapsedHeadingLocation(
    step.parts.filter((part) => part.kind === "tool").map((part) => (part as ToolEvent).arguments),
  );

  async function handleStop() {
    if (!sessionId || stopping) return;
    setStopping(true);
    try {
      const stopped = await cancelAgent(sessionId, step.stepId);
      if (!stopped) {
        toaster.create({
          type: "error",
          title: translation("stopFailed"),
          description: translation("stopFailedDescription"),
          closable: true,
        });
      }
    } finally {
      setStopping(false);
    }
  }

  return (
    <DisclosureRow
      defaultOpen
      icon={<Box color="fg.muted"><LuBot /></Box>}
      title={<DisclosureLabel>{step.goal || translation("agentTask")}</DisclosureLabel>}
      badges={
        <>
          <Pill colorPalette="gray">{agentLabel || translation("agent")}</Pill>
          <ToolLocationBadge arguments={stepLocation} />
          <AgentStateBadge state={step.state} />
        </>
      }
      actions={active ? (
        <Tooltip content={translation("stopAgent")} openDelay={300}>
          <IconButton
            aria-label={stopping ? translation("stoppingAgent") : translation("stopAgent")}
            variant="plain"
            colorPalette="red"
            boxSize="5"
            minW="5"
            disabled={!sessionId || stopping}
            onClick={handleStop}
          >
            {stopping ? <ActivitySpinner /> : <ActivityIcon><LuSquare /></ActivityIcon>}
          </IconButton>
        </Tooltip>
      ) : undefined}
    >
      {step.parts.length > 0 ? <AgentTimeline parts={step.parts} agents={agents} /> : undefined}
    </DisclosureRow>
  );
}

export function AgentsPanel({
  agentGroups,
  agents,
  sessionId,
  open,
  onClose,
  focusedGroupId,
}: AgentsPanelProps) {
  const translation = useTranslations("AgentsPanel");
  const cardRefs = useRef<Map<string, HTMLDivElement>>(new Map());
  const agentLabels = new Map(agents.map((agent) => [agent.id, agent.title || agent.name]));

  useEffect(() => {
    if (!open || !focusedGroupId) return;
    const node = cardRefs.current.get(focusedGroupId);
    node?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [open, focusedGroupId]);

  // Fills its tile (the PanelTiles grid owns size + placement); keeps only its own
  // card surface (bg.panel + border + radius).
  return (
    <PanelCard>
      <PanelHeader
        icon={<LuNetwork size={14} />}
        title={translation("agents")}
        onClose={onClose}
        closeLabel={translation("collapseSidebar")}
      />

      <PanelBody px={4}>
        {agentGroups.length === 0 ? (
          <PanelEmptyState
            icon={<LuBot />}
            title={translation("noActivityTitle")}
            description={translation("noActivityDescription")}
          />
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
                      sessionId={sessionId}
                    />
                  ))}
                </Flex>
              </Box>
            ))}
          </Flex>
        )}
      </PanelBody>
    </PanelCard>
  );
}
