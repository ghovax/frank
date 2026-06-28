export type ToolEventStatus = "running" | "completed" | "done" | "failed";

const CONTROL_TOOL_NAMES: ReadonlySet<string> = new Set(["update_goal"]);

export interface ToolEvent {
  name: string;
  arguments?: Record<string, unknown>;
  sequenceNumber?: number;
  toolCallId?: string;
  result?: unknown;
  status?: ToolEventStatus;
}

export function isSameToolEvent(event: ToolEvent, name: string, toolCallId: string): boolean {
  const idMatches = !!toolCallId && event.toolCallId === toolCallId;
  const fallbackMatches = !toolCallId && event.result == null && event.name === name;
  return idMatches || fallbackMatches;
}

export function nextToolSequence(events: readonly ToolEvent[]): number {
  return events.length + 1;
}

export function isControlToolName(name: unknown): boolean {
  return CONTROL_TOOL_NAMES.has(String(name ?? ""));
}
