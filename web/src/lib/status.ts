import type { IconType } from "react-icons";

import { STATUS_GLYPH, STATUS_PALETTE, toolStatusKind, type StatusKind } from "@shared/status";

import { glyph } from "./glyphs";

export { STATUS_PALETTE, toolStatusKind };
export type { StatusKind };

// The glyph for statuses that render as icon chips. What a glyph is belongs to this client.
export const STATUS_ICON: Partial<Record<StatusKind, IconType>> = Object.fromEntries(
  Object.entries(STATUS_GLYPH).map(([kind, name]) => [kind, glyph(name)]),
) as Partial<Record<StatusKind, IconType>>;

// A task lifecycle value as the normalized kind, tolerant of the spacing and casing a model may emit.
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
