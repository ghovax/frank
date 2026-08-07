"use client";

import { Box, Flex, Text } from "@chakra-ui/react";
import { memo, useMemo, useState, type ReactNode } from "react";
import { LuBrain } from "react-icons/lu";
import { AnimatePresence, motion } from "motion/react";
import { useTranslations } from "next-intl";
import { getToolCallDisplay } from "@/lib/glyphs";
import { STATUS_ICON, STATUS_PALETTE, type StatusKind } from "@/lib/status";
import { RollingNumber } from "./rolling-number";
import { ToolCallLabel } from "./tool-label";
import { Pill } from "./ui/pill";
import { DisclosureRow } from "./ui/disclosure-row";
import type { ToolEvent } from "@/lib/tool-event";
import { hasBackgroundJobId, toolStatus } from "@/lib/tool-event";
import {
  ToolCall,
  ToolCallDetail,
  ToolLocationBadge,
  ToolAccessBadges,
  collapsedHeadingLocation,
  toolCallDetail,
} from "./tool-call";

// The grouped, collapsible run of contiguous tool calls, and the single source of truth for how a batch reads.

// Tally tools by name while preserving first-seen order.
function tallyTools(tools: ToolEvent[]): { order: string[]; counts: Map<string, number> } {
  const order: string[] = [];
  const counts = new Map<string, number>();
  for (const tool of tools) {
    const name = tool.name || "unknown";
    if (!counts.has(name)) order.push(name);
    counts.set(name, (counts.get(name) ?? 0) + 1);
  }
  return { order, counts };
}

// One tally chip in the heading, carrying both the icon and its count so each reads as a single unit.
function TallyBadge({
  icon,
  count,
  colorPalette = "gray",
  title,
  alwaysShowCount = false,
}: {
  icon: ReactNode;
  count: number;
  colorPalette?: string;
  title?: string;
  alwaysShowCount?: boolean;
}) {
  return (
    <Pill colorPalette={colorPalette} title={title} icon={icon}>
      {alwaysShowCount || count > 1 ? <RollingNumber value={count} /> : null}
    </Pill>
  );
}

// Map a tool's icon colour to a palette, so each tally badge carries that tool's own accent.
function paletteFromIconColor(iconColor: string): string {
  return iconColor.endsWith(".fg") ? iconColor.slice(0, -3) : "gray";
}

interface ToolGroupProps {
  tools: ToolEvent[];
  // Keeps the group expanded after its calls complete, so the latest one stays open until the answer arrives.
  keepOpen?: boolean;
}

