import type { IconType } from "react-icons/lu";
import {
  LuGlobe,
  LuTerminal,
  LuUsers,
  LuNetwork,
  LuListChecks,
  LuRefreshCw,
  LuWrench,
  LuCheck,
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
    case "orchestrate":
      return { icon: LuNetwork, iconColor: "orange.fg" };
    case "write_tasks":
    case "update_tasks":
      return { icon: LuListChecks, iconColor: "teal.fg" };
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
      return args?.agent ? `Spawning "${String(args.agent)}" agent` : "Spawning agent";
    case "orchestrate": {
      const steps = Array.isArray(args?.steps) ? args.steps : [];
      return `Orchestrating ${steps.length} step${steps.length !== 1 ? "s" : ""}`;
    }
    case "write_tasks": {
      const tasks = Array.isArray(args?.tasks) ? args.tasks : [];
      return `Creating ${tasks.length} task${tasks.length !== 1 ? "s" : ""}`;
    }
    case "update_tasks":
      return "Updating tasks";
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

  switch (toolName) {
    case "web_search": {
      const query = data?.query ? String(data.query) : "";
      return {
        icon: LuGlobe,
        iconColor: "blue.fg",
        label: query ? `Results for "${query}" received` : "Browse results received",
      };
    }
    case "bash":
      return { icon: LuTerminal, iconColor: "green.fg", label: "Command finished" };
    case "spawn_agent":
    case "agent":
      return { icon: LuUsers, iconColor: "purple.fg", label: "Agent finished" };
    case "orchestrate":
      return { icon: LuNetwork, iconColor: "orange.fg", label: "Orchestration finished" };
    case "write_tasks":
      return { icon: LuListChecks, iconColor: "teal.fg", label: "Tasks created" };
    case "update_tasks":
      return { icon: LuRefreshCw, iconColor: "teal.fg", label: "Tasks updated" };
    default:
      return {
        icon: LuCheck,
        iconColor: "green.fg",
        label: toolName ? `${toolName} finished` : "Finished",
      };
  }
}
