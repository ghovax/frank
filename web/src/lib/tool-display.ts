import type { IconType } from "react-icons";
import {
  LuGlobe,
  LuTerminal,
  LuUsers,
  LuNetwork,
  LuPuzzle,
  LuWrench,
  LuLayoutDashboard,
  LuFileText,
  LuFolderSearch,
  LuSearchCode,
  LuFilePen,
  LuFilePlus,
  LuDownload,
  LuMessageCircleQuestion,
  LuSparkles,
  LuListChecks,
  LuTarget,
  LuMousePointerClick,
  LuCompass,
  LuCircleStop,
  LuUserSearch,
  LuMessageSquareShare,
  LuMessageSquareReply,
} from "react-icons/lu";

interface ToolDisplayInfo {
  icon: IconType;
  iconColor: string;
  label: string;
  // Whether `name` is one of our first-class tools. An unrecognized tool gets the
  // generic wrench icon and its raw name shown monospace (see `mono`).
  known: boolean;
  // Render the whole label as monospace code — true only for an unrecognized tool
  // shown by its bare name (no justification to describe it).
  mono: boolean;
  // Render the label as inline Markdown — true when it is the model's own
  // justification (which may carry code spans, `file:line` refs, emphasis). A
  // fallback label (a raw command or path) is plain text so it is never mangled.
  labelIsMarkdown: boolean;
}

// Every tool that has a first-class icon/label below. Anything else is "unknown"
// and surfaces its raw name in monospace.
const KNOWN_TOOL_NAMES: ReadonlySet<string> = new Set([
  "web_search", "bash", "spawn_agent", "cancel_agent", "ask_agent", "respond_agent", "read_task", "read_file", "find_files",
  "search_content", "edit_file", "write_file", "fetch_url", "ask_user", "load_skill",
  "set_tasks", "update_tasks", "update_goal", "computer", "browser",
  "work_habits",
  "open_artifact",
  "call_mcp_tool", "list_mcp_tools", "list_mcp_resources", "read_mcp_resource",
]);

function stripCdPrefix(command: string): string {
  const cdMatch = command.match(/^cd\s+'[^']*'\s+&&\s+(.*)/s);
  return cdMatch ? cdMatch[1] : command;
}

function iconForTool(name: string): { icon: IconType; iconColor: string } {
  switch (name) {
    case "web_search":
      return { icon: LuGlobe, iconColor: "blue.fg" };
    case "bash":
      return { icon: LuTerminal, iconColor: "green.fg" };
    case "spawn_agent":
      return { icon: LuUsers, iconColor: "purple.fg" };
    case "cancel_agent":
      return { icon: LuCircleStop, iconColor: "red.fg" };
    case "ask_agent":
      return { icon: LuMessageSquareShare, iconColor: "purple.fg" };
    case "respond_agent":
      return { icon: LuMessageSquareReply, iconColor: "blue.fg" };
    case "read_task":
      return { icon: LuNetwork, iconColor: "orange.fg" };
    case "read_file":
      return { icon: LuFileText, iconColor: "blue.fg" };
    case "find_files":
      return { icon: LuFolderSearch, iconColor: "cyan.fg" };
    case "search_content":
      return { icon: LuSearchCode, iconColor: "teal.fg" };
    case "edit_file":
      return { icon: LuFilePen, iconColor: "yellow.fg" };
    case "write_file":
      return { icon: LuFilePlus, iconColor: "green.fg" };
    case "fetch_url":
      return { icon: LuDownload, iconColor: "blue.fg" };
    case "ask_user":
      return { icon: LuMessageCircleQuestion, iconColor: "purple.fg" };
    case "load_skill":
      return { icon: LuSparkles, iconColor: "pink.fg" };
    case "set_tasks":
    case "update_tasks":
      return { icon: LuListChecks, iconColor: "blue.fg" };
    case "update_goal":
      return { icon: LuTarget, iconColor: "orange.fg" };
    case "open_artifact":
      return { icon: LuLayoutDashboard, iconColor: "pink.fg" };
    case "computer":
      return { icon: LuMousePointerClick, iconColor: "cyan.fg" };
    case "browser":
      return { icon: LuCompass, iconColor: "orange.fg" };
    case "work_habits":
      return { icon: LuUserSearch, iconColor: "blue.fg" };
    case "call_mcp_tool":
    case "list_mcp_tools":
    case "list_mcp_resources":
    case "read_mcp_resource":
      return { icon: LuPuzzle, iconColor: "purple.fg" };
    default:
      return { icon: LuWrench, iconColor: "fg.muted" };
  }
}

// A translator scoped to the "ToolDisplay" message namespace (from next-intl's useTranslations).
// Typed loosely here so the pure label helpers stay decoupled from the generated message types;
// callers pass their own `t`.
export type ToolDisplayTranslator = (key: string, values?: Record<string, string | number>) => string;

