import type { GlyphName } from "./tools";

/** What a turn's state is called and in which colour, shared by every surface that shows one. */

/** The normalised lifecycle status every source enum maps into, so a colour is decided once. */
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

/** The glyph for the statuses that render as icon chips. Prose surfaces show a label instead. */
export const STATUS_GLYPH: Partial<Record<StatusKind, GlyphName>> = {
  input_required: "circle-alert",
  failed: "circle-x",
  background: "moon",
};

/** A live tool call's status — and whether it was pushed to the background — as a kind. */
export function toolStatusKind(
  status: string | undefined,
  background = false,
): StatusKind {
  if (status === "running") return background ? "background" : "running";
  if (status === "failed" || status === "error") return "failed";
  if (status === "input_required") return "input_required";
  return "completed";
}
