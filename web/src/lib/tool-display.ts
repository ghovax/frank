import type { IconType } from "react-icons";
import {
  LuGlobe,
  LuTerminal,
  LuUsers,
  LuNetwork,
  LuListChecks,
  LuPuzzle,
  LuWrench,
  LuCheck,
  LuLayoutDashboard,
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
    case "render_widget":
      return args?.title ? `Rendering "${String(args.title)}"` : "Rendering a widget";
    case "write_tasks": {
      const tasks = Array.isArray(args?.tasks) ? args.tasks : [];
      return `Creating ${tasks.length} task${tasks.length !== 1 ? "s" : ""}`;
    }
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

function parseResultContent(content?: string): Record<string, unknown> | null {
  if (!content) return null;
  try {
    const parsed = JSON.parse(content);
    return typeof parsed === "object" && parsed !== null ? parsed : null;
  } catch {
    return null;
  }
}

export function getToolResultDisplay(name?: string, content?: string): ToolDisplayInfo {
  const toolName = name ?? "";
  const data = parseResultContent(content);
  const icon = LuCheck;
  const iconColor = "green.fg";

  switch (toolName) {
    case "web_search": {
      const query = data?.query ? String(data.query) : "";
      return {
        icon,
        iconColor,
        label: query ? `Results for "${query}" received` : "Browse results received",
      };
    }
    case "bash":
      return { icon, iconColor, label: "Command finished" };
    case "spawn_agent":
    case "agent":
      return { icon, iconColor, label: "Agent finished" };
    case "read_task":
      return { icon, iconColor, label: "Task read" };
    case "render_widget":
      return { ...iconForTool(toolName), label: "Widget rendered" };
    case "write_tasks":
      return { icon, iconColor, label: "Tasks created" };
    case "update_tasks":
      return { icon, iconColor, label: "Tasks updated" };
    case "call_mcp_tool":
      return { ...iconForTool(toolName), label: "MCP tool finished" };
    case "list_mcp_tools":
      return { ...iconForTool(toolName), label: "MCP tools listed" };
    case "list_mcp_resources":
      return { ...iconForTool(toolName), label: "MCP resources listed" };
    case "read_mcp_resource":
      return { ...iconForTool(toolName), label: "MCP resource read" };
    default:
      return {
        icon,
        iconColor,
        label: toolName ? `${toolName} finished` : "Finished",
      };
  }
}
