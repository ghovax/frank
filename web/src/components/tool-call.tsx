"use client";

import { Box, Flex } from "@chakra-ui/react";
import type { ReactNode } from "react";
import { useTranslations } from "next-intl";
import { getToolCallDisplay, requestedAccess, type ToolDisplayTranslator } from "@/lib/tool-display";
import { callMayMutate } from "@shared/tools";
import { ToolCallLabel } from "./tool-label";
import type { ToolEvent, ToolEventStatus } from "@/lib/tool-event";
import { hasBackgroundJobId } from "@/lib/tool-event";
import { ToolCallView, ToolResultView } from "./tool-views";
import { FieldScope } from "./ui/display";
import { Pill } from "./ui/pill";
import { DisclosureLabel, DisclosureRow } from "./ui/disclosure-row";
import { STATUS_PALETTE, toolStatusKind } from "@/lib/status";
import { asRecord } from "@/lib/coerce";

// The location a filesystem/shell tool ran against, as a compact badge — but only when it is *remote*.
function toolLocationBadge(value: unknown): { label: string; palette: "blue" } | null {
  if (typeof value !== "string" || !value.startsWith("ssh://")) return null;
  const authority = value.slice("ssh://".length).split("/")[0];
  return { label: authority || value, palette: "blue" };
}

function ToolLocationBadge({ arguments: args }: { arguments?: Record<string, unknown> }) {
  const info = toolLocationBadge(args?.location);
  if (!info) return null;
  return <Pill colorPalette={info.palette}>{info.label}</Pill>;
}

// A tool call's live status as a pill (colour from the shared status palette).
export function ToolStatusBadge({ status }: { status: ToolEventStatus }) {
  const translation = useTranslations("ToolCard");
  if (status === "completed" || status === "done") return null;
  const labelKey = status === "input_required" ? "inputRequired" : status === "failed" ? "failed" : "running";
  return <Pill colorPalette={STATUS_PALETTE[toolStatusKind(status)]}>{translation(labelKey)}</Pill>;
}

// Always-visible markers for a tool call: a write badge when the call can change something, and an access badge when it asked to reach past its confinement.
export function ToolAccessBadges({ name, arguments: toolArguments }: { name?: string; arguments?: Record<string, unknown> }) {
  const translation = useTranslations("ToolCard");
  if (!toolArguments) return null;
  const readOnly = !callMayMutate(name ?? "", toolArguments);
  const access = requestedAccess(toolArguments);
  const badges: ReactNode[] = [];
  if (!readOnly) badges.push(<Pill key="write" colorPalette="orange">{translation("write")}</Pill>);
  if (access.any) badges.push(<Pill key="access" colorPalette="purple">{translation("accessRequested")}</Pill>);
  if (badges.length === 0) return null;
  return <>{badges}</>;
}

// The location to badge on a collapsed heading that summarizes several calls.
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
  actions?: ReactNode;
}

function isToolErrorResult(content: string | null): boolean {
  if (!content) return false;
  try {
    const parsed = JSON.parse(content);
    return !!parsed && typeof parsed === "object" && !Array.isArray(parsed) && (parsed as Record<string, unknown>).code === "tool_error";
  } catch {
    // Not JSON — the plain text is what gets shown, which is the point of trying.
    return false;
  }
}

// Whether ToolResultView will actually render something inline for this result.
function resultRendersInside(name: string, content: string, status: ToolEventStatus | undefined): boolean {
  if (name === "list_mcp_tools" || name === "list_mcp_resources") return false;
  let parsed: unknown;
  try {
    parsed = JSON.parse(content);
  } catch {
    // Non-JSON string results (plain text) always have something to show.
    return true;
  }
  const record = asRecord(parsed);
  if (status === "running" && hasBackgroundJobId(record)) return false;
  const code = String(record.code ?? "");
  if (code === "empty_response" && !record.message) return false;
  return true;
}

