/**
 * What a tool call is called, and which glyph stands for it.
 *
 * Lifted wholesale from `web/src/lib/tool-display.ts`. It had been written twice — once there,
 * once on the phone — and the second copy was already drifting: different wording for the same
 * call, and a different glyph for `search_code`.
 *
 * The glyph is a **name**, not a component. The desktop draws with `react-icons/lu` and the phone
 * with `lucide-react-native`, which are different packages exporting different objects, so a
 * shared component is not possible. A shared *decision* is: this file says `bash` is a terminal,
 * and each client has a small table turning `"terminal"` into something it can draw.
 *
 * The tint is likewise a token name (`green.fg`), which both clients already resolve.
 */

import { labels } from "./labels";

/** The glyph vocabulary. One concept, one name, and no name meaning two things. */
export type GlyphName =
  | "globe" | "terminal" | "file-text" | "search-code" | "mouse-pointer-click"
  | "file-pen" | "file-plus" | "download" | "message-circle-question" | "target"
  | "user-search" | "sparkles" | "plug" | "list-checks" | "server" | "wrench"
  | "users" | "radio-tower" | "clock" | "history"
  // One tool, one glyph: the families below used to share theirs, so four different peer calls
  // and four different MCP calls were the same picture in a list.
  | "user-plus" | "send" | "list" | "plug-zap" | "boxes" | "book-open"
  | "satellite-dish" | "square-check" | "hard-drive-download"
  // The settings controls, which pick from the same vocabulary so a concept cannot wear one
  // glyph in the transcript and another in a menu.
  | "hand" | "badge-check" | "eye" | "box" | "folder" | "git-branch" | "copy" | "zap"
  | "circle-slash" | "user-round-x" | "mic" | "mic-off";

/**
 * One icon per concept, for the whole interface. Kept together with the colours so a concept
 * cannot pick up a new glyph and keep an old colour.
 */
export const CONCEPT_GLYPHS = {
  /** A skill: something the agent knows how to do. Also `load_skill`, which loads one. */
  skill: "sparkles",
  /** MCP — a configured server, its tools, and every call into one. */
  mcp: "plug",
  /** The agent's own task list (`set_tasks` / `update_tasks`), and nothing else. */
  tasks: "list-checks",
  /** A peer session: one this session created, or the conversation of one it can see. */
  peer: "users",
  /** An agent registered on another host — not a peer, and not on this filesystem. */
  remoteAgent: "radio-tower",
  /** A place this workspace can work in — a folder here, or one on an SSH host. */
  environment: "server",
  /** A tool the interface does not recognise. Only ever the fallback. */
  unknownTool: "wrench",
} satisfies Record<string, GlyphName>;

export const CONCEPT_TINTS = {
  skill: "pink.fg",
  mcp: "purple.fg",
  tasks: "blue.fg",
  peer: "orange.fg",
  remoteAgent: "teal.fg",
  environment: "fg.muted",
  unknownTool: "fg.muted",
} satisfies Record<keyof typeof CONCEPT_GLYPHS, string>;

/** Every tool with a first-class glyph and label. Anything else surfaces its raw name. */
const KNOWN_TOOL_NAMES: ReadonlySet<string> = new Set([
  "search_web", "bash", "read_file",
  "search_code", "control_screen",
  "edit_file", "write_file", "fetch_url", "ask_user", "load_skill",
  "set_tasks", "update_tasks", "update_goal",
  "wait_for", "read_turn", "download_file",
  "work_habits",
  "call_mcp_tool", "list_mcp_tools", "list_mcp_resources", "read_mcp_resource",
  "create_session", "message_session", "read_session", "list_sessions",
  "list_remote_agents", "message_remote_agent",
]);


/**
 * Whether a call can change anything, for the "write" marker a person reads before approving.
 *
 * The marker used to appear whenever a call had not *declared* `mutates: false` — which is every
 * call, because most tools have nothing to declare. `read_file` was badged a write, and so was
 * `list_sessions`, and a badge that appears on everything says nothing about anything: the one
 * call that really did rewrite a file looked exactly like the one that read a directory listing.
 *
 * So the tool answers first, and the declaration only decides the cases where the tool genuinely
 * could go either way. `bash` is the reason this is not simply a list: it runs whatever it is
 * given, so an undeclared `bash` is treated as mutating — which is also how the harness itself
 * treats it when it decides whether to ask.
 */
