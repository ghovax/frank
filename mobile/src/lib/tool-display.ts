/**
 * What a tool call is called, and which glyph stands for it.
 *
 * A transcription of `web/src/lib/tool-display.ts`, with the desktop's translator collapsed into
 * literal English: this client is not translated yet, and pretending otherwise by threading a
 * `t()` through would be scaffolding around a thing that does not exist.
 *
 * The rule the desktop states and this keeps: the model's own `explanation` is the label when it
 * gave one, and a derived label — the command, the path, the query — only when it did not.
 */

import {
  Braces, Download, FilePen, FilePlus, FileText, Globe, ListTodo, MessageCircleQuestion,
  MousePointerClick, Plug, SearchCode, Sparkles, Target, Terminal, UserSearch, Wrench,
  type LucideIcon,
} from "lucide-react-native";

import type { Palette } from "../theme/tokens";

export interface ToolDisplay {
  icon: LucideIcon;
  tint: keyof Palette;
  label: string;
  /** A bare unrecognised tool name reads as code, because it is an identifier. */
  mono: boolean;
}

const KNOWN = new Set([
  "search_web", "bash", "read_file", "search_code", "control_screen", "edit_file", "write_file",
  "fetch_url", "ask_user", "load_skill", "set_tasks", "update_tasks", "update_goal", "work_habits",
  "call_mcp_tool", "list_mcp_tools", "list_mcp_resources", "read_mcp_resource",
]);

function glyph(name: string): { icon: LucideIcon; tint: keyof Palette } {
  switch (name) {
    case "search_web": return { icon: Globe, tint: "blueFg" };
    case "bash": return { icon: Terminal, tint: "greenFg" };
    case "read_file": return { icon: FileText, tint: "blueFg" };
    case "search_code": return { icon: SearchCode, tint: "greenFg" };
    case "control_screen": return { icon: MousePointerClick, tint: "purpleFg" };
    case "edit_file": return { icon: FilePen, tint: "yellowFg" };
    case "write_file": return { icon: FilePlus, tint: "greenFg" };
    case "fetch_url": return { icon: Download, tint: "blueFg" };
    case "ask_user": return { icon: MessageCircleQuestion, tint: "purpleFg" };
    case "load_skill": return { icon: Sparkles, tint: "pinkFg" };
    case "set_tasks":
    case "update_tasks": return { icon: ListTodo, tint: "blueFg" };
    case "update_goal": return { icon: Target, tint: "orangeFg" };
    case "work_habits": return { icon: UserSearch, tint: "blueFg" };
    case "call_mcp_tool":
    case "list_mcp_tools":
    case "list_mcp_resources":
    case "read_mcp_resource": return { icon: Plug, tint: "purpleFg" };
    default: return { icon: Wrench, tint: "fgMuted" };
  }
}

/** The agent's shell wrapper: `cd '…' && real command`. Only the real command is worth showing. */
function stripDirectoryPrefix(command: string): string {
  const matched = command.match(/^cd\s+'[^']*'\s+&&\s+([\s\S]*)/);
  return matched ? matched[1] : command;
}

function fileName(path: string): string {
  return path.split("/").filter(Boolean).at(-1) ?? path;
}

function shortPath(path: string): string {
  const parts = path.split("/");
  return parts.length > 2 ? `…/${parts.slice(-2).join("/")}` : path;
}

function derivedLabel(name: string, args: Record<string, unknown> | undefined): string {
  const text = (key: string) => (args?.[key] == null ? "" : String(args[key]));
  switch (name) {
    case "search_web": return text("query") ? `Searched the web for “${text("query")}”` : "Searched the web";
    case "bash": return text("command") ? stripDirectoryPrefix(text("command")) : "Ran a command";
    case "read_file": return text("file_path") ? `Read ${fileName(text("file_path"))}` : "Read a file";
    case "search_code": return text("query") ? `Searched the code for “${text("query")}”` : "Searched the code";
    case "control_screen": {
      const first = text("script").split("\n").map((line) => line.trim()).find(Boolean) ?? "";
      if (first) return first;
      return text("surface") ? `Controlled ${text("surface")}` : "Controlled the screen";
    }
    case "edit_file": return text("file_path") ? `Edited ${shortPath(text("file_path"))}` : "Edited a file";
    case "write_file": return text("file_path") ? `Wrote ${shortPath(text("file_path"))}` : "Wrote a file";
    case "fetch_url": return text("url") || "Fetched a page";
    case "ask_user": return "Asked a question";
    case "load_skill": return text("name") ? `Loaded the ${text("name")} skill` : "Loaded a skill";
    case "set_tasks": return "Set the task list";
    case "update_tasks": return "Updated the task list";
    case "update_goal": return "Updated the goal";
    case "work_habits": return "Looked up how you work";
    case "call_mcp_tool": return text("tool_name") ? `Called ${text("tool_name")}` : "Called an MCP tool";
    case "list_mcp_tools": return "Listed the MCP tools";
    case "list_mcp_resources": return "Listed the MCP resources";
    case "read_mcp_resource": return text("uri") ? `Read ${text("uri")}` : "Read an MCP resource";
    default: return name;
  }
}

export function toolDisplay(name: string, args: Record<string, unknown> | undefined): ToolDisplay {
  const known = KNOWN.has(name);
  const explanation = args?.explanation ? String(args.explanation) : "";
  return {
    ...glyph(name),
    label: explanation || derivedLabel(name, args),
    mono: !known && !explanation,
  };
}

/** The `Braces` glyph, for a JSON body with nothing more specific to say about it. */
export const RAW_ICON = Braces;
