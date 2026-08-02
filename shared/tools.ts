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
  | "users" | "radio-tower"
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
  "work_habits",
  "call_mcp_tool", "list_mcp_tools", "list_mcp_resources", "read_mcp_resource",
  "create_session", "message_session", "read_session", "list_sessions", "end_session",
  "list_remote_agents", "message_remote_agent",
]);

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

function glyphForTool(name: string): { glyph: GlyphName; tint: string } {
  switch (name) {
    case "search_web": return { glyph: "globe", tint: "blue.fg" };
    case "bash": return { glyph: "terminal", tint: "green.fg" };
    case "read_file": return { glyph: "file-text", tint: "blue.fg" };
    case "search_code": return { glyph: "search-code", tint: "teal.fg" };
    case "control_screen": return { glyph: "mouse-pointer-click", tint: "cyan.fg" };
    case "edit_file": return { glyph: "file-pen", tint: "yellow.fg" };
    case "write_file": return { glyph: "file-plus", tint: "green.fg" };
    case "fetch_url": return { glyph: "download", tint: "blue.fg" };
    case "ask_user": return { glyph: "message-circle-question", tint: "purple.fg" };
    case "load_skill": return { glyph: CONCEPT_GLYPHS.skill, tint: CONCEPT_TINTS.skill };
    case "set_tasks":
    case "update_tasks": return { glyph: CONCEPT_GLYPHS.tasks, tint: CONCEPT_TINTS.tasks };
    case "update_goal": return { glyph: "target", tint: "orange.fg" };
    case "work_habits": return { glyph: "user-search", tint: "blue.fg" };
    case "call_mcp_tool":
    case "list_mcp_tools":
    case "list_mcp_resources":
    case "read_mcp_resource": return { glyph: CONCEPT_GLYPHS.mcp, tint: CONCEPT_TINTS.mcp };
    case "create_session":
    case "message_session":
    case "read_session":
    case "list_sessions":
    case "end_session": return { glyph: CONCEPT_GLYPHS.peer, tint: CONCEPT_TINTS.peer };
    case "list_remote_agents":
    case "message_remote_agent": return { glyph: CONCEPT_GLYPHS.remoteAgent, tint: CONCEPT_TINTS.remoteAgent };
    default: return { glyph: CONCEPT_GLYPHS.unknownTool, tint: CONCEPT_TINTS.unknownTool };
  }
}

/** The agent's shell wrapper: `cd '…' && real command`. Only the real command is worth showing. */
function stripCdPrefix(command: string): string {
  const cdMatch = command.match(/^cd\s+'[^']*'\s+&&\s+([\s\S]*)/);
  return cdMatch ? cdMatch[1] : command;
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
      return args?.command ? stripCdPrefix(String(args.command)) : translation("bashBare");
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
    case "end_session":
      return translation("endSession");
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