const NEVER_MUTATES: ReadonlySet<string> = new Set([
  "read_file", "search_code", "search_web", "fetch_url", "read_turn", "wait_for",
  "list_mcp_tools", "list_mcp_resources", "read_mcp_resource",
  // The peer-session calls. Creating a session and briefing it changes what is *happening* —
  // but this badge is read by a person deciding whether to approve a call, and to them "write"
  // means something on disk changes. A peer created read-only, badged as a write, taught them
  // to ignore the badge on the calls where it means a file.
  "create_session", "message_session", "read_session", "list_sessions",
  "list_remote_agents", "message_remote_agent",
  // The agent's own bookkeeping: its task list and its goal live in the session record.
  "set_tasks", "update_tasks", "update_goal",
  "load_skill", "ask_user", "work_habits",
]);

const ALWAYS_MUTATES: ReadonlySet<string> = new Set([
  "edit_file", "write_file", "download_file",
]);

export function callMayMutate(name: string, args: Record<string, unknown> | undefined): boolean {
  if (NEVER_MUTATES.has(name)) return false;
  if (ALWAYS_MUTATES.has(name)) return true;
  const request = args?.access_request;
  if (request && typeof request === "object") {
    return (request as Record<string, unknown>).mutates !== false;
  }
  return true;
}

export interface ToolDisplay {
  glyph: GlyphName;
  tint: string;
  label: string;
  /** Whether `name` is one of the first-class tools above. */
  known: boolean;
  /** Render the label as monospace — true only for an unrecognised tool shown by its bare name. */
  mono: boolean;
  /**
   * Render the label as inline Markdown — true when it is the model's own explanation, which may
   * carry code spans, `file:line` refs and emphasis. A derived label (a raw command or path) is
   * plain text so it is never mangled.
   */
  labelIsMarkdown: boolean;
}

export type Translate = (key: string, values?: Record<string, string | number>) => string;

/**
 * One glyph per tool, and never two tools wearing the same one.
 *
 * This was a `switch` with fall-through cases, which is how the sharing crept in: four peer
 * calls returned the concept glyph for "peer", four MCP calls returned the one for "MCP", and a
 * transcript of ten calls showed three pictures. A glyph is how a person finds a call while
 * scanning, so two calls sharing one is the same as neither having one.
 *
 * As a table rather than a switch because a table can be *checked*: `assertDistinctGlyphs`
 * below runs at import and throws if any glyph is used twice. A future tool that reaches for a
 * taken picture fails the build rather than quietly blending into the list.
 */
