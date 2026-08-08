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
  argumentsComplete?: boolean;
  toolCallId?: string;
  result?: unknown;
  status?: ToolEventStatus;
  permission?: ToolPermission;
  question?: ToolQuestion;
}

/** Whether a call has a complete explanation and may enter the transcript. */
export function toolCallReady(event: ToolEvent): boolean {
  const explanation = event.arguments?.explanation;
  return (
    event.argumentsComplete === true &&
    typeof explanation === "string" &&
    explanation.trim().length > 0
  );
}

export function isSameToolEvent(event: ToolEvent, _name: string, toolCallId: string): boolean {
  return toolCallId.length > 0 && event.toolCallId === toolCallId;
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
  key: "reasonAccessRequest",
  values: { count: number },
) => string;

export function permissionReasonText(
  reason: PermissionReason | undefined,
  translation: PermissionReasonTranslator,
): string {
  if (!reason?.kind) return "";
  const count = (reason.paths ?? []).filter(Boolean).length;
  switch (reason.kind) {
    case "reaches_outside_confinement":
      return translation("reasonAccessRequest", { count });
    default:
      return "";
  }
}

// The paths a reason names, for the list that renders beside its sentence.
export function permissionReasonPaths(reason: PermissionReason | undefined): string[] {
  return (reason?.paths ?? []).filter(Boolean);
}