// The single source of truth for what a tool line expands into: whether its call arguments and/or its result have anything to show inside the collapsible, and thus whether the line is collapsible at all.
export interface ToolCallDetail {
  showArguments: boolean;
  showResult: boolean;
  collapsible: boolean;
}

export function toolCallDetail(
  name: string,
  args: Record<string, unknown> | undefined,
  result: unknown,
  status?: ToolEventStatus,
): ToolCallDetail {
  // `explanation` is rendered as the line's label, and `location` as a trailing badge — neither is body content, so a call carrying only those has nothing to expand into.
  const showArguments = !!args && Object.keys(args).some((key) => key !== "explanation" && key !== "location");
  const resultContent = result == null ? null : typeof result === "string" ? result : JSON.stringify(result);
  // A tool_error is surfaced on the line itself and leaves nothing for the body.
  const showResult =
    resultContent != null && !isToolErrorResult(resultContent) && resultRendersInside(name, resultContent, status);
  // The task list is the model's own internal bookkeeping — its line never exposes the raw task entries, so it is never collapsible regardless of its arguments.
  const isInternalPlanning = name === "set_tasks" || name === "update_tasks";
  return { showArguments, showResult, collapsible: !isInternalPlanning && (showArguments || showResult) };
}

// What a tool line expands into: the call's arguments, then its result.
export function ToolCallDetail({ name, arguments: toolArguments, result, status }: ToolEvent) {
  const { showArguments, showResult } = toolCallDetail(name, toolArguments, result, status);
  const resultContent = result == null ? null : typeof result === "string" ? result : JSON.stringify(result);
  return (
    // gap matches FieldList's own field spacing so the call's last field (e.g.
    <Flex direction="column" gap={2} align="stretch">
      {/* One scope around both halves: a field the arguments already showed does not render
          again in the result. See `FieldScope` — the guard is the primitive, not the view. */}
      <FieldScope>
        {showArguments && <ToolCallView name={name} args={toolArguments} />}
        {showResult && <ToolResultView name={name} content={resultContent ?? ""} status={status} />}
      </FieldScope>
    </Flex>
  );
}

// A tool call is a line of activity, not a card: icon + label at the same type scale as the surrounding markdown, with its badges hugging the text.
export function ToolCall({ name, arguments: toolArguments, result, status, actions }: ToolCallProps) {
  const translation = useTranslations("ToolCall");
  // One decision, shared with every other tool-line surface: what (if anything) this line expands into.
  const { collapsible } = toolCallDetail(name, toolArguments, result, status);
  // A running call whose interim result says the work moved to the background.
  const background = status === "running" && hasBackgroundJobId(result);
  const tDisplay = useTranslations("ToolDisplay") as unknown as ToolDisplayTranslator;
  const { icon: Icon, iconColor } = getToolCallDisplay(name, toolArguments, tDisplay);

  return (
    <DisclosureRow
      // input_required tints the whole line; otherwise it settles muted and brightens on open/hover — the one colour rule DisclosureRow owns.
      tone={status === "input_required" ? "attention" : "muted"}
      maxH="480px"
      icon={<Box color={iconColor} display="flex" alignItems="center"><Icon /></Box>}
      title={
        <DisclosureLabel shimmer={status === "running"}>
          <ToolCallLabel name={name} args={toolArguments} />
        </DisclosureLabel>
      }
      badges={
        <>
          <ToolLocationBadge arguments={toolArguments} />
          <ToolAccessBadges name={name} arguments={toolArguments} />
          {status === "running" || status === "completed" || status === "failed" || status === "input_required" ? <ToolStatusBadge status={status} /> : null}
          {background ? <Pill colorPalette={STATUS_PALETTE.background}>{translation("background")}</Pill> : null}
        </>
      }
      actions={actions}
    >
      {collapsible ? (
        <ToolCallDetail name={name} arguments={toolArguments} result={result} status={status} />
      ) : undefined}
    </DisclosureRow>
  );
}