const TOOL_GLYPHS: Record<string, { glyph: GlyphName; tint: string }> = {
  search_web: { glyph: "globe", tint: "blue.fg" },
  bash: { glyph: "terminal", tint: "green.fg" },
  read_file: { glyph: "file-text", tint: "blue.fg" },
  search_code: { glyph: "search-code", tint: "teal.fg" },
  control_screen: { glyph: "mouse-pointer-click", tint: "cyan.fg" },
  edit_file: { glyph: "file-pen", tint: "yellow.fg" },
  write_file: { glyph: "file-plus", tint: "green.fg" },
  fetch_url: { glyph: "download", tint: "blue.fg" },
  download_file: { glyph: "hard-drive-download", tint: "blue.fg" },
  ask_user: { glyph: "message-circle-question", tint: "purple.fg" },
  load_skill: { glyph: CONCEPT_GLYPHS.skill, tint: CONCEPT_TINTS.skill },
  set_tasks: { glyph: CONCEPT_GLYPHS.tasks, tint: CONCEPT_TINTS.tasks },
  update_tasks: { glyph: "square-check", tint: CONCEPT_TINTS.tasks },
  update_goal: { glyph: "target", tint: "orange.fg" },
  // A wait is the one call that is doing nothing on purpose, and it reads that way: a clock,
  // in the muted tint the interface uses for what is quiet rather than notable.
  wait_for: { glyph: "clock", tint: "fg.muted" },
  read_turn: { glyph: "history", tint: "blue.fg" },
  work_habits: { glyph: "user-search", tint: "blue.fg" },
  call_mcp_tool: { glyph: CONCEPT_GLYPHS.mcp, tint: CONCEPT_TINTS.mcp },
  list_mcp_tools: { glyph: "plug-zap", tint: CONCEPT_TINTS.mcp },
  list_mcp_resources: { glyph: "boxes", tint: CONCEPT_TINTS.mcp },
  read_mcp_resource: { glyph: "book-open", tint: CONCEPT_TINTS.mcp },
  create_session: { glyph: "user-plus", tint: CONCEPT_TINTS.peer },
  message_session: { glyph: "send", tint: CONCEPT_TINTS.peer },
  read_session: { glyph: CONCEPT_GLYPHS.peer, tint: CONCEPT_TINTS.peer },
  list_sessions: { glyph: "list", tint: CONCEPT_TINTS.peer },
  list_remote_agents: { glyph: CONCEPT_GLYPHS.remoteAgent, tint: CONCEPT_TINTS.remoteAgent },
  message_remote_agent: { glyph: "satellite-dish", tint: CONCEPT_TINTS.remoteAgent },
};

function assertDistinctGlyphs(): void {
  const seen = new Map<GlyphName, string>();
  for (const [tool, { glyph }] of Object.entries(TOOL_GLYPHS)) {
    const taken = seen.get(glyph);
    if (taken) {
      throw new Error(`Two tools share the glyph "${glyph}": ${taken} and ${tool}. One tool, one glyph.`);
    }
    seen.set(glyph, tool);
  }
  if (seen.has(CONCEPT_GLYPHS.unknownTool)) {
    throw new Error(`"${CONCEPT_GLYPHS.unknownTool}" is the fallback for a tool nobody recognises; a real tool may not wear it.`);
  }
}

assertDistinctGlyphs();

function glyphForTool(name: string): { glyph: GlyphName; tint: string } {
  return TOOL_GLYPHS[name] ?? { glyph: CONCEPT_GLYPHS.unknownTool, tint: CONCEPT_TINTS.unknownTool };
}

function shortPath(path: string): string {
  const parts = path.split("/");
  return parts.length > 2 ? `…/${parts.slice(-2).join("/")}` : path;
}

function fileName(path: string): string {
  return path.split("/").filter(Boolean).at(-1) ?? path;
}

/**
 * `read_file` with an optional line range — a complete sentence per case, so the range is not an
 * English-word-order suffix a translator would have to reassemble.
 */
function readFileLabel(filePath: string, args: Record<string, unknown>, translation: Translate): string {
  const file = fileName(filePath);
  const offset = Number(args.offset ?? 1);
  const limit = args.limit == null ? 0 : Number(args.limit);
  const defaultLimit = 2000;
  const hasSpecificOffset = Number.isFinite(offset) && offset > 1;
  const hasSpecificLimit = Number.isFinite(limit) && limit > 0 && limit !== defaultLimit;
  if (!hasSpecificOffset && !hasSpecificLimit) return translation("readFile", { file });
  if (!Number.isFinite(limit) || limit <= 0) return translation("readFileFromLine", { file, line: offset });
  return translation("readFileLines", { file, start: offset, end: offset + limit - 1 });
}

/**
 * A `control_screen` call with no explanation: describe it from its surface plus the script's
 * first line — the whole script is body content, not a one-line label.
 */
function controlScreenLabel(args: Record<string, unknown> | undefined, translation: Translate): string {
  const surface = args?.surface ? String(args.surface) : "";
  const firstLine = args?.script
    ? String(args.script).split("\n").map((line) => line.trim()).find(Boolean) ?? ""
    : "";
  if (firstLine) return translation("controlScreenScript", { script: firstLine });
  return surface ? translation("controlScreenSurface", { surface }) : translation("controlScreenBare");
}

