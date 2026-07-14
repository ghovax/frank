"use client";

import { Badge, Box, Flex } from "@chakra-ui/react";
import { useState } from "react";
import { LuChevronDown, LuChevronRight } from "react-icons/lu";
import { useTranslations } from "next-intl";
import { getToolCallDisplay } from "@/lib/tool-display";
import { ToolCallLabel } from "./tool-label";
import type { PermissionDecision, QuestionAnswer, ToolEvent } from "@/lib/tool-event";
import { BrowserActionBadge, ComputerActionBadge, ToolCallView, ToolResultView, extractToolArtifacts } from "./tool-views";
import { Pill } from "./tool-views/primitives";
import { ToolRiskBadges, ToolStatusBadge } from "./tool-card";

// The location a filesystem/shell tool ran against, as a compact badge — but only when it
// is *remote*. Local runs (`file://…`) get no badge: the absence of a badge already reads as
// "here", so a "Local" tag would be pure noise, and a remote badge only ever appears in a
// project that actually spans machines. A remote (`ssh://host/path`) shows just the host
// authority (everything after `ssh://` up to the path) — enough to name the machine.
function toolLocationBadge(value: unknown): { label: string; palette: "blue" } | null {
  if (typeof value !== "string" || !value.startsWith("ssh://")) return null;
  const authority = value.slice("ssh://".length).split("/")[0];
  return { label: authority || value, palette: "blue" };
}

function ToolLocationBadge({ arguments: args }: { arguments?: Record<string, unknown> }) {
  const info = toolLocationBadge(args?.location);
  if (!info) return null;
  return <Badge size="sm" variant="subtle" colorPalette={info.palette} borderRadius="sm" flexShrink={0}>{info.label}</Badge>;
}

// The location to badge on a collapsed heading that summarizes several calls. A remote is
// the notable case (local is the implied default), so surface it: if the batch touched
// exactly one remote place, badge that — even when local calls sit alongside it. A purely
// local batch needs no heading badge; multiple distinct remotes defer to the expanded
// per-call badges.
export function collapsedHeadingLocation(argumentsList: (Record<string, unknown> | undefined)[]): Record<string, unknown> | undefined {
  const remotes = new Map<string, Record<string, unknown>>();
  for (const args of argumentsList) {
    const location = args?.location;
    if (typeof location === "string" && location.startsWith("ssh://")) remotes.set(location, args!);
  }
  return remotes.size === 1 ? [...remotes.values()][0] : undefined;
}

export { ToolLocationBadge };

interface ToolCallProps extends ToolEvent {
  agents?: { id: string; name: string; title?: string }[];
  onPermission?: (requestId: string, decision: PermissionDecision) => void;
  onQuestion?: (requestId: string, answers: QuestionAnswer[]) => void;
  // The single live artifact id (owned by ChatPanel). Only the matching
  // iframe-type artifact mounts its frame; the rest collapse to a placeholder.
  activeArtifactId?: string | null;
  onActivateArtifact?: (id: string) => void;
}

