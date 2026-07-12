"use client";

import { Badge, Box, Flex } from "@chakra-ui/react";
import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { LuBot, LuNetwork } from "react-icons/lu";
import type { ToolEvent } from "@/lib/tool-event";
import type { AgentStep, AgentGroup, TaskState } from "@/lib/use-chat";
import { AgentTimeline } from "./agent-timeline";
import { ToolLocationBadge, collapsedHeadingLocation } from "./tool-call";
import { ToolCard, ToolCardBody, ToolCardHeader } from "./tool-card";
import { PanelCard, PanelHeader, PanelBody, PanelEmptyState } from "@/components/ui/panel";

// Maps an A2A TaskState to a status badge. Completed steps carry no badge —
// the settled state is self-evident from the card. Only non-default states show.
function AgentStateBadge({ state }: { state: TaskState }) {
  const t = useTranslations("AgentsPanel");
  if (state === "completed") return null;
  const { label, palette } =
    state === "failed"
      ? { label: t("stateFailed"), palette: "red" }
      : state === "rejected"
        ? { label: t("stateRejected"), palette: "red" }
        : state === "canceled"
          ? { label: t("stateCanceled"), palette: "gray" }
          : state === "input-required"
            ? { label: t("stateInputRequired"), palette: "yellow" }
            : state === "auth-required"
              ? { label: t("stateAuthRequired"), palette: "yellow" }
              : state === "working"
                ? { label: t("stateWorking"), palette: "blue" }
                : { label: t("stateSubmitted"), palette: "blue" };
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
  const t = useTranslations("AgentsPanel");
  const [open, setOpen] = useState(true);
  // The remote the step ran against (if any) — surfaced on the step's own (top)
  // collapsible, mirroring the grouped tool-call heading.
  const stepLocation = collapsedHeadingLocation(
    step.parts.filter((part) => part.kind === "tool").map((part) => (part as ToolEvent).arguments),
  );

  return (
    <ToolCard>
      <ToolCardHeader
        icon={<Box color="fg.muted"><LuBot size={12} /></Box>}
        title={step.goal || t("agentTask")}
        badges={
          <>
            <Badge size="sm" variant="subtle" colorPalette="gray" borderRadius="sm" flexShrink={0}>
              {agentLabel || t("agent")}
            </Badge>
            <ToolLocationBadge arguments={stepLocation} />
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
}: AgentsPanelProps) {
  const t = useTranslations("AgentsPanel");
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
        title={t("agents")}
        onClose={onClose}
        closeLabel={t("collapseSidebar")}
      />

      <PanelBody>
        {agentGroups.length === 0 ? (
          <PanelEmptyState
            icon={<LuBot />}
            title={t("noActivityTitle")}
            description={t("noActivityDescription")}
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
