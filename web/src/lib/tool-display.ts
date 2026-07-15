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

function fallbackLabel(name: string, args?: Record<string, unknown>): string {
  switch (name) {
    case "web_search":
      return args?.query ? `Browsing for "${String(args.query)}"` : "Browsing the web";
    case "bash":
      return args?.command ? stripCdPrefix(String(args.command)) : "Running command";
    case "spawn_agent":
      return args?.agent ? `Delegating to "${String(args.agent)}" agent` : "Delegating to agent";
    case "cancel_agent":
      return "Canceling spawned agent";
    case "ask_agent":
      return "Asking an agent";
    case "respond_agent":
      return "Responding to an agent";
    case "read_task":
      return "Reading a related task";
    case "read_file":
      return args?.file_path ? `Reading the file ${fileName(String(args.file_path))}${readFileRange(args)}` : "Reading file";
    case "find_files":
      return args?.pattern ? `Finding files matching "${String(args.pattern)}"` : "Finding files";
    case "search_content":
      return args?.pattern ? `Searching for "${String(args.pattern)}"` : "Searching content";
    case "edit_file":
      return args?.file_path ? `Editing ${shortPath(String(args.file_path))}` : "Editing file";
    case "write_file":
      return args?.file_path ? `Writing ${shortPath(String(args.file_path))}` : "Writing file";
    case "fetch_url":
      return args?.url ? `Fetching ${String(args.url)}` : "Fetching URL";
    case "ask_user":
      return "Asking the user";
    case "load_skill":
      return args?.name ? `Loading "${String(args.name)}" skill` : "Loading skill";
    case "set_tasks":
      return "Setting task list";
    case "update_tasks":
      return "Updating task list";
    case "update_goal":
      return "Updating goal";
    case "open_artifact":
      return args?.title ? `Opening "${String(args.title)}"` : "Opening an artifact";
    case "computer":
      return computerLabel(args);
    case "browser":
      return browserLabel(args);
    case "work_habits":
      return "Loading your work habits…";
    case "call_mcp_tool":
      return args?.tool_name ? `Calling MCP tool "${String(args.tool_name)}"` : "Calling MCP tool";
    case "list_mcp_tools":
      return "Listing MCP tools";
    case "list_mcp_resources":
      return "Listing MCP resources";
    case "read_mcp_resource":
      return args?.uri ? `Reading MCP resource "${String(args.uri)}"` : "Reading MCP resource";
    default:
      return name;
  }
}

// A computer-control call with no justification: describe it from its action + target.
// The verb map keeps the wording consistent with the tool's own action names.
function computerLabel(args?: Record<string, unknown>): string {
  const action = args?.action ? String(args.action) : "";
  const app = args?.app ? String(args.app) : "";
  const verbs: Record<string, string> = {
    observe: "Looking at",
    find: "Searching in",
    click: "Clicking in",
    type: "Typing in",
    press: "Pressing a key in",
    menu: "Choosing a menu in",
    scroll: "Scrolling",
    screenshot: "Capturing",
  };
  const verb = verbs[action];
  if (!verb) return app ? `Controlling ${app}` : "Controlling this Mac";
  return app ? `${verb} ${app}` : verb;
}

// A browser call with no justification: describe it from its action and target.
function browserLabel(args?: Record<string, unknown>): string {
  const action = args?.action ? String(args.action) : "";
  switch (action) {
    case "navigate":
      return args?.url ? `Opening ${String(args.url)}` : "Opening a page";
    case "observe":
      return "Reading the page";
    case "find":
      return "Searching the page";
    case "click":
      return "Clicking in the page";
    case "type":
      return "Typing in the page";
    case "read":
      return "Reading the page text";
    case "evaluate":
      return "Running JavaScript";
    case "network":
      return "Reading network activity";
    case "press":
      return args?.key ? `Pressing ${String(args.key)}` : "Pressing a key";
    case "hover":
      return "Hovering in the page";
    case "scroll":
      return "Scrolling the page";
    case "back":
      return "Going back";
    case "forward":
      return "Going forward";
    case "reload":
      return "Reloading the page";
    case "tabs":
      return "Listing tabs";
    case "new_tab":
      return args?.url ? `Opening ${String(args.url)} in a new tab` : "Opening a new tab";
    case "switch_tab":
      return "Switching tab";
    case "close_tab":
      return "Closing a tab";
    default:
      return "Using the browser";
  }
}

function shortPath(path: string): string {
  const parts = path.split("/");
  return parts.length > 2 ? `…/${parts.slice(-2).join("/")}` : path;
}

function fileName(path: string): string {
  return path.split("/").filter(Boolean).at(-1) ?? path;
}

function readFileRange(args: Record<string, unknown>): string {
  const offset = Number(args.offset ?? 1);
  const limit = args.limit == null ? 0 : Number(args.limit);
  const defaultLimit = 2000;
  const hasSpecificOffset = Number.isFinite(offset) && offset > 1;
  const hasSpecificLimit = Number.isFinite(limit) && limit > 0 && limit !== defaultLimit;
  if (!hasSpecificOffset && !hasSpecificLimit) return "";
  if (!Number.isFinite(limit) || limit <= 0) return ` around line ${offset}`;
  return ` around lines ${offset}–${offset + limit - 1}`;
}

export function getToolCallDisplay(
  name: string,
  args?: Record<string, unknown>
): ToolDisplayInfo {
  const known = KNOWN_TOOL_NAMES.has(name);
  const justification = args?.justification ? String(args.justification) : "";
  return {
    ...iconForTool(name),
    label: justification || fallbackLabel(name, args),
    known,
    // A bare, unrecognized tool name reads as code (it *is* an identifier).
    mono: !known && !justification,
    // Only the model's justification is trusted as Markdown; fallbacks stay literal.
    labelIsMarkdown: !!justification,
  };
}