function isToolErrorResult(content: string | null): boolean {
  if (!content) return false;
  try {
    const parsed = JSON.parse(content);
    return !!parsed && typeof parsed === "object" && !Array.isArray(parsed) && (parsed as Record<string, unknown>).code === "tool_error";
  } catch {
    return false;
  }
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

// A running call whose (interim) result says the work moved to the background. Exported so
// the group header can count backgrounded calls separately from foreground-running ones.
export function isBackgroundResult(result: unknown): boolean {
  const code = String(asRecord(result).code ?? "");
  return code.endsWith("_started") || code === "background_task_scheduled";
}

// A tool call is a line of activity, not a card: icon + label at the same type
// scale as the surrounding markdown, with its badges hugging the text. Expanding
// hangs the structured detail off a hairline left rule — the same visual grammar
// as a blockquote — so a run of calls reads as an annotated ledger inside the
// prose rather than a stack of boxes interrupting it.
export function ToolCall({ name, arguments: toolArguments, result, status, agents = [] }: ToolCallProps) {
  const t = useTranslations("ToolCall");
  const [open, setOpen] = useState(false);
  const hasArguments = !!toolArguments && Object.keys(toolArguments).length > 0;
  const resultContent = result == null ? null : typeof result === "string" ? result : JSON.stringify(result);
  // Renderable artifacts (e.g. a map) render outside the line and stay visible;
  // the textual result stays inside the collapsible detail. When the result is an
  // artifact, there is no separate text to show inside.
  const artifacts = resultContent ? extractToolArtifacts(name, resultContent) : [];
  const showResultInside = resultContent != null && artifacts.length === 0 && !isToolErrorResult(resultContent);
  // The task list is the model's own internal bookkeeping. Its line shows only the
  // icon + justification (e.g. "Updating task list") and a status — never an
  // expandable detail exposing the raw task entries.
  const isInternalPlanning = name === "set_tasks" || name === "update_tasks";
  const collapsible = !isInternalPlanning && (hasArguments || showResultInside);
  // A pending approval/question no longer forces the detail open — it is surfaced in
  // an overlay above the composer (see PermissionOverlay / QuestionOverlay). The
  // line only reflects the "input required" status in its badge and tint.
  const background = status === "running" && isBackgroundResult(result);

  const { icon: Icon, iconColor } = getToolCallDisplay(name, toolArguments);

  return (
    <Box minW={0}>
      {/* w=fit-content: the line is only as wide as its text, so the click target and
          hover tint never extend into the blank space to the right of a short label.
          The base color sets the settled (muted) tone; every piece without its own
          color — the label, the chevron — inherits it, so the hover brighten is one rule.
          A collapsible line is a real <button> (like the group heading) so the disclosure
          is keyboard-reachable, not click-only. */}
      <Flex
        as={collapsible ? "button" : "div"}
        align="center"
        gap={1.5}
        h={6}
        w="fit-content"
        maxW="100%"
        minW={0}
        textAlign="left"
        cursor={collapsible ? "pointer" : undefined}
        onClick={collapsible ? () => setOpen((current) => !current) : undefined}
        userSelect="none"
        color={status === "input_required" ? "yellow.fg" : open ? "fg" : "fg.muted"}
        _hover={collapsible ? { color: "fg" } : undefined}
      >
        <Box color={iconColor} display="flex" alignItems="center" flexShrink={0}>
          <Icon size={13} />
        </Box>
        <Box
          minW={0}
          overflow="hidden"
          whiteSpace="nowrap"
          textOverflow="ellipsis"
          textStyle="fieldLabel"
          className={status === "running" ? "running-title-shimmer" : undefined}
        >
          <ToolCallLabel name={name} args={toolArguments} />
        </Box>
        <Flex align="center" gap={1.5} flexShrink={0}>
          {name === "computer" && <ComputerActionBadge action={toolArguments?.action ? String(toolArguments.action) : undefined} />}
          {name === "browser" && <BrowserActionBadge action={toolArguments?.action ? String(toolArguments.action) : undefined} />}
          <ToolLocationBadge arguments={toolArguments} />
          <ToolRiskBadges arguments={toolArguments} />
          {status === "running" || status === "completed" || status === "failed" || status === "input_required" ? <ToolStatusBadge status={status} /> : null}
          {background ? <Pill colorPalette="purple">{t("background")}</Pill> : null}
        </Flex>
        {collapsible && (
          <Box display="flex" alignItems="center" flexShrink={0} opacity={0.7}>
            {open ? <LuChevronDown size={12} /> : <LuChevronRight size={12} />}
          </Box>
        )}
      </Flex>

      {collapsible && open && (
        // ml centers the 2px rule under the 13px icon above it; pl indents the detail
        // clear of the rule so it reads as a quoted aside within the transcript.
        <Box
          className="reveal-enter"
          ml="5px"
          mt={0.5}
          mb={1}
          pl={3}
          borderLeft="2px solid"
          borderColor="border.muted"
          maxH="480px"
          overflowY="auto"
          overflowX="auto"
        >
          {/* gap matches FieldList's own field spacing so the call's last field
              (e.g. Risk) and the result's first (e.g. PID) read as one list. */}
          <Flex direction="column" gap={2} align="stretch">
            {hasArguments && <ToolCallView name={name} args={toolArguments} agents={agents} />}
            {showResultInside && <ToolResultView name={name} content={resultContent ?? ""} args={toolArguments} />}
          </Flex>
        </Box>
      )}
    </Box>
  );
}
