import { asRecord } from "./coerce";

export type ToolEventStatus = "running" | "completed" | "done" | "failed" | "input_required";

// An approval attached to the tool call that triggered it, so the command and the question sit together.
export type PermissionDecision = "deny" | "allow_once";

// Why approval is needed, as facts rather than a finished sentence, so the interface writes it in its own language.
export interface PermissionReason {
  kind: string;
  paths?: string[];
}

export interface ToolPermission {
  requestId: string;
  // Prose the harness did not author, untranslatable by construction and shown as it came.
  explanation?: string;
  reason?: PermissionReason;
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

// One answer per question: a label, a list of labels, or the text the user typed.
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

/** Whether a call is finished, and so whether its arguments are whole rather than mid-stream. */
export function isSettled(event: { result?: unknown; status?: ToolEventStatus }): boolean {
  return event.result !== undefined || event.status !== "running";
}

export function isSameToolEvent(event: ToolEvent, name: string, toolCallId: string): boolean {
  const idMatches = !!toolCallId && event.toolCallId === toolCallId;
  const fallbackMatches = !toolCallId && event.result == null && event.name === name;
  return idMatches || fallbackMatches;
}

// Narrow an arbitrary value to a known status, for the raw strings that arrive on wire events.
export function toolStatus(status: unknown): ToolEventStatus | undefined {
  return status === "running" ||
    status === "completed" ||
    status === "done" ||
    status === "failed" ||
    status === "input_required"
    ? status
    : undefined;
}

export function hasBackgroundJobId(result: unknown): boolean {
  return String(asRecord(result).job_id ?? "").trim().length > 0;
}

// The structured reason as a sentence in the caller's language, or empty when there is none.
export type PermissionReasonTranslator = (
  key: "reasonReadsOutsideWorkspace" | "reasonAccessRequest",
  values: { count: number },
) => string;

export function permissionReasonText(
  reason: PermissionReason | undefined,
  translation: PermissionReasonTranslator,
): string {
  if (!reason?.kind) return "";
  const count = (reason.paths ?? []).filter(Boolean).length;
  switch (reason.kind) {
    case "reads_outside_workspace":
      return translation("reasonReadsOutsideWorkspace", { count });
    case "access_request":
      return translation("reasonAccessRequest", { count });
    default:
      // An unknown kind is a newer harness talking to an older interface, so nothing is said.
      return "";
  }
}

// The paths a reason names, for the list that renders beside its sentence.
export function permissionReasonPaths(reason: PermissionReason | undefined): string[] {
  return (reason?.paths ?? []).filter(Boolean);
}