export const ToolGroup = memo(function ToolGroup({ tools, keepOpen = false }: ToolGroupProps) {
  const translation = useTranslations("ToolGroup");
  const backgroundCount = tools.filter(
    (tool) => toolStatus(tool.status) === "running" && hasBackgroundJobId(tool.result),
  ).length;
  const runningCount =
    tools.filter((tool) => toolStatus(tool.status) === "running").length - backgroundCount;
  const inputRequiredCount = tools.filter(
    (tool) => toolStatus(tool.status) === "input_required",
  ).length;
  const inputRequired = inputRequiredCount > 0;
  const failedCount = tools.filter((tool) => toolStatus(tool.status) === "failed").length;
  const active = runningCount > 0 || backgroundCount > 0 || inputRequired || keepOpen;
  // Tri-state, so the group can be toggled either way from its default: null follows the default.
  const [manualOverride, setManualOverride] = useState<boolean | null>(null);
  const bodyOpen = manualOverride ?? false;

  // The left icon owns the latest call, so the tally counts only the earlier ones.
  const tally = useMemo(() => tallyTools(tools.slice(0, -1)), [tools]);
  // The status line shows the latest tool's own label, which says more than a static "working".
  const latestTool = tools[tools.length - 1];
  // The newest call has nothing to say yet, so the line follows the newest one that does and never empties.
  const labelledTool = useMemo(
    () => [...tools].reverse().find((tool) => getToolCallDisplay(tool.name, tool.arguments).label),
    [tools],
  );
  const headingDisplay = latestTool
    ? getToolCallDisplay(latestTool.name, latestTool.arguments)
    : null;
  const HeadingIcon = headingDisplay?.icon ?? LuBrain;
  const headingIconColor = headingDisplay?.iconColor ?? "purple.fg";
  // Badge the collapsed heading when the batch touched a single remote place, since local is the implied default.
  const groupLocation = useMemo(
    () => collapsedHeadingLocation(tools.map((tool) => tool.arguments)),
    [tools],
  );
  const latestLabel = labelledTool
    ? getToolCallDisplay(labelledTool.name, labelledTool.arguments).label
    : "";
  // A tools-less group is a "thinking before acting" phase and owns the leading brain icon.
  const thinkingOnly = tools.length === 0;
  const headingText =
    latestLabel ||
    (thinkingOnly
      ? translation("thinking")
      : active
        ? translation("working")
        : translation("actionsTaken"));
  // A group of one skips the per-call line and opens straight onto that call's detail.
  const soleTool = tools.length === 1 ? tools[0] : null;
  const soleDetail = soleTool
    ? toolCallDetail(soleTool.name, soleTool.arguments, soleTool.result, soleTool.status)
    : null;
  // Any call can be opened, including a single one, since the row carries the label rather than the arguments.
  const interactive = soleTool ? !!soleDetail?.collapsible : tools.length > 0;

  // Status chips surface only the states needing attention; running and completed calls speak for themselves.
  const statusChips = [
    inputRequiredCount > 0 && {
      kind: "input_required" as StatusKind,
      count: inputRequiredCount,
      title: translation("inputRequired"),
    },
    failedCount > 0 && {
      kind: "failed" as StatusKind,
      count: failedCount,
      title: translation("failedCount", { count: failedCount }),
    },
    backgroundCount > 0 && {
      kind: "background" as StatusKind,
      count: backgroundCount,
      title: translation("backgroundCount", { count: backgroundCount }),
    },
  ].filter((chip): chip is { kind: StatusKind; count: number; title: string } => Boolean(chip));

  // The animated label slot, with both labels in one grid cell so nothing reflows as they crossfade.
  const titleSlot = (
    // The shimmer belongs to this box rather than the label, whose key changes as each new tool crossfades in.
    <Box
      minW={0}
      display="grid"
      gridTemplateColumns="minmax(0, 1fr)"
      position="relative"
      className={active ? "running-title-shimmer" : undefined}
    >
      {/* No cross-fade: a label that is simply replaced is easier to read while the explanation changes every second. */}
      <Box gridArea="1 / 1" minW={0} display="flex" alignItems="center">
        <Text
          textStyle="sm"
          fontWeight="normal"
          whiteSpace="nowrap"
          overflow="hidden"
          textOverflow="ellipsis"
        >
          {labelledTool ? (
            <ToolCallLabel name={labelledTool.name} args={labelledTool.arguments} />
          ) : (
            headingText
          )}
        </Text>
      </Box>
    </Box>
  );

  // The heading's chip cluster: tallies, file changes, the remote badge and status chips, all animated.
  const hasBadges =
    tally.order.length > 0 || statusChips.length > 0 || !!groupLocation || !!soleTool;
  const badgeSlot = (
    <>
      {/* The write and access markers of a lone call move up to the heading rather than disappearing with its line. */}
      {soleTool ? <ToolAccessBadges name={soleTool.name} arguments={soleTool.arguments} /> : null}
      <AnimatePresence initial={false}>
        {tally.order.map((name) => {
          const display = getToolCallDisplay(name, undefined);
          const ToolIcon = display.icon;
          const count = tally.counts.get(name) ?? 0;
          return (
            <motion.div
              key={name}
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.12, ease: "easeOut" }}
              style={{ display: "inline-flex", alignItems: "center" }}
            >
              <TallyBadge
                title={display.label}
                count={count}
                colorPalette={paletteFromIconColor(display.iconColor)}
                icon={<ToolIcon />}
                alwaysShowCount
              />
            </motion.div>
          );
        })}
      </AnimatePresence>
      <ToolLocationBadge arguments={groupLocation} />
      <AnimatePresence initial={false}>
        {statusChips.map(({ kind, count, title }) => {
          const palette = STATUS_PALETTE[kind];
          const ChipIcon = STATUS_ICON[kind];
          return (
            <motion.div
              key={kind}
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.12, ease: "easeOut" }}
              style={{ display: "inline-flex", alignItems: "center" }}
            >
              <TallyBadge
                title={title}
                count={count}
                colorPalette={palette}
                icon={ChipIcon ? <ChipIcon /> : null}
              />
            </motion.div>
          );
        })}
      </AnimatePresence>
    </>
  );

  return (
    <Box alignSelf="flex-start" w="100%" minW={0}>
      <DisclosureRow
        open={bodyOpen}
        onOpenChange={
          interactive
            ? () => setManualOverride((current) => (current === null ? true : !current))
            : undefined
        }
        tone={active ? "active" : "muted"}
        maxH={80}
        followTailKey={tools.length}
        icon={
          <Box color={headingIconColor}>
            <HeadingIcon />
          </Box>
        }
        title={titleSlot}
        // `undefined` rather than an empty fragment, which is truthy and would render an empty badge row.
        badges={hasBadges ? badgeSlot : undefined}
      >
        {!interactive ? undefined : soleTool ? (
          <ToolCallDetail
            name={soleTool.name}
            arguments={soleTool.arguments}
            result={soleTool.result}
            toolCallId={soleTool.toolCallId}
            status={soleTool.status}
          />
        ) : (
          <Flex direction="column" gap={1}>
            {tools.map((tool, index) => (
              <ToolCall
                key={tool.toolCallId || `tool-${index}`}
                name={tool.name}
                arguments={tool.arguments}
                result={tool.result}
                toolCallId={tool.toolCallId}
                status={tool.status}
                permission={tool.permission}
                question={tool.question}
              />
            ))}
          </Flex>
        )}
      </DisclosureRow>
    </Box>
  );
});
