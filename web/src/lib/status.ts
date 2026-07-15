import type { IconType } from "react-icons";
import { LuCircleAlert, LuCircleX, LuLoaderCircle, LuMoon } from "react-icons/lu";
import type { ToolEventStatus } from "@/lib/tool-event";

// The normalized lifecycle status shared by every surface that shows one — a tool
// call, a grouped run of calls, an agent step, a task row. Each source enum (tool
// event status, A2A task state, task-list lifecycle) maps into this so the colour of
// "running"/"failed"/"input required"/"background" is decided in exactly one place
// and can never drift between the prose badges and the icon chips.
export type StatusKind =
  | "running"
  | "completed"
  | "failed"
  | "input_required"
  | "canceled"
  | "background"
  | "blocked"
  | "pending"
  | "unknown";

// The one palette per status. Both the prose pills and the icon chips read this.
export const STATUS_PALETTE: Record<StatusKind, string> = {
  running: "blue",
  completed: "gray",
  failed: "red",
  input_required: "yellow",
  canceled: "gray",
  background: "purple",
  blocked: "yellow",
  pending: "gray",
  unknown: "gray",
};

// The glyph for statuses that render as icon chips (the tool-group recap). Prose
// surfaces show a label instead and ignore this. `running` is spun by the caller.
export const STATUS_ICON: Partial<Record<StatusKind, IconType>> = {
  input_required: LuCircleAlert,
  failed: LuCircleX,
  running: LuLoaderCircle,
  background: LuMoon,
};

// A live tool call's status (plus whether it was pushed to the background) → the
// normalized kind, so one call reads with the same colour language as its group.
export function toolStatusKind(status: ToolEventStatus | undefined, background = false): StatusKind {
  if (status === "running") return background ? "background" : "running";
  if (status === "failed") return "failed";
  if (status === "input_required") return "input_required";
  return "completed";
}

// An A2A task state (an agent step) → the normalized kind.
export function taskStateKind(state: string): StatusKind {
  switch (state) {
    case "completed":
      return "completed";
    case "failed":
    case "rejected":
      return "failed";
    case "canceled":
      return "canceled";
    case "input-required":
    case "auth-required":
      return "input_required";
    default:
      return "running"; // working / submitted
  }
}

// A task-list lifecycle value (the model's own task bookkeeping) → the normalized
// kind. Tolerant of spacing/casing/hyphenation the model may emit.
export function taskLifecycleKind(status: string): StatusKind {
  switch (status.toLowerCase().replace(/[\s-]+/g, "_")) {
    case "completed":
      return "completed";
    case "in_progress":
      return "running";
    case "blocked":
      return "blocked";
    case "cancelled":
    case "canceled":
      return "canceled";
    case "deleted":
      return "failed";
    case "pending":
    case "":
      return "pending";
    default:
      return "unknown";
  }
}
