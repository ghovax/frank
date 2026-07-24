import { asRecord } from "./coerce";

export type ToolEventStatus = "running" | "completed" | "done" | "failed" | "input_required";

// A human-in-the-loop approval attached to the tool call that triggered it (e.g.
// a sandbox read outside the working directory). Lives on the same card so the
// command — and, once approved, its output — read together.
export type PermissionDecision = "deny" | "allow_once";

export interface ToolPermission {
  requestId: string;
  justification?: string;
  risk?: string;
  decision?: PermissionDecision;
}

export interface QuestionOption {
  label: string;
  description?: string;
}

export interface QuestionItem {
  question: string;
  header?: string;
  options?: QuestionOption[];
  multiple?: boolean;
  // When false, no "type your own answer" field is shown. Defaults to true.
  custom?: boolean;
}

// One answer per question: a selected label, a list of labels (multi-select),
// or the custom text the user typed.
export type QuestionAnswer = string | string[];

export interface ToolQuestion {
  requestId: string;
  questions: QuestionItem[];
  answers?: QuestionAnswer[];
  declined?: boolean;
}

export interface ToolEvent {
  name: string;
  arguments?: Record<string, unknown>;
  toolCallId?: string;
  result?: unknown;
  status?: ToolEventStatus;
  permission?: ToolPermission;
  question?: ToolQuestion;
}

export function isSameToolEvent(event: ToolEvent, name: string, toolCallId: string): boolean {
  const idMatches = !!toolCallId && event.toolCallId === toolCallId;
  const fallbackMatches = !toolCallId && event.result == null && event.name === name;
  return idMatches || fallbackMatches;
}

// Narrow an arbitrary value to a known tool-event status (or undefined) — for the
// raw status strings that arrive on wire events.
export function toolStatus(status: unknown): ToolEventStatus | undefined {
  return status === "running" || status === "completed" || status === "done" || status === "failed" || status === "input_required"
    ? status
    : undefined;
}

export function hasBackgroundTaskIdentifier(result: unknown): boolean {
  return String(asRecord(result).task_identifier ?? "").trim().length > 0;
}
