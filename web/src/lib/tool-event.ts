import { asRecord } from "./coerce";

export type ToolEventStatus = "running" | "completed" | "done" | "failed" | "input_required";

// A human-in-the-loop approval attached to the tool call that triggered it (e.g.
// a sandbox read outside the working directory). Lives on the same card so the
// command — and, once approved, its output — read together.
export type PermissionDecision = "deny" | "allow_once";

// Why approval is needed, as facts rather than as a finished sentence. The harness sends
// this instead of English prose so the interface writes the sentence in its own language;
// `kind` selects the message and `paths` are interpolated into it.
export interface PermissionReason {
  kind: string;
  paths?: string[];
}

export interface ToolPermission {
  requestId: string;
  // Prose the harness did not author — the reviewer's verdict, or the model's own account of
  // itself. Untranslatable by construction, and shown as it came.
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

export function hasBackgroundJobId(result: unknown): boolean {
  return String(asRecord(result).job_id ?? "").trim().length > 0;
}

// The harness's structured reason as a sentence, in the caller's language, or "" when there
// is none or the interface does not recognise the kind.
//
// This exists so that no user-facing sentence is ever assembled on the server. The daemon has
// no locale: prose built there reaches every interface in one language and never passes
// through the message catalogue, which is how "Sandbox approval required: …" ended up inside
// a Japanese window. The harness sends `kind` and the paths; whoever draws the prompt writes
// the sentence.
//
// The sentence only. The paths are *data*, and they render as a list beside it — never joined
// into the message. A separator is a locale's decision, not a component's, and a set of paths
// flattened into one string reads as one value when it is several.
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
      // An unknown kind is a newer harness talking to an older interface. Saying nothing lets
      // the model's own explanation stand, which is better than inventing a sentence for a
      // reason this build does not understand.
      return "";
  }
}

// The paths a reason names, for the list that renders beside its sentence.
export function permissionReasonPaths(reason: PermissionReason | undefined): string[] {
  return (reason?.paths ?? []).filter(Boolean);
}
