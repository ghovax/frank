/**
 * What a turn's state is called, and in which colour.
 *
 * Lifted from `web/src/lib/status.ts`, minus its icons — those are `react-icons` components on
 * the desktop and `lucide-react-native` ones on the phone, so the *choice* of glyph is shared
 * below as a name and the component each client resolves for itself.
 *
 * The palette entries are colour *names*, not values. Each client already has a table turning
 * `blue` into whatever blue means on its surface; what must not differ is which states are blue.
 */

/**
 * The normalised lifecycle status every surface that shows one maps into — a tool call, a
 * grouped run of calls, an agent step, a task row. Each source enum lands here so the colour of
 * "running" / "failed" / "input required" / "background" is decided in exactly one place.
 */
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
export const STATUS_GLYPH: Partial<Record<StatusKind, string>> = {
  input_required: "circle-alert",
  failed: "circle-x",
  background: "moon",
};

/** A live tool call's status — and whether it was pushed to the background — as a kind. */
export function toolStatusKind(status: string | undefined, background = false): StatusKind {
  if (status === "running") return background ? "background" : "running";
  if (status === "failed" || status === "error") return "failed";
  if (status === "input_required") return "input_required";
  return "completed";
}

/** An A2A task state as a kind. */
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
    case "input_required":
      return "input_required";
    case "working":
    case "submitted":
      return "running";
    default:
      return "unknown";
  }
}
