import type { IconType } from "react-icons";

import { STATUS_GLYPH, STATUS_PALETTE, type StatusKind } from "@shared/status";

import { glyph } from "./glyphs";
import type { ToolEventStatus } from "@/lib/tool-event";

export { STATUS_PALETTE };
export type { StatusKind };

// The glyph for statuses that render as icon chips (the tool-group recap).
export const STATUS_ICON: Partial<Record<StatusKind, IconType>> = Object.fromEntries(
  Object.entries(STATUS_GLYPH).map(([kind, name]) => [kind, glyph(name)]),
) as Partial<Record<StatusKind, IconType>>;

// A live tool call's status (plus whether it was pushed to the background), mapped to the normalized kind, so one call reads with the same colour language as its group.
export function toolStatusKind(status: ToolEventStatus | undefined, background = false): StatusKind {
  if (status === "running") return background ? "background" : "running";
  if (status === "failed") return "failed";
  if (status === "input_required") return "input_required";
  return "completed";
}

// An A2A task state (an agent step), mapped to the normalized kind.
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

// A task-list lifecycle value (the model's own task bookkeeping), mapped to the normalized kind.
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
