import { asRecord } from "./coerce";

export type ToolEventStatus = "running" | "completed" | "done" | "failed" | "input_required";

// Runtime event names that do not render as tool cards. `query` was the dispatch
// envelope of older sessions (concrete tools are called natively now); keeping it
// hidden means replayed transcripts from before the change still render cleanly.
const HIDDEN_TOOL_EVENT_NAMES: ReadonlySet<string> = new Set(["query"]);

// A human-in-the-loop approval attached to the tool call that triggered it (e.g.
// a sandbox read outside the working directory). Lives on the same card so the
// command — and, once approved, its output — read together.
export type PermissionDecision = "deny" | "allow_once" | "allow_always";

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

export function isHiddenToolEventName(name: unknown): boolean {
  return HIDDEN_TOOL_EVENT_NAMES.has(String(name ?? ""));
}

// Narrow an arbitrary value to a known tool-event status (or undefined) — for the
// raw status strings that arrive on wire events.
export function toolStatus(status: unknown): ToolEventStatus | undefined {
  return status === "running" || status === "completed" || status === "done" || status === "failed" || status === "input_required"
    ? status
    : undefined;
}

// A running call whose (interim) result says the work moved to the background — its
// result is a "*_started" (or scheduled) placeholder rather than the real output.
export function isBackgroundResult(result: unknown): boolean {
  const code = String(asRecord(result).code ?? "");
  return code.endsWith("_started") || code === "background_task_scheduled";
}
