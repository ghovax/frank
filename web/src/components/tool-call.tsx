"use client";

import { Box, Flex } from "@chakra-ui/react";
import type { ReactNode } from "react";
import { useTranslations } from "next-intl";
import { getToolCallDisplay } from "@/lib/tool-display";
import { ToolCallLabel } from "./tool-label";
import type { PermissionDecision, QuestionAnswer, ToolEvent, ToolEventStatus } from "@/lib/tool-event";
import { hasBackgroundTaskIdentifier } from "@/lib/tool-event";
import { BrowserActionBadge, ComputerActionBadge, ToolCallView, ToolResultView, extractToolArtifacts } from "./tool-views";
import { Pill } from "./ui/pill";
import { DisclosureLabel, DisclosureRow } from "./ui/disclosure-row";
import { STATUS_PALETTE, toolStatusKind } from "@/lib/status";
import { asRecord } from "@/lib/coerce";

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
  return <Pill colorPalette={info.palette}>{info.label}</Pill>;
}

// A tool call's live status as a pill (colour from the shared status palette). A
// completed call carries no badge — its settled line speaks for itself.
export function ToolStatusBadge({ status }: { status: ToolEventStatus }) {
  const t = useTranslations("ToolCard");
  if (status === "completed" || status === "done") return null;
  const labelKey = status === "input_required" ? "inputRequired" : status === "failed" ? "failed" : "running";
  return <Pill colorPalette={STATUS_PALETTE[toolStatusKind(status)]}>{t(labelKey)}</Pill>;
}

// Always-visible safety markers for a tool call: a write badge when it can modify
// state (read_only === false), and its risk level when medium/high. Read-only /
// low-risk calls stay bare.
export function ToolRiskBadges({ arguments: toolArguments }: { arguments?: Record<string, unknown> }) {
  const t = useTranslations("ToolCard");
  if (!toolArguments) return null;
  const readOnly = toolArguments.read_only !== false;
  const risk = typeof toolArguments.risk === "string" ? toolArguments.risk : "";
  const badges: ReactNode[] = [];
  if (!readOnly) badges.push(<Pill key="write" colorPalette="orange">{t("write")}</Pill>);
  if (risk === "medium" || risk === "high") {
    badges.push(
      <Pill key="risk" colorPalette={risk === "high" ? "red" : "yellow"}>
        {risk === "high" ? t("highRisk") : t("mediumRisk")}
      </Pill>,
    );
  }
  if (badges.length === 0) return null;
  return <>{badges}</>;
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
  actions?: ReactNode;
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

// Whether ToolResultView will actually render something inline for this result.
// It mirrors the null-return paths in ToolResultView so an expanded line never
// shows an empty bordered rail (which otherwise leaves a gap below the line):
// a few tool names render nothing inline, and background/started/empty results
// carry no body to show.
function resultRendersInside(name: string, content: string, status: ToolEventStatus | undefined): boolean {
  if (name === "list_mcp_tools" || name === "list_mcp_resources" || name === "open_artifact") return false;
  let parsed: unknown;
  try {
    parsed = JSON.parse(content);
  } catch {
    // Non-JSON string results (plain text) always have something to show.
    return true;
  }
  const record = asRecord(parsed);
  if (status === "running" && hasBackgroundTaskIdentifier(record)) return false;
  const code = String(record.code ?? "");
  if (code === "empty_response" && !record.message) return false;
  return true;
}

// The single source of truth for what a tool line expands into: whether its call
// arguments and/or its result have anything to show inside the collapsible, and thus
// whether the line is collapsible at all. A line with no showable detail must NOT be
// made expandable — otherwise the chevron opens onto an empty bordered rail. Exported
// so every surface that renders a tool line (the transcript ToolCall, a grouped run)
// makes the same decision rather than each re-deriving it and drifting.
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
  // `justification` is rendered as the line's label, and `location` as a trailing
  // badge — neither is body content, so a call carrying only those has nothing to
  // expand into.
  const showArguments = !!args && Object.keys(args).some((key) => key !== "justification" && key !== "location");
  const resultContent = result == null ? null : typeof result === "string" ? result : JSON.stringify(result);
  // Renderable artifacts (e.g. a map) render outside the line and stay visible; a
  // tool_error is surfaced on the line itself; both leave nothing for the body.
  const artifacts = resultContent ? extractToolArtifacts(name, resultContent) : [];
  const showResult =
    resultContent != null && artifacts.length === 0 && !isToolErrorResult(resultContent) && resultRendersInside(name, resultContent, status);
  // The task list is the model's own internal bookkeeping — its line never exposes
  // the raw task entries, so it is never collapsible regardless of its arguments.
  const isInternalPlanning = name === "set_tasks" || name === "update_tasks";
  return { showArguments, showResult, collapsible: !isInternalPlanning && (showArguments || showResult) };
}

// A tool call is a line of activity, not a card: icon + label at the same type
// scale as the surrounding markdown, with its badges hugging the text. Expanding
// hangs the structured detail off a hairline left rule — the same visual grammar
// as a blockquote — so a run of calls reads as an annotated ledger inside the
// prose rather than a stack of boxes interrupting it.
export function ToolCall({ name, arguments: toolArguments, result, status, agents = [], actions }: ToolCallProps) {
  const t = useTranslations("ToolCall");
  // One decision, shared with every other tool-line surface: what (if anything) this
  // line expands into. A line with nothing to show is not collapsible (DisclosureRow
  // enforces that from the presence of body children), so it never opens an empty rail.
  const { showArguments, showResult, collapsible } = toolCallDetail(name, toolArguments, result, status);
  const resultContent = result == null ? null : typeof result === "string" ? result : JSON.stringify(result);
  // A running call whose interim result says the work moved to the background.
  const background = status === "running" && hasBackgroundTaskIdentifier(result);
  const { icon: Icon, iconColor } = getToolCallDisplay(name, toolArguments);

  return (
    <DisclosureRow
      // input_required tints the whole line; otherwise it settles muted and brightens
      // on open/hover — the one colour rule DisclosureRow owns.
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
          {name === "computer" && <ComputerActionBadge action={toolArguments?.action ? String(toolArguments.action) : undefined} />}
          {name === "browser" && <BrowserActionBadge action={toolArguments?.action ? String(toolArguments.action) : undefined} />}
          <ToolLocationBadge arguments={toolArguments} />
          <ToolRiskBadges arguments={toolArguments} />
          {status === "running" || status === "completed" || status === "failed" || status === "input_required" ? <ToolStatusBadge status={status} /> : null}
          {background ? <Pill colorPalette={STATUS_PALETTE.background}>{t("background")}</Pill> : null}
        </>
      }
      actions={actions}
    >
      {collapsible ? (
        // gap matches FieldList's own field spacing so the call's last field (e.g. Risk)
        // and the result's first (e.g. PID) read as one list.
        <Flex direction="column" gap={2} align="stretch">
          {showArguments && <ToolCallView name={name} args={toolArguments} agents={agents} />}
          {showResult && <ToolResultView name={name} content={resultContent ?? ""} args={toolArguments} status={status} />}
        </Flex>
      ) : undefined}
    </DisclosureRow>
  );
}