function fallbackLabel(name: string, args: Record<string, unknown> | undefined, t: ToolDisplayTranslator): string {
  switch (name) {
    case "web_search":
      return args?.query ? t("webSearch", { query: String(args.query) }) : t("webSearchBare");
    case "bash":
      return args?.command ? stripCdPrefix(String(args.command)) : t("bashBare");
    case "spawn_agent":
      return args?.agent ? t("spawnAgent", { agent: String(args.agent) }) : t("spawnAgentBare");
    case "cancel_agent":
      return t("cancelAgent");
    case "ask_agent":
      return t("askAgent");
    case "respond_agent":
      return t("respondAgent");
    case "read_task":
      return t("readTask");
    case "read_file":
      return args?.file_path ? readFileLabel(String(args.file_path), args, t) : t("readFileBare");
    case "find_files":
      return args?.pattern ? t("findFiles", { pattern: String(args.pattern) }) : t("findFilesBare");
    case "search_content":
      return args?.pattern ? t("searchContent", { pattern: String(args.pattern) }) : t("searchContentBare");
    case "edit_file":
      return args?.file_path ? t("editFile", { path: shortPath(String(args.file_path)) }) : t("editFileBare");
    case "write_file":
      return args?.file_path ? t("writeFile", { path: shortPath(String(args.file_path)) }) : t("writeFileBare");
    case "fetch_url":
      return args?.url ? t("fetchUrl", { url: String(args.url) }) : t("fetchUrlBare");
    case "ask_user":
      return t("askUser");
    case "load_skill":
      return args?.name ? t("loadSkill", { name: String(args.name) }) : t("loadSkillBare");
    case "set_tasks":
      return t("setTasks");
    case "update_tasks":
      return t("updateTasks");
    case "update_goal":
      return t("updateGoal");
    case "open_artifact":
      return args?.title ? t("openArtifact", { title: String(args.title) }) : t("openArtifactBare");
    case "computer":
      return computerLabel(args, t);
    case "browser":
      return browserLabel(args, t);
    case "work_habits":
      return t("workHabits");
    case "call_mcp_tool":
      return args?.tool_name ? t("callMcpTool", { tool: String(args.tool_name) }) : t("callMcpToolBare");
    case "list_mcp_tools":
      return t("listMcpTools");
    case "list_mcp_resources":
      return t("listMcpResources");
    case "read_mcp_resource":
      return args?.uri ? t("readMcpResource", { uri: String(args.uri) }) : t("readMcpResourceBare");
    default:
      return name;
  }
}

// read_file with an optional line range — a complete sentence per case, so the range is not an
// English-word-order suffix a translator would have to reassemble.
function readFileLabel(filePath: string, args: Record<string, unknown>, t: ToolDisplayTranslator): string {
  const file = fileName(filePath);
  const offset = Number(args.offset ?? 1);
  const limit = args.limit == null ? 0 : Number(args.limit);
  const defaultLimit = 2000;
  const hasSpecificOffset = Number.isFinite(offset) && offset > 1;
  const hasSpecificLimit = Number.isFinite(limit) && limit > 0 && limit !== defaultLimit;
  if (!hasSpecificOffset && !hasSpecificLimit) return t("readFile", { file });
  if (!Number.isFinite(limit) || limit <= 0) return t("readFileFromLine", { file, line: offset });
  return t("readFileLines", { file, start: offset, end: offset + limit - 1 });
}

// A computer-control call with no justification: describe it from its action + target.
function computerLabel(args: Record<string, unknown> | undefined, t: ToolDisplayTranslator): string {
  const action = args?.action ? String(args.action) : "";
  const app = args?.app ? String(args.app) : "";
  const verbActions = new Set(["observe", "find", "click", "type", "press", "menu", "scroll", "screenshot"]);
  if (!verbActions.has(action)) return app ? t("computerControlling", { app }) : t("computerControllingMac");
  return app ? t(`computer_${action}_app`, { app }) : t(`computer_${action}`);
}

// A browser call with no justification: describe it from its action and target.
function browserLabel(args: Record<string, unknown> | undefined, t: ToolDisplayTranslator): string {
  const action = args?.action ? String(args.action) : "";
  switch (action) {
    case "navigate":
      return args?.url ? t("browserNavigate", { url: String(args.url) }) : t("browserNavigateBare");
    case "observe":
      return t("browserObserve");
    case "find":
      return t("browserFind");
    case "click":
      return t("browserClick");
    case "type":
      return t("browserType");
    case "read":
      return t("browserRead");
    case "evaluate":
      return t("browserEvaluate");
    case "network":
      return t("browserNetwork");
    case "press":
      return args?.key ? t("browserPress", { key: String(args.key) }) : t("browserPressBare");
    case "hover":
      return t("browserHover");
    case "scroll":
      return t("browserScroll");
    case "back":
      return t("browserBack");
    case "forward":
      return t("browserForward");
    case "reload":
      return t("browserReload");
    case "tabs":
      return t("browserTabs");
    case "new_tab":
      return args?.url ? t("browserNewTab", { url: String(args.url) }) : t("browserNewTabBare");
    case "switch_tab":
      return t("browserSwitchTab");
    case "close_tab":
      return t("browserCloseTab");
    default:
      return t("browserDefault");
  }
}

function shortPath(path: string): string {
  const parts = path.split("/");
  return parts.length > 2 ? `…/${parts.slice(-2).join("/")}` : path;
}

function fileName(path: string): string {
  return path.split("/").filter(Boolean).at(-1) ?? path;
}

export function getToolCallDisplay(
  name: string,
  args: Record<string, unknown> | undefined,
  t: ToolDisplayTranslator,
): ToolDisplayInfo {
  const known = KNOWN_TOOL_NAMES.has(name);
  const justification = args?.justification ? String(args.justification) : "";
  return {
    ...iconForTool(name),
    label: justification || fallbackLabel(name, args, t),
    known,
    // A bare, unrecognized tool name reads as code (it *is* an identifier).
    mono: !known && !justification,
    // Only the model's justification is trusted as Markdown; fallbacks stay literal.
    labelIsMarkdown: !!justification,
  };
}