function fallbackLabel(name: string, args: Record<string, unknown> | undefined, translation: Translate): string {
  switch (name) {
    case "search_web":
      return args?.query ? translation("webSearch", { query: String(args.query) }) : translation("webSearchBare");
    case "bash":
      // Whole, not edited. A leading `cd '…' && ` used to be the harness's own wrapper and was
      // cut out here; the harness now sets the process's working directory instead, so any `cd`
      // in a command is the model's, and removing it from the label hid where the rest of the
      // command would run — on the very line a person reads before approving it.
      return args?.command ? String(args.command) : translation("bashBare");
    case "read_file":
      return args?.file_path ? readFileLabel(String(args.file_path), args, translation) : translation("readFileBare");
    case "search_code":
      return args?.query ? translation("searchCode", { query: String(args.query) }) : translation("searchCodeBare");
    case "control_screen":
      return controlScreenLabel(args, translation);
    case "edit_file":
      return args?.file_path ? translation("editFile", { path: shortPath(String(args.file_path)) }) : translation("editFileBare");
    case "write_file":
      return args?.file_path ? translation("writeFile", { path: shortPath(String(args.file_path)) }) : translation("writeFileBare");
    case "fetch_url":
      return args?.url ? translation("fetchUrl", { url: String(args.url) }) : translation("fetchUrlBare");
    case "ask_user":
      return translation("askUser");
    case "load_skill":
      return args?.name ? translation("loadSkill", { name: String(args.name) }) : translation("loadSkillBare");
    case "set_tasks":
      return translation("setTasks");
    case "update_tasks":
      return translation("updateTasks");
    case "update_goal":
      return translation("updateGoal");
    case "wait_for": {
      const seconds = Number(args?.seconds);
      return Number.isFinite(seconds) && seconds > 0
        ? translation("waitFor", { seconds })
        : translation("waitForBare");
    }
    case "read_turn":
      return translation("readTurn");
    case "download_file":
      return args?.url ? translation("downloadFile", { url: String(args.url) }) : translation("downloadFileBare");
    case "work_habits":
      return translation("workHabits");
    case "call_mcp_tool":
      return args?.tool_name ? translation("callMcpTool", { tool: String(args.tool_name) }) : translation("callMcpToolBare");
    case "list_mcp_tools":
      return translation("listMcpTools");
    case "list_mcp_resources":
      return translation("listMcpResources");
    case "read_mcp_resource":
      return args?.uri ? translation("readMcpResource", { uri: String(args.uri) }) : translation("readMcpResourceBare");
    case "create_session":
      return args?.agent ? translation("createSession", { agent: String(args.agent) }) : translation("createSessionBare");
    case "message_session":
      return translation("messageSession");
    case "read_session":
      return translation("readSession");
    case "list_sessions":
      return translation("listSessions");
    case "list_remote_agents":
      return translation("listRemoteAgents");
    case "message_remote_agent":
      return args?.name ? translation("messageRemoteAgent", { name: String(args.name) }) : translation("messageRemoteAgentBare");
    default:
      return name;
  }
}

/**
 * How one tool call presents itself.
 *
 * `translate` is the desktop's `next-intl` reader, so it keeps its locale and its plural rules.
 * Omitted — which is what the phone does, having no i18n framework — it falls back to the shared
 * catalogue's English, which is the same catalogue the desktop's `en` comes from.
 */
export function toolCallDisplay(
  name: string,
  args: Record<string, unknown> | undefined,
  translate?: Translate,
): ToolDisplay {
  const translation = translate ?? labels("ToolDisplay");
  const known = KNOWN_TOOL_NAMES.has(name);
  const explanation = args?.explanation ? String(args.explanation) : "";
  return {
    ...glyphForTool(name),
    label: explanation || fallbackLabel(name, args, translation),
    known,
    // A bare, unrecognised tool name reads as code, because it *is* an identifier.
    mono: !known && !explanation,
    // Only the model's explanation is trusted as Markdown; fallbacks stay literal.
    labelIsMarkdown: !!explanation,
  };
}
