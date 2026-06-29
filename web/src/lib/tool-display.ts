import type { IconType } from "react-icons";
import {
  LuGlobe,
  LuTerminal,
  LuUsers,
  LuNetwork,
  LuListChecks,
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
  LuTarget,
} from "react-icons/lu";

interface ToolDisplayInfo {
  icon: IconType;
  iconColor: string;
  label: string;
}

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
    case "update_goal":
      return { icon: LuTarget, iconColor: "red.fg" };
    case "open_web_preview":
    case "render_widget":
      return { icon: LuLayoutDashboard, iconColor: "pink.fg" };
    case "write_tasks":
    case "update_tasks":
      return { icon: LuListChecks, iconColor: "teal.fg" };
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
    case "read_task":
      return "Reading a related task";
    case "read_file":
      return args?.file_path ? `Reading ${shortPath(String(args.file_path))}` : "Reading file";
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
    case "update_goal":
      return "Updating goal";
    case "open_web_preview":
      return args?.title ? `Previewing "${String(args.title)}"` : "Opening a web preview";
    case "render_widget":
      return args?.title ? `Rendering "${String(args.title)}"` : "Rendering a widget";
    case "write_tasks":
      return "Creating tasks";
    case "update_tasks":
      return "Updating tasks";
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

function shortPath(path: string): string {
  const parts = path.split("/");
  return parts.length > 2 ? `…/${parts.slice(-2).join("/")}` : path;
}

export function getToolCallDisplay(
  name: string,
  args?: Record<string, unknown>
): ToolDisplayInfo {
  const justification = args?.justification ? String(args.justification) : "";
  return {
    ...iconForTool(name),
    label: justification || fallbackLabel(name, args),
  };
}
