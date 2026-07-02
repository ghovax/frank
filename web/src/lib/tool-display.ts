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
  LuClipboardList,
  LuSearchCheck,
  LuPanelRightOpen,
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
    case "open_preview":
    case "render_widget":
      return { icon: LuLayoutDashboard, iconColor: "pink.fg" };
    case "research_board":
      return { icon: LuClipboardList, iconColor: "cyan.fg" };
    case "research_evidence":
      return { icon: LuSearchCheck, iconColor: "teal.fg" };
    case "research_open":
      return { icon: LuPanelRightOpen, iconColor: "blue.fg" };
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
    case "open_preview":
      return args?.title ? `Previewing "${String(args.title)}"` : "Opening a preview";
    case "research_board":
      return researchBoardLabel(args);
    case "research_evidence":
      return researchEvidenceLabel(args);
    case "research_open":
      return researchOpenLabel(args);
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

const RESEARCH_TARGET_LABELS: Record<string, string> = {
  workspace: "workspace",
  source: "source",
  preparation_run: "preparation run",
  evidence: "evidence",
  anchor: "citation anchor",
  report: "report",
  note: "note",
  quarantine: "quarantine record",
};

const RESEARCH_BOARD_LABELS: Record<string, (target: string) => string> = {
  insert: (target) =>
    target === "workspace" ? "Starting research workspace" : `Adding research ${target}`,
  annotate: (target) => `Annotating research ${target}`,
  exclude: (target) => `Excluding research ${target}`,
  supersede: (target) => `Superseding research ${target}`,
  prepare: (target) => `Preparing research ${target}`,
  publish: (target) => `Publishing research ${target}`,
  inspect: (target) => `Inspecting research ${target}`,
};

const RESEARCH_EVIDENCE_LABELS: Record<string, string> = {
  source: "Reading research source",
  anchor: "Reading citation anchor",
  quarantine: "Reviewing quarantined sources",
  validate_report: "Validating research report",
};

const RESEARCH_OPEN_LABELS: Record<string, string> = {
  anchor: "Opening citation preview",
  source: "Opening research source",
  report: "Opening research report",
};

function researchKey(value: unknown): string {
  return String(value ?? "").trim().toLowerCase().replace(/[\s-]+/g, "_");
}

function researchTarget(value: unknown): string {
  const key = researchKey(value);
  return RESEARCH_TARGET_LABELS[key] ?? (key ? key.replace(/_/g, " ") : "item");
}

function researchBoardLabel(args?: Record<string, unknown>): string {
  const label = RESEARCH_BOARD_LABELS[researchKey(args?.action)];
  return label ? label(researchTarget(args?.target)) : "Updating research board";
}

function researchEvidenceLabel(args?: Record<string, unknown>): string {
  if (researchKey(args?.operation) === "search") {
    const query = String(args?.query ?? "").trim();
    return query ? `Searching evidence for "${query}"` : "Searching research evidence";
  }
  return RESEARCH_EVIDENCE_LABELS[researchKey(args?.operation)] ?? "Reading research evidence";
}

function researchOpenLabel(args?: Record<string, unknown>): string {
  return RESEARCH_OPEN_LABELS[researchKey(args?.target)] ?? "Opening research artifact";
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
