/** What a tool call is called, and which glyph stands for it, shared by every client. */

/** The glyph vocabulary. One concept, one name, and no name meaning two things. */
export type GlyphName =
  | "globe"
  | "terminal"
  | "mouse-pointer-click"
  | "download"
  | "message-circle-question"
  | "target"
  | "user-search"
  | "sparkles"
  | "plug"
  | "list-checks"
  | "server"
  | "wrench"
  | "users"
  | "radio-tower"
  | "clock"
  | "history"
  // One tool, one glyph, so two different calls are never the same picture in a list.
  | "user-plus"
  | "send"
  | "list"
  | "plug-zap"
  | "boxes"
  | "book-open"
  | "satellite-dish"
  | "square-check"
  | "hard-drive-download"
  // The settings controls pick from the same vocabulary, so a concept cannot wear two glyphs.
  | "hand"
  | "badge-check"
  | "box"
  | "folder"
  | "git-branch"
  | "copy"
  | "zap"
  | "circle-slash"
  | "user-round-x"
  | "mic"
  | "mic-off"
  // The status chips draw from it too, for the same reason.
  | "circle-alert"
  | "circle-x"
  | "moon";

/** What a call claims about changing things, which is three answers and not two. */
export type MutationClaim = "reads" | "writes" | "undeclared";

/** Calls that cannot change anything, whatever they say, because of what they are. */
const NEVER_MUTATES: ReadonlySet<string> = new Set([
  "search_web",
  "fetch_url",
  "read_turn",
  "wait_for",
  "list_mcp_tools",
  "list_mcp_resources",
  "read_mcp_resource",
  // The peer-session calls, which change what is happening but not what this machine holds.
  "create_session",
  "message_session",
  "read_session",
  "list_sessions",
  "list_remote_agents",
  "message_remote_agent",
  // The agent's own bookkeeping: its task list and its goal live in the session record.
  "set_tasks",
  "update_tasks",
  "update_goal",
  "load_skill",
  "ask_user",
  "work_habits",
]);

const ALWAYS_MUTATES: ReadonlySet<string> = new Set(["download_file"]);

/** Missing mutation declarations are unknown rather than writes. */
export function mutationClaim(
  name: string,
  args: Record<string, unknown> | undefined,
): MutationClaim {
  if (NEVER_MUTATES.has(name)) return "reads";
  if (ALWAYS_MUTATES.has(name)) return "writes";
  const request = args?.access_request;
  if (!request || typeof request !== "object") return "undeclared";
  const mutates = (request as Record<string, unknown>).mutates;
  if (mutates === false) return "reads";
  if (mutates === true) return "writes";
  return "undeclared";
}

export interface RequestedAccess {
  reads: string[];
  writes: string[];
  network: boolean;
  /** Whether the call asked for anything at all, since a bare `mutates` is a claim rather than a request. */
  any: boolean;
}

/** What reach a call asked for beyond its sandbox. Always a value, so a caller reads its fields without a guard. */
export function requestedAccess(
  args: Record<string, unknown> | undefined,
): RequestedAccess {
  const request = args?.access_request;
  const empty: RequestedAccess = {
    reads: [],
    writes: [],
    network: false,
    any: false,
  };
  if (!request || typeof request !== "object") return empty;
  const record = request as Record<string, unknown>;
  const paths = (value: unknown): string[] =>
    Array.isArray(value)
      ? value.filter((entry): entry is string => typeof entry === "string")
      : [];
  const reads = paths(record.reads);
  const writes = paths(record.writes);
  const network = record.network === true;
  return {
    reads,
    writes,
    network,
    any: reads.length > 0 || writes.length > 0 || network,
  };
}

export interface ToolDisplay {
  glyph: GlyphName;
  tint: string;
  /** The model's own explanation, which every tool requires, and empty until it arrives. */
  label: string;
}

/** One glyph per tool, and never two tools wearing the same one. */
const TOOL_GLYPHS: Record<string, { glyph: GlyphName; tint: string }> = {
  search_web: { glyph: "globe", tint: "blue.fg" },
  bash: { glyph: "terminal", tint: "green.fg" },
  control_screen: { glyph: "mouse-pointer-click", tint: "cyan.fg" },
  fetch_url: { glyph: "download", tint: "blue.fg" },
  download_file: { glyph: "hard-drive-download", tint: "blue.fg" },
  ask_user: { glyph: "message-circle-question", tint: "purple.fg" },
  load_skill: { glyph: "sparkles", tint: "pink.fg" },
  set_tasks: { glyph: "list-checks", tint: "blue.fg" },
  update_tasks: { glyph: "square-check", tint: "blue.fg" },
  update_goal: { glyph: "target", tint: "orange.fg" },
  // A wait is the one call doing nothing on purpose, so it reads that way in the muted tint.
  wait_for: { glyph: "clock", tint: "fg.muted" },
  read_turn: { glyph: "history", tint: "blue.fg" },
  work_habits: { glyph: "user-search", tint: "blue.fg" },
  call_mcp_tool: { glyph: "plug", tint: "purple.fg" },
  list_mcp_tools: { glyph: "plug-zap", tint: "purple.fg" },
  list_mcp_resources: { glyph: "boxes", tint: "purple.fg" },
  read_mcp_resource: { glyph: "book-open", tint: "purple.fg" },
  create_session: { glyph: "user-plus", tint: "orange.fg" },
  message_session: { glyph: "send", tint: "orange.fg" },
  read_session: { glyph: "users", tint: "orange.fg" },
  list_sessions: { glyph: "list", tint: "orange.fg" },
  list_remote_agents: { glyph: "radio-tower", tint: "teal.fg" },
  message_remote_agent: { glyph: "satellite-dish", tint: "teal.fg" },
};

function assertDistinctGlyphs(): void {
  const seen = new Map<GlyphName, string>();
  for (const [tool, { glyph }] of Object.entries(TOOL_GLYPHS)) {
    const taken = seen.get(glyph);
    if (taken) {
      throw new Error(
        `Two tools share the glyph "${glyph}": ${taken} and ${tool}. One tool, one glyph.`,
      );
    }
    seen.set(glyph, tool);
  }
  if (seen.has("wrench")) {
    throw new Error(
      '"wrench" stands for a tool with no glyph of its own; a real tool may not wear it.',
    );
  }
}

assertDistinctGlyphs();

/** How one tool call presents itself: its glyph, and the explanation the model was required to give. */
export function toolCallDisplay(
  name: string,
  args: Record<string, unknown> | undefined,
  ready = false,
): ToolDisplay {
  // The backend marks all streamed arguments complete before the explanation becomes visible.
  return {
    ...(TOOL_GLYPHS[name] ?? { glyph: "wrench", tint: "fg.muted" }),
    label: ready && args?.explanation ? String(args.explanation) : "",
  };
}
